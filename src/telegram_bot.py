import asyncio
import base64
import io
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from src import config
from src.db import (
    build_context,
    get_active_tasks,
    get_db_stats,
    get_dm_summary_data,
    get_known_chats,
    get_module_health,
    get_setting,
    search_messages,
    set_setting,
    complete_task,
    cancel_task,
)
from src.ai_brain import brain
from src.telegram_listener import resolve_chat_names
from src.confidence_manager import (
    resolve_batch_all_tasks,
    resolve_batch_nothing,
    resolve_single,
)

logger = logging.getLogger("jarvis.bot")

bot: Bot = None
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ─── Утилиты ─────────────────────────────────────────────────

def _now_local() -> datetime:
    """Текущее время в часовом поясе владельца (Красноярск UTC+7)."""
    return datetime.now(timezone.utc) + timedelta(hours=config.USER_TIMEZONE_OFFSET)


def owner_only(handler):
    """Декоратор: только владелец может использовать."""
    async def wrapper(message: Message, **kwargs):
        if message.from_user.id != config.TELEGRAM_OWNER_ID:
            return
        return await handler(message)
    return wrapper


async def _mode_footer() -> str:
    """Футер с индикатором AI-режима и статусом модулей."""
    mode = await brain.get_mode()
    health = await get_module_health()
    ok_count = sum(1 for h in health if h["status"] == "ok")
    total = len(health) if health else 0

    if mode == "cli":
        return f"\n\n— CLI mode | {ok_count}/{total} модулей OK"
    else:
        cost = brain.last_api_cost
        return f"\n\n— API mode (${cost:.3f}) | {ok_count}/{total} модулей OK"


async def send_to_owner(text: str, reply_markup=None):
    """Отправка сообщения владельцу."""
    # Telegram лимит 4096 символов
    if len(text) > 4096:
        text = text[:4090] + "..."
    await bot.send_message(
        config.TELEGRAM_OWNER_ID,
        text,
        reply_markup=reply_markup,
        parse_mode=None,
    )


# Callback для уведомлений из других модулей
async def notify_callback(text: str, **kwargs):
    """Универсальный callback для уведомлений из listener/confidence/scheduler."""
    markup_type = kwargs.get("reply_markup_type")
    markup = None

    if markup_type == "new_contact":
        contact_id = kwargs.get("contact_id", 0)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Мониторить", callback_data=f"contact_monitor:{contact_id}"),
                InlineKeyboardButton(text="Только сохранять", callback_data=f"contact_save:{contact_id}"),
                InlineKeyboardButton(text="Игнорировать", callback_data=f"contact_ignore:{contact_id}"),
            ]
        ])

    elif markup_type == "urgent_confidence":
        queue_id = kwargs.get("queue_id", 0)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, задача", callback_data=f"conf_yes:{queue_id}"),
                InlineKeyboardButton(text="Нет", callback_data=f"conf_no:{queue_id}"),
                InlineKeyboardButton(text="Позже", callback_data=f"conf_later:{queue_id}"),
            ]
        ])

    elif markup_type == "batch_confidence":
        queue_ids = kwargs.get("queue_ids", [])
        ids_str = ",".join(str(q) for q in queue_ids)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Все задачи", callback_data=f"batch_all:{ids_str}"),
                InlineKeyboardButton(text="Ничего", callback_data=f"batch_none:{ids_str}"),
                InlineKeyboardButton(text="Выбрать", callback_data=f"batch_pick:{ids_str}"),
            ]
        ])

    await send_to_owner(text, reply_markup=markup)


# ─── Постоянная клавиатура ───────────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Запрос")]],
    resize_keyboard=True,
    is_persistent=True,
)


# ─── Команды ─────────────────────────────────────────────────

@router.message(Command("start"))
@owner_only
async def cmd_start(message: Message):
    await message.answer(
        "Jarvis активен. Нажми «Запрос» или используй команды.",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("help"))
@owner_only
async def cmd_help(message: Message):
    text = (
        "КОМАНДЫ JARVIS:\n\n"
        "Запрос     — свободный вопрос (кнопка внизу)\n"
        "/tasks     — активные задачи с дедлайнами\n"
        "/summary   — краткое содержание дня\n"
        "/health    — статус системы и модулей\n"
        "/whitelist — чаты для мониторинга\n"
        "/blacklist — исключения из мониторинга\n"
        "/admin     — управление: перезапуск, логи, бэкап\n"
        "/mode      — AI-режим (CLI/API), переключение\n"
        "/settings  — настройки (лимиты, whitelist, расписание)\n"
        "/help      — эта справка\n\n"
        "ФОТО: отправь скриншот — проанализирую содержимое\n\n"
        "ТЕКСТОМ (без команд):\n"
        "\"Переключи на API\" — смена AI-режима\n"
        "Любой вопрос — Jarvis поймёт контекст"
    )
    await send_to_owner(text)


@router.message(Command("tasks"))
@owner_only
async def cmd_tasks(message: Message):
    tasks = await get_active_tasks()
    if not tasks:
        await send_to_owner("Активных задач нет.")
        return

    lines = ["АКТИВНЫЕ ЗАДАЧИ:\n"]
    for t in tasks:
        type_emoji = {"task": "T", "promise_mine": "P>", "promise_incoming": ">P"}.get(t["type"], "?")
        deadline_str = ""
        if t["deadline"]:
            deadline_str = f" | до {t['deadline'].strftime('%d.%m')}"
        who_str = f" [{t['who']}]" if t.get("who") else ""
        lines.append(f"#{t['id']} [{type_emoji}] {t['description']}{who_str}{deadline_str}")

    # Кнопки управления задачами (первые 5)
    buttons = []
    for t in tasks[:5]:
        buttons.append([
            InlineKeyboardButton(text=f"Выполнено #{t['id']}", callback_data=f"task_done:{t['id']}"),
            InlineKeyboardButton(text=f"Отменить #{t['id']}", callback_data=f"task_cancel:{t['id']}"),
        ])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await send_to_owner("\n".join(lines), reply_markup=markup)


@router.message(Command("summary"))
@owner_only
async def cmd_summary(message: Message):
    await send_to_owner("Генерирую дайджест...")

    stats = await get_db_stats()
    tasks = await get_active_tasks()

    data = {
        "completed": 0,
        "in_progress": len(tasks),
        "new_tasks": 0,
        "messages_count": stats.get("messages", 0),
        "events": [],
    }
    digest = await brain.generate_digest(data)
    await send_to_owner(digest)


@router.message(Command("health"))
@owner_only
async def cmd_health(message: Message):
    health = await get_module_health()
    stats = await get_db_stats()

    now = _now_local()
    lines = [f"Статус ({now.strftime('%H:%M')} {config.USER_TIMEZONE_NAME}):\n"]

    for h in health:
        status = "OK" if h["status"] == "ok" else "FAIL"
        ago = ""
        if h.get("timestamp"):
            delta = datetime.now(h["timestamp"].tzinfo) - h["timestamp"]
            minutes = int(delta.total_seconds() / 60)
            ago = f"  heartbeat: {minutes}м назад"
        error_str = f"  err: {h['error']}" if h.get("error") else ""
        lines.append(f"  {h['module']:25s} {status}{ago}{error_str}")

    mode = await brain.get_mode()
    lines.append(f"\nБД: PostgreSQL OK, {stats.get('db_size', '?')}")
    lines.append(f"AI mode: {'CLI (подписка)' if mode == 'cli' else 'API (токены)'}")

    # Аккаунты
    acc_info = f"Аккаунты: [{config.ACCOUNT_LABEL_1}]"
    if config.TELEGRAM_API_ID_2:
        acc_info += f" + [{config.ACCOUNT_LABEL_2}]"
    lines.append(acc_info)

    await send_to_owner("\n".join(lines))


@router.message(Command("mode"))
@owner_only
async def cmd_mode(message: Message):
    mode = await brain.get_mode()
    label = "CLI (Claude Code, подписка)" if mode == "cli" else "API (Claude API, токены)"
    other = "API" if mode == "cli" else "CLI"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Переключить на {other}", callback_data=f"switch_mode:{other.lower()}")]
    ])

    await send_to_owner(f"Текущий режим: {label}", reply_markup=markup)


@router.message(Command("admin"))
@owner_only
async def cmd_admin(message: Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Перезапустить модуль", callback_data="admin:restart"),
            InlineKeyboardButton(text="Показать логи", callback_data="admin:logs"),
        ],
        [
            InlineKeyboardButton(text="Бэкап БД", callback_data="admin:backup"),
            InlineKeyboardButton(text="Статус VPS", callback_data="admin:vps"),
        ],
    ])
    await send_to_owner("Управление:", reply_markup=markup)


@router.message(Command("settings"))
@owner_only
async def cmd_settings(message: Message):
    mode = await brain.get_mode()
    limit = await get_setting("confidence_daily_limit", str(config.CONFIDENCE_DAILY_LIMIT))
    batch_hour = await get_setting("confidence_batch_hour", str(config.CONFIDENCE_BATCH_HOUR))
    whitelist = await get_setting("whitelist", "[]")

    try:
        wl_list = json.loads(whitelist)
    except json.JSONDecodeError:
        wl_list = []

    text = (
        f"НАСТРОЙКИ:\n\n"
        f"AI-режим: {mode}\n"
        f"Confidence лимит: {limit}/день\n"
        f"Confidence батч: 17:00 {config.USER_TIMEZONE_NAME}\n"
        f"Whitelist чатов: {len(wl_list)}\n"
    )
    await send_to_owner(text)


# ─── Callback-обработчики ────────────────────────────────────

@router.callback_query(F.data.startswith("switch_mode:"))
async def cb_switch_mode(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    new_mode = callback.data.split(":")[1]
    await brain.set_mode(new_mode)
    label = "CLI (подписка)" if new_mode == "cli" else "API (токены)"
    await callback.answer(f"Переключено на {label}")
    await send_to_owner(f"Режим переключён на: {label}")


@router.callback_query(F.data.startswith("task_done:"))
async def cb_task_done(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    task_id = int(callback.data.split(":")[1])
    await complete_task(task_id)
    await callback.answer(f"Задача #{task_id} выполнена")


@router.callback_query(F.data.startswith("task_cancel:"))
async def cb_task_cancel(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    task_id = int(callback.data.split(":")[1])
    await cancel_task(task_id)
    await callback.answer(f"Задача #{task_id} отменена")


@router.callback_query(F.data.startswith("conf_yes:"))
async def cb_conf_yes(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    queue_id = int(callback.data.split(":")[1])
    await resolve_single(queue_id, "task")
    await callback.answer("Добавлено как задача")


@router.callback_query(F.data.startswith("conf_no:"))
async def cb_conf_no(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    queue_id = int(callback.data.split(":")[1])
    await resolve_single(queue_id, "info")
    await callback.answer("Пропущено")


@router.callback_query(F.data.startswith("batch_all:"))
async def cb_batch_all(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    ids = [int(x) for x in callback.data.split(":")[1].split(",") if x]
    await resolve_batch_all_tasks(ids)
    await callback.answer(f"Все {len(ids)} добавлены как задачи")


@router.callback_query(F.data.startswith("batch_none:"))
async def cb_batch_none(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    ids = [int(x) for x in callback.data.split(":")[1].split(",") if x]
    await resolve_batch_nothing(ids)
    await callback.answer("Все отклонены")


@router.callback_query(F.data.startswith("admin:"))
async def cb_admin(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    action = callback.data.split(":")[1]

    if action == "restart":
        modules = [
            "telegram_listener", "telegram_bot", "ai_brain",
            "email_checker", "whisper_processor", "scheduler", "watchdog",
        ]
        buttons = [
            [InlineKeyboardButton(text=m, callback_data=f"restart_mod:{m}")]
            for m in modules
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text("Какой модуль перезапустить?", reply_markup=markup)

    elif action == "logs":
        await callback.answer("Логи...")
        try:
            result = subprocess.run(
                ["journalctl", "-u", "jarvis-*", "-n", "20", "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            logs = result.stdout[-3000:] if result.stdout else "Логов нет"
            await send_to_owner(f"ЛОГИ:\n{logs}")
        except Exception as e:
            await send_to_owner(f"Ошибка чтения логов: {e}")

    elif action == "backup":
        await callback.answer("Бэкап...")
        try:
            result = subprocess.run(
                ["pg_dump", "-U", config.DB_USER, config.DB_NAME, "-f", "/tmp/jarvis_backup.sql"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                await send_to_owner("Бэкап создан: /tmp/jarvis_backup.sql")
            else:
                await send_to_owner(f"Ошибка бэкапа: {result.stderr}")
        except Exception as e:
            await send_to_owner(f"Ошибка бэкапа: {e}")

    elif action == "vps":
        await callback.answer("Статус...")
        try:
            uptime = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5).stdout.strip()
            df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5).stdout.strip()
            free = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5).stdout.strip()
            await send_to_owner(f"VPS:\n{uptime}\n\nDisk:\n{df}\n\nRAM:\n{free}")
        except Exception as e:
            await send_to_owner(f"Ошибка: {e}")


@router.callback_query(F.data.startswith("restart_mod:"))
async def cb_restart_module(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    module = callback.data.split(":")[1]
    await callback.answer(f"Перезапуск {module}...")
    try:
        result = subprocess.run(
            ["systemctl", "restart", f"jarvis-{module}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            await send_to_owner(f"Модуль {module} перезапущен.")
        else:
            await send_to_owner(f"Ошибка перезапуска {module}: {result.stderr}")
    except Exception as e:
        await send_to_owner(f"Ошибка: {e}")


# ─── Whitelist чатов ──────────────────────────────────────────

@router.message(Command("whitelist"))
@owner_only
async def cmd_whitelist(message: Message):
    args = message.text.strip().split(maxsplit=1)
    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    # /whitelist — показать список + компактная кнопка для управления
    if len(args) < 2:
        lines = []
        if wl:
            # Получаем названия напрямую из Telegram через Telethon
            chat_names = await resolve_chat_names(wl)
            lines.append(f"Whitelist ({len(wl)} чатов):")
            for cid in wl:
                name = chat_names.get(cid, "")
                label = f"{cid} ({name})" if name else str(cid)
                lines.append(f"  • {label}")
        else:
            lines.append("Whitelist пуст.")

        lines.append("\nПерешли сообщение из группы — добавлю автоматически.")

        # Одна компактная кнопка — развернуть список чатов
        buttons = [[InlineKeyboardButton(
            text="Управление чатами",
            callback_data="wl_manage",
        )]]
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await send_to_owner("\n".join(lines), reply_markup=markup)
        return

    subcmd = args[1].strip()

    # /whitelist clear
    if subcmd == "clear":
        await set_setting("whitelist", "[]")
        await send_to_owner("Whitelist очищен.")
        return

    # /whitelist add <id> или /whitelist del <id>
    parts = subcmd.split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in ("add", "del"):
        await send_to_owner("Формат: /whitelist add <chat_id> или /whitelist del <chat_id>")
        return

    action = parts[0]
    raw_ids = parts[1].replace(",", " ").split()
    added, removed, errors = [], [], []

    for raw_id in raw_ids:
        try:
            chat_id = int(raw_id.strip())
        except ValueError:
            errors.append(raw_id)
            continue

        if action == "add":
            if chat_id not in wl:
                wl.append(chat_id)
                added.append(str(chat_id))
        elif action == "del":
            if chat_id in wl:
                wl.remove(chat_id)
                removed.append(str(chat_id))

    await set_setting("whitelist", json.dumps(wl))

    result = []
    if added:
        result.append(f"Добавлено: {', '.join(added)}")
    if removed:
        result.append(f"Удалено: {', '.join(removed)}")
    if errors:
        result.append(f"Ошибка (не число): {', '.join(errors)}")
    result.append(f"Всего в whitelist: {len(wl)}")

    await send_to_owner("\n".join(result))


# ─── Whitelist callbacks ─────────────────────────────────────

@router.callback_query(F.data == "wl_manage")
async def cb_wl_manage(callback: CallbackQuery):
    """Показать список известных чатов для управления whitelist."""
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return

    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    # Собираем все известные chat_id: из БД + из whitelist
    known = await get_known_chats(exclude_private=True)
    all_ids = list({c["chat_id"] for c in known} | set(wl))
    if not all_ids:
        await callback.answer("Пока нет известных групп")
        return

    # Получаем актуальные названия через Telethon
    chat_names = await resolve_chat_names(all_ids)

    buttons = []
    row = []
    for cid in all_ids[:10]:
        title = chat_names.get(cid, str(cid))
        short = title[:18] if len(title) <= 18 else title[:16] + ".."
        if cid in wl:
            row.append(InlineKeyboardButton(text=f"❌ {short}", callback_data=f"wl_del:{cid}"))
        else:
            row.append(InlineKeyboardButton(text=f"➕ {short}", callback_data=f"wl_add:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if wl:
        buttons.append([InlineKeyboardButton(text="Очистить всё", callback_data="wl_clear")])
    buttons.append([InlineKeyboardButton(text="Закрыть", callback_data="wl_close")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        f"Whitelist: {len(wl)} чатов. ➕ = добавить, ❌ = убрать:",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("wl_add:"))
async def cb_wl_add(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    chat_id = int(callback.data.split(":")[1])
    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    if chat_id not in wl:
        wl.append(chat_id)
        await set_setting("whitelist", json.dumps(wl))
        await callback.answer(f"Добавлен: {chat_id}")
    else:
        await callback.answer("Уже в whitelist")

    # Обновляем сообщение — показываем актуальное состояние
    await _refresh_wl_manage(callback.message, wl)


@router.callback_query(F.data.startswith("wl_del:"))
async def cb_wl_del(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    chat_id = int(callback.data.split(":")[1])
    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    if chat_id in wl:
        wl.remove(chat_id)
        await set_setting("whitelist", json.dumps(wl))
        await callback.answer(f"Удалён: {chat_id}")
    else:
        await callback.answer("Не было в whitelist")

    await _refresh_wl_manage(callback.message, wl)


@router.callback_query(F.data == "wl_clear")
async def cb_wl_clear(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    await set_setting("whitelist", "[]")
    await callback.answer("Whitelist очищен")
    await callback.message.edit_text("Whitelist очищен.")


@router.callback_query(F.data == "wl_close")
async def cb_wl_close(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []
    count = len(wl)
    await callback.message.edit_text(f"Whitelist: {count} чатов.")


async def _refresh_wl_manage(message, wl: list):
    """Перерисовать кнопки управления whitelist."""
    known = await get_known_chats(exclude_private=True)
    all_ids = list({c["chat_id"] for c in known} | set(wl))
    chat_names = await resolve_chat_names(all_ids)

    buttons = []
    row = []
    for cid in all_ids[:10]:
        title = chat_names.get(cid, str(cid))
        short = title[:18] if len(title) <= 18 else title[:16] + ".."
        if cid in wl:
            row.append(InlineKeyboardButton(text=f"❌ {short}", callback_data=f"wl_del:{cid}"))
        else:
            row.append(InlineKeyboardButton(text=f"➕ {short}", callback_data=f"wl_add:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if wl:
        buttons.append([InlineKeyboardButton(text="Очистить всё", callback_data="wl_clear")])
    buttons.append([InlineKeyboardButton(text="Закрыть", callback_data="wl_close")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await message.edit_text(
            f"Whitelist: {len(wl)} чатов. ➕ = добавить, ❌ = убрать:",
            reply_markup=markup,
        )
    except Exception:
        pass  # Сообщение не изменилось — игнорируем


# ─── Обработка пересланных сообщений (для whitelist) ─────────

@router.message(F.forward_from_chat)
@owner_only
async def handle_forwarded_from_chat(message: Message):
    """Пересланное сообщение из группы/канала — предложить добавить в whitelist."""
    chat = message.forward_from_chat
    chat_id = chat.id
    chat_title = chat.title or str(chat_id)

    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    if chat_id in wl:
        await send_to_owner(f"Чат «{chat_title}» ({chat_id}) уже в whitelist.")
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, добавить", callback_data=f"wl_fwd_add:{chat_id}"),
        InlineKeyboardButton(text="Нет", callback_data="wl_fwd_no"),
    ]])
    await send_to_owner(
        f"Добавить «{chat_title}» ({chat_id}) в whitelist?",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("wl_fwd_add:"))
async def cb_wl_fwd_add(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    chat_id = int(callback.data.split(":")[1])
    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    if chat_id not in wl:
        wl.append(chat_id)
        await set_setting("whitelist", json.dumps(wl))

    await callback.answer("Добавлено!")
    await callback.message.edit_text(
        f"Чат {chat_id} добавлен в whitelist. Всего: {len(wl)}."
    )


@router.callback_query(F.data == "wl_fwd_no")
async def cb_wl_fwd_no(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    await callback.answer("Ок")
    await callback.message.edit_text("Ок, не добавляю.")


# ─── Blacklist ────────────────────────────────────────────────

@router.message(Command("blacklist"))
@owner_only
async def cmd_blacklist(message: Message):
    args = message.text.strip().split(maxsplit=1)
    raw = await get_setting("blacklist", "[]")
    try:
        bl = json.loads(raw)
    except json.JSONDecodeError:
        bl = []

    if len(args) < 2:
        lines = []
        if bl:
            chat_names = await resolve_chat_names(bl)
            lines.append(f"Blacklist ({len(bl)} записей):")
            for cid in bl:
                name = chat_names.get(cid, "")
                label = f"{cid} ({name})" if name else str(cid)
                lines.append(f"  • {label}")
        else:
            lines.append("Blacklist пуст.")

        lines.append("\nДобавить: /blacklist add <id>\nУдалить: /blacklist del <id>")

        buttons = [[InlineKeyboardButton(text="Управление", callback_data="bl_manage")]]
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await send_to_owner("\n".join(lines), reply_markup=markup)
        return

    subcmd = args[1].strip()

    if subcmd == "clear":
        await set_setting("blacklist", "[]")
        await send_to_owner("Blacklist очищен.")
        return

    parts = subcmd.split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in ("add", "del"):
        await send_to_owner("Формат: /blacklist add <id> или /blacklist del <id>")
        return

    action = parts[0]
    raw_ids = parts[1].replace(",", " ").split()
    added, removed, errors = [], [], []

    for raw_id in raw_ids:
        try:
            item_id = int(raw_id.strip())
        except ValueError:
            errors.append(raw_id)
            continue

        if action == "add":
            if item_id not in bl:
                bl.append(item_id)
                added.append(str(item_id))
        elif action == "del":
            if item_id in bl:
                bl.remove(item_id)
                removed.append(str(item_id))

    await set_setting("blacklist", json.dumps(bl))

    result = []
    if added:
        result.append(f"Заблокировано: {', '.join(added)}")
    if removed:
        result.append(f"Разблокировано: {', '.join(removed)}")
    if errors:
        result.append(f"Ошибка (не число): {', '.join(errors)}")
    result.append(f"Всего в blacklist: {len(bl)}")

    await send_to_owner("\n".join(result))


@router.callback_query(F.data == "bl_manage")
async def cb_bl_manage(callback: CallbackQuery):
    """Показать известные чаты/контакты для добавления в blacklist."""
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return

    raw = await get_setting("blacklist", "[]")
    try:
        bl = json.loads(raw)
    except json.JSONDecodeError:
        bl = []

    # Собираем все известные chat_id из БД + blacklist
    known = await get_known_chats(exclude_private=False)
    all_ids = list({c["chat_id"] for c in known} | set(bl))
    if not all_ids:
        await callback.answer("Пока нет известных чатов")
        return

    chat_names = await resolve_chat_names(all_ids)

    buttons = []
    row = []
    for cid in all_ids[:12]:
        title = chat_names.get(cid, str(cid))
        short = title[:18] if len(title) <= 18 else title[:16] + ".."
        if cid in bl:
            row.append(InlineKeyboardButton(text=f"✅ {short}", callback_data=f"bl_del:{cid}"))
        else:
            row.append(InlineKeyboardButton(text=f"🚫 {short}", callback_data=f"bl_add:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if bl:
        buttons.append([InlineKeyboardButton(text="Очистить blacklist", callback_data="bl_clear")])
    buttons.append([InlineKeyboardButton(text="Закрыть", callback_data="bl_close")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        f"Blacklist: {len(bl)}. 🚫 = заблокировать, ✅ = разблокировать:",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("bl_add:"))
async def cb_bl_add(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    item_id = int(callback.data.split(":")[1])
    raw = await get_setting("blacklist", "[]")
    try:
        bl = json.loads(raw)
    except json.JSONDecodeError:
        bl = []

    if item_id not in bl:
        bl.append(item_id)
        await set_setting("blacklist", json.dumps(bl))
        await callback.answer(f"Заблокирован: {item_id}")
    else:
        await callback.answer("Уже в blacklist")

    await _refresh_bl_manage(callback.message, bl)


@router.callback_query(F.data.startswith("bl_del:"))
async def cb_bl_del(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    item_id = int(callback.data.split(":")[1])
    raw = await get_setting("blacklist", "[]")
    try:
        bl = json.loads(raw)
    except json.JSONDecodeError:
        bl = []

    if item_id in bl:
        bl.remove(item_id)
        await set_setting("blacklist", json.dumps(bl))
        await callback.answer(f"Разблокирован: {item_id}")
    else:
        await callback.answer("Не было в blacklist")

    await _refresh_bl_manage(callback.message, bl)


@router.callback_query(F.data == "bl_clear")
async def cb_bl_clear(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    await set_setting("blacklist", "[]")
    await callback.answer("Blacklist очищен")
    await callback.message.edit_text("Blacklist очищен.")


@router.callback_query(F.data == "bl_close")
async def cb_bl_close(callback: CallbackQuery):
    if callback.from_user.id != config.TELEGRAM_OWNER_ID:
        return
    raw = await get_setting("blacklist", "[]")
    try:
        bl = json.loads(raw)
    except json.JSONDecodeError:
        bl = []
    await callback.message.edit_text(f"Blacklist: {len(bl)} записей.")


async def _refresh_bl_manage(message, bl: list):
    """Перерисовать кнопки управления blacklist."""
    known = await get_known_chats(exclude_private=False)
    all_ids = list({c["chat_id"] for c in known} | set(bl))
    chat_names = await resolve_chat_names(all_ids)

    buttons = []
    row = []
    for cid in all_ids[:12]:
        title = chat_names.get(cid, str(cid))
        short = title[:18] if len(title) <= 18 else title[:16] + ".."
        if cid in bl:
            row.append(InlineKeyboardButton(text=f"✅ {short}", callback_data=f"bl_del:{cid}"))
        else:
            row.append(InlineKeyboardButton(text=f"🚫 {short}", callback_data=f"bl_add:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if bl:
        buttons.append([InlineKeyboardButton(text="Очистить blacklist", callback_data="bl_clear")])
    buttons.append([InlineKeyboardButton(text="Закрыть", callback_data="bl_close")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await message.edit_text(
            f"Blacklist: {len(bl)}. 🚫 = заблокировать, ✅ = разблокировать:",
            reply_markup=markup,
        )
    except Exception:
        pass


# ─── Кнопка "Запрос" + свободные сообщения ───────────────────

@router.message(F.text == "Запрос")
@owner_only
async def btn_query(message: Message):
    await message.answer("Что хочешь узнать? Пиши вопрос.")


@router.message(F.photo)
@owner_only
async def handle_photo(message: Message):
    """Обработка фото — Claude Vision."""
    await message.answer("Анализирую изображение...")

    # Скачиваем фото (максимальное разрешение)
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    question = message.caption.strip() if message.caption else "Опиши и проанализируй что на этом изображении. Если это счёт, штраф, документ — выдели ключевые данные (суммы, даты, реквизиты)."

    system_context = await _build_system_context()
    context = await build_context(question) if message.caption else ""

    try:
        answer = await brain.answer_query_with_image(
            question=question,
            image_base64=image_b64,
            media_type="image/jpeg",
            context=context,
            system_context=system_context,
        )
        await send_to_owner(answer)
    except Exception as e:
        logger.error(f"Vision error: {e}", exc_info=True)
        await send_to_owner(f"Не удалось проанализировать изображение: {e}")


@router.message(F.text)
@owner_only
async def handle_free_text(message: Message):
    text = message.text.strip()

    # Текстовые команды переключения режима
    text_lower = text.lower()
    if text_lower in ("переключи на api", "switch to api"):
        await brain.set_mode("api")
        await send_to_owner("Переключено на Claude API. Теперь расходуются токены.\nДля возврата: /mode или напиши \"переключи на CLI\"")
        return

    if text_lower in ("переключи на cli", "switch to cli"):
        await brain.set_mode("cli")
        await send_to_owner("Переключено на Claude CLI (подписка).\nДля возврата: /mode или напиши \"переключи на API\"")
        return

    # Свободный запрос к AI
    await message.answer("Ищу...")

    context = await build_context(text)
    system_context = await _build_system_context()

    answer = await brain.answer_query(text, context, system_context=system_context)
    await send_to_owner(answer)


async def _build_system_context() -> str:
    """Собирает динамический контекст для system prompt AI."""
    parts = []

    # Whitelist с названиями
    raw_wl = await get_setting("whitelist", "[]")
    try:
        wl_ids = json.loads(raw_wl)
    except json.JSONDecodeError:
        wl_ids = []

    # Аккаунты
    if config.TELEGRAM_API_ID_2:
        parts.append(f"Мониторю 2 Telegram-аккаунта: [{config.ACCOUNT_LABEL_1}] и [{config.ACCOUNT_LABEL_2}]. В сводках каждое сообщение помечено аккаунтом.")
    else:
        parts.append(f"Мониторю 1 Telegram-аккаунт: [{config.ACCOUNT_LABEL_1}].")

    if wl_ids:
        chat_names = await resolve_chat_names(wl_ids)
        wl_names = [chat_names.get(cid, str(cid)) for cid in wl_ids]
        parts.append(f"Мониторинг: {len(wl_ids)} групп в whitelist ({', '.join(wl_names)}) + все личные чаты (кроме ботов).")
    else:
        parts.append("Мониторинг: whitelist пуст, только личные чаты (кроме ботов).")

    # Статистика
    stats = await get_db_stats()
    parts.append(f"В памяти: {stats.get('messages', 0)} сообщений, {stats.get('active_tasks', 0)} активных задач.")

    # Свежие ЛС
    since = datetime.now(timezone.utc) - timedelta(hours=12)
    dm_data = await get_dm_summary_data(since)
    if dm_data:
        dm_lines = []
        for d in dm_data[:10]:
            acc = d.get("account", "")
            acc_tag = f" [{acc}]" if acc else ""
            dm_lines.append(f"{d['sender_name']}{acc_tag} ({d['msg_count']} сообщ.)")
        parts.append(f"Свежие ЛС за 12ч: {', '.join(dm_lines)}.")

    parts.append("Ты имеешь полный доступ к базе данных сообщений. Если знаешь ответ из контекста — отвечай уверенно.")

    return "\n".join(parts)


# ─── Запуск бота ─────────────────────────────────────────────

async def start_bot():
    global bot
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    logger.info("Telegram бот запущен")
    await dp.start_polling(bot)


async def stop_bot():
    if bot:
        await bot.session.close()
        logger.info("Telegram бот остановлен")
