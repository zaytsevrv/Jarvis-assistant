import asyncio
import logging
import signal
import sys
from pathlib import Path

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config
from src.db import init_pool, close_pool, create_tables
from src.telegram_bot import start_bot, stop_bot, notify_callback
from src.telegram_listener import (
    start_listener,
    stop_listener,
    set_notify_callback as listener_set_notify,
    set_classify_callback,
    set_bot_id,
)
from src.confidence_manager import (
    process_classification,
    set_notify_callback as confidence_set_notify,
)
from src.scheduler import (
    start_scheduler,
    stop_scheduler,
    set_notify_callback as scheduler_set_notify,
)
from src.watchdog import (
    start_watchdog,
    set_notify_callback as watchdog_set_notify,
)


# ─── Логирование ─────────────────────────────────────────────

def setup_logging():
    log_format = "%(asctime)s | %(name)-25s | %(levelname)-5s | %(message)s"
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Подавляем шум от библиотек
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


logger = logging.getLogger("jarvis.main")


# ─── Запуск ──────────────────────────────────────────────────

async def main():
    setup_logging()
    logger.info("=" * 50)
    logger.info("JARVIS запускается...")
    logger.info("=" * 50)

    # 0. Валидация конфигурации (fail fast)
    config.validate_config()
    logger.info("Конфигурация OK")

    # 1. База данных
    logger.info("Инициализация PostgreSQL...")
    await init_pool()
    await create_tables()
    logger.info("PostgreSQL OK")

    # 2. Привязка callbacks (все модули уведомляют через бот)
    listener_set_notify(notify_callback)
    set_classify_callback(process_classification)
    # A2: Передаём bot_id чтобы listener не классифицировал диалог с ботом
    try:
        bot_id = int(config.TELEGRAM_BOT_TOKEN.split(":")[0])
        set_bot_id(bot_id)
    except (ValueError, IndexError):
        logger.warning("Не удалось извлечь bot_id из TELEGRAM_BOT_TOKEN")
    confidence_set_notify(notify_callback)
    scheduler_set_notify(notify_callback)
    watchdog_set_notify(notify_callback)

    # 3. Запуск модулей параллельно
    tasks = [
        asyncio.create_task(start_bot(), name="telegram_bot"),
        asyncio.create_task(_resilient_listener(), name="telegram_listener"),
        asyncio.create_task(start_watchdog(), name="watchdog"),
    ]

    # Scheduler запускается синхронно (APScheduler внутренне async)
    await start_scheduler()

    logger.info("Все модули запущены")

    # 4. Ожидание завершения (Ctrl+C)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Получен сигнал остановки")


async def _resilient_listener():
    """Wrapper: перезапускает Telethon при крашах, не убивая бот и scheduler.
    Уведомления: одно при падении, одно при восстановлении. Без спама."""
    retry_delay = 30
    max_delay = 300  # 5 мин максимум
    notified_down = False

    while not _shutting_down:
        try:
            # Если это retry после падения — ставим флаг для уведомления
            from src.telegram_listener import set_recovery_flag
            if notified_down:
                set_recovery_flag()
            await start_listener()
            break  # Нормальное завершение (shutdown)
        except Exception as e:
            logger.error(f"Telethon crashed: {e}")
            # Одно уведомление при первом падении
            if not notified_down:
                notified_down = True
                try:
                    await notify_callback(
                        "⚠️ <b>Telethon отключён</b>\n"
                        "Мониторинг чатов не работает. Бот отвечает, но не видит новые сообщения.\n"
                        "Буду пробовать переподключиться автоматически."
                    )
                except Exception:
                    pass
            # Чистим клиенты
            try:
                await stop_listener()
            except Exception:
                pass
            # Ждём перед ретраем (exponential backoff)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)
            logger.info("Telethon: попытка переподключения...")


_shutting_down = False


async def shutdown():
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.info("Останавливаю JARVIS...")
    await stop_scheduler()
    await stop_listener()
    await stop_bot()
    await close_pool()
    logger.info("JARVIS остановлен")


def handle_signal(sig, frame):
    logger.info(f"Сигнал {sig}, останавливаю...")
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown())


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
    finally:
        loop.run_until_complete(shutdown())
        loop.close()
