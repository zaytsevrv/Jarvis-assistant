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

    # 1. База данных
    logger.info("Инициализация PostgreSQL...")
    await init_pool()
    await create_tables()
    logger.info("PostgreSQL OK")

    # 2. Привязка callbacks (все модули уведомляют через бот)
    listener_set_notify(notify_callback)
    set_classify_callback(process_classification)
    confidence_set_notify(notify_callback)
    scheduler_set_notify(notify_callback)
    watchdog_set_notify(notify_callback)

    # 3. Запуск модулей параллельно
    tasks = [
        asyncio.create_task(start_bot(), name="telegram_bot"),
        asyncio.create_task(start_listener(), name="telegram_listener"),
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


async def shutdown():
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
