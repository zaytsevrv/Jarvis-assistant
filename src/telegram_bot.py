import asyncio
import json
import logging
import subprocess
from datetime import datetime

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
    get_module_health,
    get_setting,
    search_messages,
    set_setting,
    complete_task,
    cancel_task,
)
from src.ai_brain import brain
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
    """Отправка сообщения владельцу с футером."""
    footer = await _mode_footer()
    full_text = text + footer
    # Telegram лимит 4096 символов
    if len(full_text) > 4096:
        full_text = full_text[:4090] + "..."
    await bot.send_message(
        config.TELEGRAM_OWNER_ID,
        full_text,
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
        "/admin     — управление: перезапуск, логи, бэкап\n"
        "/mode      — AI-режим (CLI/API), переключение\n"
        "/settings  — настройки (лимиты, whitelist, расписание)\n"
        "/help      — эта справка\n\n"
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

    lines = [f"Статус ({datetime.now().strftime('%H:%M')} МСК):\n"]

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
        f"Confidence батч: {batch_hour}:00 МСК\n"
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

    # /whitelist — показать список
    if len(args) < 2:
        if not wl:
            await send_to_owner(
                "Whitelist пуст — listener не мониторит ни один чат.\n\n"
                "Добавить: /whitelist add <chat_id>\n"
                "Удалить: /whitelist del <chat_id>\n"
                "Очистить: /whitelist clear\n\n"
                "Узнать chat_id: перешли сообщение из чата боту @getmyid_bot"
            )
        else:
            lines = [f"Whitelist ({len(wl)} чатов):"]
            for cid in wl:
                lines.append(f"  • {cid}")
            lines.append(f"\nУдалить: /whitelist del <chat_id>")
            await send_to_owner("\n".join(lines))
        return

    subcmd = args[1].strip()

    # /whitelist clear
    if subcmd == "clear":
        await set_setting("whitelist", "[]")
        await send_to_owner("Whitelist очищен. Listener больше ничего не мониторит.")
        return

    # /whitelist add <id> или /whitelist del <id>
    parts = subcmd.split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in ("add", "del"):
        await send_to_owner("Формат: /whitelist add <chat_id> или /whitelist del <chat_id>")
        return

    action = parts[0]
    # Поддержка нескольких ID через пробел или запятую
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


# ─── Кнопка "Запрос" + свободные сообщения ───────────────────

@router.message(F.text == "Запрос")
@owner_only
async def btn_query(message: Message):
    await message.answer("Что хочешь узнать? Пиши вопрос.")


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

    # Сборка контекста: FTS + поиск по именам + активные задачи
    context = await build_context(text)

    answer = await brain.answer_query(text, context)
    await send_to_owner(answer)


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
