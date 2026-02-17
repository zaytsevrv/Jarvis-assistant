import asyncio
import json
import logging
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.types import (
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
)
from src.ai_brain import brain

logger = logging.getLogger("jarvis.listener")

# Telegram-клиент (Telethon)
client: TelegramClient = None

# Callback для уведомлений в бот (устанавливается из main.py)
_notify_callback = None
_classify_callback = None


def set_notify_callback(callback):
    global _notify_callback
    _notify_callback = callback


def set_classify_callback(callback):
    global _classify_callback
    _classify_callback = callback


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


async def start_listener():
    global client

    proxy = _build_proxy()

    client = TelegramClient(
        "jarvis_session",
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
        proxy=proxy,
    )
    await client.start(phone=config.TELEGRAM_PHONE)
    proxy_label = f" через proxy ({config.PROXY_TYPE})" if proxy else ""
    logger.info(f"Telethon: подключён{proxy_label}")

    # Регистрация обработчика
    client.add_event_handler(on_new_message, events.NewMessage)

    # Heartbeat
    asyncio.create_task(_heartbeat_loop())

    # Держим клиент запущенным
    await client.run_until_disconnected()


async def stop_listener():
    global client
    if client:
        await client.disconnect()
        client = None
        logger.info("Telethon: отключён")


# ─── Обработка сообщений ─────────────────────────────────────

def _is_private_chat(msg) -> bool:
    """Проверяет, является ли сообщение из личного чата (ЛС)."""
    return isinstance(msg.peer_id, PeerUser)


async def on_new_message(event):
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
        )

        if db_msg_id is None:
            return  # Дубликат — пропускаем

        # --- AI-классификация для whitelist-чатов и личных чатов ---

        # Проверка: новый контакт? (только для whitelist)
        if in_whitelist and sender_id and sender_id != config.TELEGRAM_OWNER_ID:
            is_known = await is_known_contact(sender_id)
            if not is_known:
                contact = await get_or_create_contact(sender_id, sender_name)
                preview = text[:100] if text else "[медиа]"
                await notify_owner(
                    f"Новый контакт: {sender_name}\n"
                    f"Первое сообщение: \"{preview}\"\n"
                    f"Чат: {chat_title}",
                )

        # Классификация AI — для whitelist и личных чатов
        if text and len(text) > 5:
            if _classify_callback:
                asyncio.create_task(
                    _classify_callback(db_msg_id, text, sender_name, chat_title, chat_id)
                )

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


async def _get_whitelist() -> set:
    raw = await get_setting("whitelist", "[]")
    try:
        ids = json.loads(raw)
        return set(ids)
    except json.JSONDecodeError:
        return set()


async def _get_blacklist() -> set:
    raw = await get_setting("blacklist", "[]")
    try:
        ids = json.loads(raw)
        return set(ids)
    except json.JSONDecodeError:
        return set()


async def _heartbeat_loop():
    while True:
        try:
            await heartbeat("telegram_listener")
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
        await asyncio.sleep(config.HEARTBEAT_INTERVAL_SEC)
