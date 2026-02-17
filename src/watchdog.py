import asyncio
import logging
import subprocess
from datetime import datetime, timezone, timedelta

from src import config
from src.db import get_module_health, heartbeat

logger = logging.getLogger("jarvis.watchdog")

# Модули, которые мониторим
MONITORED_MODULES = [
    "telegram_listener",
    "telegram_bot",
    "ai_brain",
    "scheduler",
]

# Сколько пропущенных heartbeat = проблема
MAX_MISSED_HEARTBEATS = 3  # 3 * 5 мин = 15 мин

# Счётчик алертов (модуль → количество отправленных)
_alert_counts: dict[str, int] = {}
# Модули, которые уже были "down" на прошлой проверке
_known_down: set[str] = set()

# Библиотека типичных ошибок и инструкций
ERROR_INSTRUCTIONS = {
    "Session expired": (
        "Telegram попросил переавторизоваться.\n"
        "Без этого чтение чатов не работает.\n\n"
        "ЧТО ДЕЛАТЬ:\n"
        "1. Открой Claude Code (терминал)\n"
        "2. Введи: ssh jarvis@[IP-адрес-сервера]\n"
        "3. Введи: cd /opt/jarvis && python3 reauth_telegram.py\n"
        "4. Telegram пришлёт код в приложение — введи его\n"
        "5. Готово. Listener перезапустится автоматически."
    ),
    "IMAP auth": (
        "Почтовый сервер отклонил авторизацию.\n\n"
        "ЧТО ДЕЛАТЬ:\n"
        "1. Проверь пароль приложения в .env\n"
        "2. Gmail: Настройки Google → Безопасность → Пароли приложений\n"
        "3. Mail.ru: Настройки → Безопасность → Пароли для приложений\n"
        "4. Обнови .env и перезапусти через /admin"
    ),
    "connection refused": (
        "PostgreSQL не принимает подключения.\n\n"
        "ЧТО ДЕЛАТЬ:\n"
        "1. Открой Claude Code\n"
        "2. ssh jarvis@[IP-адрес-сервера]\n"
        "3. sudo systemctl restart postgresql\n"
        "4. Подожди 30 сек, модули переподключатся автоматически"
    ),
    "rate limit": (
        "API вернул ошибку лимита запросов.\n"
        "Подожди 60 секунд, автоповтор сработает.\n"
        "Если повторяется — проверь подписку через /mode."
    ),
    "disk space": (
        "Диск заполнен более чем на 90%.\n\n"
        "ЧТО ДЕЛАТЬ:\n"
        "1. Запусти архивацию через /admin\n"
        "2. Или: ssh jarvis@[IP] → python3 -m src.memory_archiver --force"
    ),
    "timeout": (
        "Claude CLI не ответил вовремя.\n\n"
        "ЧТО ДЕЛАТЬ:\n"
        "1. Проверь подписку: /mode\n"
        "2. Переключи на API: напиши \"переключи на API\"\n"
        "3. Или подожди — возможно временная перегрузка"
    ),
}

# Callback для уведомлений в бот
_notify_callback = None


def set_notify_callback(callback):
    global _notify_callback
    _notify_callback = callback


async def notify_owner(text: str, **kwargs):
    if _notify_callback:
        await _notify_callback(text, **kwargs)


# ─── Основной цикл мониторинга ───────────────────────────────

async def start_watchdog():
    logger.info("Watchdog запущен")
    while True:
        try:
            await _check_all_modules()
            await heartbeat("watchdog")
        except Exception as e:
            logger.error(f"Watchdog ошибка: {e}", exc_info=True)
        await asyncio.sleep(config.HEARTBEAT_INTERVAL_SEC)


async def _check_all_modules():
    health = await get_module_health()
    now = datetime.now(timezone.utc)

    for module_name in MONITORED_MODULES:
        # Ищем последний heartbeat для этого модуля
        module_health = None
        for h in health:
            if h["module"] == module_name:
                module_health = h
                break

        if not module_health:
            # Модуль ещё ни разу не отметился — возможно, только запустился
            continue

        last_ts = module_health.get("timestamp")
        if not last_ts:
            continue

        # Сколько прошло с последнего heartbeat
        elapsed = now - last_ts
        missed = int(elapsed.total_seconds() / config.HEARTBEAT_INTERVAL_SEC)

        if missed >= MAX_MISSED_HEARTBEATS:
            await _handle_module_down(module_name, module_health, missed)
        else:
            # Модуль жив — сбросить счётчик алертов если был down
            if module_name in _known_down:
                _known_down.discard(module_name)
                _alert_counts.pop(module_name, None)
                await notify_owner(f"Модуль {module_name} восстановился.")

            if module_health.get("status") == "error":
                await _handle_module_error(module_name, module_health)


async def _handle_module_down(module_name: str, health_info: dict, missed: int):
    """Модуль не отвечает — уведомление (макс 3 раза, потом молчим до восстановления)."""
    _known_down.add(module_name)
    alert_count = _alert_counts.get(module_name, 0)

    if alert_count >= 3:
        # Уже сообщали 3 раза — молчим до восстановления модуля
        return

    _alert_counts[module_name] = alert_count + 1

    error_text = health_info.get("error", "Неизвестная ошибка")
    instruction = _find_instruction(error_text)

    # Определяем рекомендацию
    if module_name == "telegram_listener":
        action_hint = (
            "Возможные действия:\n"
            "• /admin → Перезапустить весь процесс\n"
            "• Проверить прокси-соединение\n"
            "• Проверить сессию Telethon"
        )
    else:
        action_hint = (
            "Возможные действия:\n"
            "• /admin → Перезапустить весь процесс\n"
            "• ssh на VPS → systemctl restart jarvis"
        )

    await notify_owner(
        f"ПРОБЛЕМА: Модуль {module_name} не отвечает ({missed * 5} мин).\n"
        f"Ошибка: \"{error_text}\"\n\n"
        f"{instruction}\n\n"
        f"{action_hint}\n\n"
        f"Уведомление {alert_count + 1}/3 (больше не повторю до восстановления)."
    )


async def _handle_module_error(module_name: str, health_info: dict):
    """Модуль отметился, но с ошибкой."""
    error = health_info.get("error", "")
    if error:
        logger.warning(f"Модуль {module_name} в состоянии error: {error}")


def _find_instruction(error_text: str) -> str:
    """Поиск инструкции по тексту ошибки."""
    error_lower = error_text.lower() if error_text else ""
    for key, instruction in ERROR_INSTRUCTIONS.items():
        if key.lower() in error_lower:
            return instruction
    return (
        "Инструкция не найдена для этой ошибки.\n"
        "Скопируй текст ошибки и отправь в Claude Code для диагностики."
    )
