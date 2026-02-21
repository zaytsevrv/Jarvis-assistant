import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.types import (
    Channel,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageService,
    PeerChannel,
    PeerChat,
    PeerUser,
)

from src import config
from src.db import (
    get_setting,
    heartbeat,
    is_known_contact,
    get_or_create_contact,
    save_message,
    mark_message_processed,
    get_tracked_tasks_for_chat,
)
from src.ai_brain import brain

logger = logging.getLogger("jarvis.listener")

# Список Telegram-клиентов (Telethon) — один или два аккаунта
_clients: list[TelegramClient] = []

# Callback для уведомлений в бот (устанавливается из main.py)
_notify_callback = None
_classify_callback = None

# ID бота — определяется при старте, используется для фильтрации
_bot_id: int = 0

# Флаг: Telethon восстанавливается после падения
_recovering: bool = False


def set_notify_callback(callback):
    global _notify_callback
    _notify_callback = callback


def set_classify_callback(callback):
    global _classify_callback
    _classify_callback = callback


def set_bot_id(bot_id: int):
    """Устанавливает ID бота для фильтрации classify pipeline."""
    global _bot_id
    _bot_id = bot_id


def set_recovery_flag():
    """Ставит флаг: при следующем успешном подключении — уведомить владельца."""
    global _recovering
    _recovering = True


async def notify_owner(text: str, **kwargs):
    if _notify_callback:
        await _notify_callback(text, **kwargs)


# ─── Инициализация ───────────────────────────────────────────

def _build_proxy():
    """Строит параметры прокси из конфигурации."""
    if not config.PROXY_TYPE or not config.PROXY_HOST:
        return None

    proxy_type = config.PROXY_TYPE.lower()
    try:
        import socks
        type_map = {
            "socks5": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
        }
        sock_type = type_map.get(proxy_type)
        if not sock_type:
            logger.warning(f"Неизвестный тип прокси: {proxy_type}. Подключение без прокси.")
            return None

        proxy = (sock_type, config.PROXY_HOST, config.PROXY_PORT,
                 True, config.PROXY_USERNAME, config.PROXY_PASSWORD)
        logger.info(f"Прокси настроен: {proxy_type}://{config.PROXY_HOST}:{config.PROXY_PORT}")
        return proxy
    except ImportError:
        logger.warning("PySocks не установлен (pip install PySocks). Подключение без прокси.")
        return None


def _make_handler(account_label: str):
    """Создаёт обработчик сообщений с привязкой к конкретному аккаунту."""
    async def handler(event):
        await on_new_message(event, account_label)
    return handler


async def start_listener():
    """Запускает Telethon-клиенты для всех настроенных аккаунтов."""
    global _clients
    _clients = []

    proxy = _build_proxy()

    # Список аккаунтов для подключения
    accounts = [
        {
            "session": "jarvis_session",
            "api_id": config.TELEGRAM_API_ID,
            "api_hash": config.TELEGRAM_API_HASH,
            "phone": config.TELEGRAM_PHONE,
            "label": config.ACCOUNT_LABEL_1,
        },
    ]

    # Второй аккаунт — если настроен
    if config.TELEGRAM_API_ID_2 and config.TELEGRAM_PHONE_2:
        accounts.append({
            "session": "jarvis_session_2",
            "api_id": config.TELEGRAM_API_ID_2,
            "api_hash": config.TELEGRAM_API_HASH_2,
            "phone": config.TELEGRAM_PHONE_2,
            "label": config.ACCOUNT_LABEL_2,
        })

    for acc in accounts:
        client = TelegramClient(
            acc["session"],
            acc["api_id"],
            acc["api_hash"],
            proxy=proxy,
        )
        await client.start(phone=acc["phone"])
        client.add_event_handler(
            _make_handler(acc["label"]),
            events.NewMessage,
        )
        _clients.append(client)
        logger.info(f"Telethon: аккаунт [{acc['label']}] подключён")

    # Уведомление о восстановлении после падения
    global _recovering
    if _recovering:
        _recovering = False
        await notify_owner("✅ <b>Telethon восстановлен</b>\nМониторинг чатов снова работает.")

    # Heartbeat
    asyncio.create_task(_heartbeat_loop())

    # Держим все клиенты запущенными
    run_tasks = [c.run_until_disconnected() for c in _clients]
    await asyncio.gather(*run_tasks)


# Кеш названий чатов (B4: TTL 5 мин)
_chat_names_cache: dict[int, str] = {}
_chat_names_cache_time: float = 0
_CHAT_NAMES_TTL = 300  # 5 минут


async def resolve_chat_names(chat_ids: list[int]) -> dict[int, str]:
    """Получает названия чатов/групп через Telethon по их ID.
    Пробует все подключённые клиенты (группа может быть только в одном аккаунте).
    Возвращает {chat_id: title}. Кеширует на 5 мин."""
    global _chat_names_cache, _chat_names_cache_time
    now = time.monotonic()

    # Если кеш свежий — ищем сначала в нём
    cache_valid = (now - _chat_names_cache_time) < _CHAT_NAMES_TTL

    result = {}
    missing = []
    for cid in chat_ids:
        if cache_valid and cid in _chat_names_cache:
            result[cid] = _chat_names_cache[cid]
        else:
            missing.append(cid)

    if not missing or not _clients:
        return result

    # Запрашиваем только недостающие
    for cid in missing:
        for client in _clients:
            try:
                entity = await client.get_entity(cid)
                title = getattr(entity, "title", None)
                if not title:
                    first = getattr(entity, "first_name", "") or ""
                    last = getattr(entity, "last_name", "") or ""
                    title = f"{first} {last}".strip() or getattr(entity, "username", "") or str(cid)
                result[cid] = title
                _chat_names_cache[cid] = title
                break
            except Exception:
                continue

    _chat_names_cache_time = now
    return result


async def stop_listener():
    global _clients
    for client in _clients:
        try:
            await client.disconnect()
        except Exception as e:
            logger.error(f"Ошибка отключения клиента: {e}")
    count = len(_clients)
    _clients = []
    logger.info(f"Telethon: отключено {count} аккаунт(ов)")


# ─── Обработка сообщений ─────────────────────────────────────

def _is_private_chat(msg) -> bool:
    """Проверяет, является ли сообщение из личного чата (ЛС)."""
    return isinstance(msg.peer_id, PeerUser)


async def on_new_message(event, account_label: str = ""):
    try:
        msg = event.message

        # Пропуск сервисных сообщений
        if isinstance(msg, MessageService):
            return

        # Пропуск стикеров, GIF, пересланных из каналов
        if _should_ignore(msg):
            return

        chat_id = msg.chat_id or 0
        is_private = _is_private_chat(msg)
        whitelist = await _get_whitelist()
        in_whitelist = chat_id in whitelist

        # Три режима:
        # 1. Whitelist-чаты → сохранять + AI-классификация
        # 2. Личные чаты (ЛС) → тихо сохранять в БД
        # 3. Остальные группы → игнорировать
        if not in_whitelist and not is_private:
            return  # Группа не из whitelist — игнорируем

        # Получаем информацию об отправителе
        sender = await msg.get_sender()
        chat = await msg.get_chat()

        sender_id = sender.id if sender else 0
        sender_name = _get_display_name(sender)
        chat_title = _get_chat_title(chat)

        # Фильтр ботов — не сохраняем сообщения от ботов в ЛС
        if is_private and getattr(sender, 'bot', False):
            return

        # Фильтр blacklist — полностью игнорируем
        blacklist = await _get_blacklist()
        if chat_id in blacklist or sender_id in blacklist:
            return

        # Определяем тип медиа
        media_type = _get_media_type(msg)

        # Текст сообщения
        text = msg.text or ""
        if not text and media_type:
            text = f"[{media_type}]"

        # Пропуск пустых сообщений
        if not text:
            return

        # B2: Vision для фото в ЛС — описываем содержимое через Haiku
        # Только личные чаты (там идёт классификация + task tracking)
        if msg.photo and is_private and not msg.text:
            try:
                image_bytes = await event.client.download_media(msg, bytes)
                if image_bytes and len(image_bytes) < 5 * 1024 * 1024:
                    description = await brain.analyze_image(image_bytes)
                    if description:
                        text = f"[photo: {description}]"
                        logger.debug(f"B2: фото Vision: {description[:80]}")
            except Exception as e:
                logger.warning(f"B2: ошибка Vision для фото: {e}")

        # Сохраняем в БД. None = дубликат
        db_msg_id = await save_message(
            telegram_msg_id=msg.id,
            chat_id=chat_id,
            chat_title=chat_title,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            media_type=media_type,
            timestamp=msg.date or datetime.now(timezone.utc),
            account=account_label,
        )

        if db_msg_id is None:
            return  # Дубликат — пропускаем

        # --- AI-классификация для whitelist-чатов и личных чатов ---

        # A2: НЕ классифицировать сообщения из чата с ботом —
        # диалог owner↔bot обрабатывается через handle_free_text с tools
        is_bot_chat = (is_private and (sender_id == _bot_id or chat_id == _bot_id))

        # v4: Определяем тип sender — канал (Channel) не является контактом
        is_channel = isinstance(sender, Channel)

        # Проверка: новый контакт? (только для whitelist, не бот, не канал, не владелец)
        if (in_whitelist and sender_id and not config.is_owner(sender_id)
                and not is_bot_chat and not is_channel):
            is_known = await is_known_contact(sender_id)
            if not is_known:
                contact = await get_or_create_contact(sender_id, sender_name)
                preview = text[:100] if text else "[медиа]"
                await notify_owner(
                    f"Новый контакт: {sender_name}\n"
                    f"Первое сообщение: \"{preview}\"\n"
                    f"Чат: {chat_title}",
                )

        # v4: Каналы из whitelist — сохраняем для дайджеста, но НЕ классифицируем
        if is_channel:
            await mark_message_processed(db_msg_id)
            return

        # v6: Классификация ЛС — AI анализирует сообщения и создаёт задачи
        # Whitelist-группы — БЕЗ классификации (только дайджест)
        # Классифицируем И входящие, И исходящие — classify_message разбирает direction
        # через owner_is_sender. Но tracked tasks проверяем ТОЛЬКО для входящих (не-владелец).
        if is_private and not is_bot_chat and len(text) > 5:
            await _classify_and_mark(
                db_msg_id, text, sender_name, chat_title, chat_id,
                sender_id=sender_id, account_label=account_label,
            )
            # v6: Event-driven проверка tracked tasks (если sender НЕ владелец)
            if not config.is_owner(sender_id):
                await _check_response_to_tracked(chat_id)
        else:
            await mark_message_processed(db_msg_id)

    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)


# ─── Утилиты ─────────────────────────────────────────────────

def _should_ignore(msg) -> bool:
    # Стикеры
    if msg.sticker:
        return True
    # GIF (документ с mime=video/mp4 и animated)
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        if doc and hasattr(doc, "mime_type"):
            if doc.mime_type == "video/mp4" and not msg.text:
                return True
    # Пересланные посты из каналов (fwd_from + channel)
    if msg.fwd_from and msg.fwd_from.from_id and isinstance(msg.fwd_from.from_id, PeerChannel):
        return True
    return False


def _get_display_name(entity) -> str:
    if not entity:
        return "Unknown"
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    if not name:
        name = getattr(entity, "title", "") or getattr(entity, "username", "") or "Unknown"
    return name


def _get_chat_title(chat) -> str:
    if hasattr(chat, "title") and chat.title:
        return chat.title
    return _get_display_name(chat)


def _get_media_type(msg) -> str | None:
    if msg.photo:
        return "photo"
    if msg.voice:
        return "voice"
    if msg.video_note:
        return "video_note"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.audio:
        return "audio"
    return None


# v6: Debounce для event-driven проверки tracked tasks
_last_track_check: dict[int, float] = {}  # {chat_id: monotonic_time}
_TRACK_CHECK_DEBOUNCE = 60  # секунд


async def _check_response_to_tracked(chat_id: int):
    """v6: Проверяет tracked tasks если в чате пришёл новый ответ. Debounce 60с."""
    now = time.monotonic()
    last = _last_track_check.get(chat_id, 0)
    if now - last < _TRACK_CHECK_DEBOUNCE:
        return
    _last_track_check[chat_id] = now

    tracked = await get_tracked_tasks_for_chat(chat_id)
    if not tracked:
        return

    from src.scheduler import check_tracked_task_single
    for task in tracked:
        try:
            await check_tracked_task_single(task)
        except Exception as e:
            logger.error(f"Event-driven check error task #{task['id']}: {e}", exc_info=True)


async def _classify_and_mark(db_msg_id, text, sender_name, chat_title, chat_id,
                             sender_id=0, account_label=""):
    """Классифицирует сообщение, затем помечает как обработанное (A9).
    v4: передаёт sender_id и account_label для контекста задач."""
    try:
        if _classify_callback:
            await _classify_callback(
                db_msg_id, text, sender_name, chat_title, chat_id,
                sender_id=sender_id, account_label=account_label,
            )
    except Exception as e:
        logger.error(f"Ошибка классификации #{db_msg_id}: {e}", exc_info=True)
    finally:
        await mark_message_processed(db_msg_id)


# ─── Кеш whitelist/blacklist (B3: TTL 60 сек) ────────────────

_wl_cache: set = set()
_wl_cache_time: float = 0
_bl_cache: set = set()
_bl_cache_time: float = 0
_CACHE_TTL = 60  # секунд


async def _get_whitelist() -> set:
    global _wl_cache, _wl_cache_time
    now = time.monotonic()
    if now - _wl_cache_time < _CACHE_TTL:
        return _wl_cache
    raw = await get_setting("whitelist", "[]")
    try:
        _wl_cache = set(json.loads(raw))
    except json.JSONDecodeError:
        _wl_cache = set()
    _wl_cache_time = now
    return _wl_cache


async def _get_blacklist() -> set:
    global _bl_cache, _bl_cache_time
    now = time.monotonic()
    if now - _bl_cache_time < _CACHE_TTL:
        return _bl_cache
    raw = await get_setting("blacklist", "[]")
    try:
        _bl_cache = set(json.loads(raw))
    except json.JSONDecodeError:
        _bl_cache = set()
    _bl_cache_time = now
    return _bl_cache


async def _heartbeat_loop():
    while True:
        try:
            await heartbeat("telegram_listener")
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
        await asyncio.sleep(config.HEARTBEAT_INTERVAL_SEC)
