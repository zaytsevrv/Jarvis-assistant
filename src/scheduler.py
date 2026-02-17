import asyncio
import json
import logging
from datetime import datetime, date, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src import config
from src.db import (
    get_active_tasks, get_db_stats, get_setting, heartbeat,
    get_messages_since, get_dm_summary_data,
)
from src.ai_brain import brain
from src.confidence_manager import send_batch_review

logger = logging.getLogger("jarvis.scheduler")

scheduler: AsyncIOScheduler = None

# Callback для отправки в бот
_notify_callback = None


def set_notify_callback(callback):
    global _notify_callback
    _notify_callback = callback


async def notify_owner(text: str, **kwargs):
    if _notify_callback:
        await _notify_callback(text, **kwargs)


# ─── Задачи ──────────────────────────────────────────────────

async def morning_briefing():
    """Утренний брифинг с summary по группам и ЛС."""
    try:
        tasks = await get_active_tasks()
        stats = await get_db_stats()

        urgent = [t for t in tasks if t.get("deadline") and t["deadline"].date() == date.today()]
        data = {
            "tasks": [
                {"id": t["id"], "description": t["description"], "deadline": str(t.get("deadline", ""))}
                for t in tasks[:10]
            ],
            "unread_count": 0,
            "deadlines": [
                {"id": t["id"], "description": t["description"], "deadline": str(t["deadline"])}
                for t in urgent
            ],
        }
        briefing = await brain.generate_briefing(data)
        await notify_owner(briefing)

        # Summary по whitelist-группам за последние 12 часов
        since = datetime.now(timezone.utc) - timedelta(hours=12)
        raw_wl = await get_setting("whitelist", "[]")
        try:
            wl_ids = json.loads(raw_wl)
        except json.JSONDecodeError:
            wl_ids = []

        if wl_ids:
            group_msgs = await get_messages_since(since, chat_ids=wl_ids)
            if group_msgs:
                # Группируем по чату
                grouped = {}
                for m in group_msgs:
                    title = m["chat_title"] or str(m["chat_id"])
                    if title not in grouped:
                        grouped[title] = []
                    grouped[title].append(f"{m['sender_name']}: {m['text'][:150]}")

                summary = await brain.generate_group_summary(grouped)
                if summary:
                    await notify_owner(f"📋 ОБЗОР ГРУПП:\n\n{summary}")

        # Summary по ЛС
        dm_data = await get_dm_summary_data(since)
        if dm_data:
            dm_summary = await brain.generate_dm_summary(dm_data)
            if dm_summary:
                await notify_owner(f"💬 ЛИЧНЫЕ СООБЩЕНИЯ:\n\n{dm_summary}")

        logger.info("Утренний брифинг отправлен")
    except Exception as e:
        logger.error(f"Ошибка утреннего брифинга: {e}", exc_info=True)


async def confidence_batch():
    """17:00 Красноярск — батч неуверенных классификаций."""
    try:
        await send_batch_review()
    except Exception as e:
        logger.error(f"Ошибка confidence batch: {e}", exc_info=True)


async def evening_digest():
    """Вечерний дайджест с summary за день."""
    try:
        tasks = await get_active_tasks()
        stats = await get_db_stats()

        # Сообщения за последние 12 часов (с утра)
        since = datetime.now(timezone.utc) - timedelta(hours=12)

        data = {
            "completed": 0,
            "in_progress": len(tasks),
            "new_tasks": 0,
            "messages_count": stats.get("messages", 0),
            "events": [],
        }
        digest = await brain.generate_digest(data)
        system_line = f"\nСИСТЕМА: {stats.get('db_size', '?')} БД"
        await notify_owner(digest + system_line)

        # Summary по whitelist-группам за день
        raw_wl = await get_setting("whitelist", "[]")
        try:
            wl_ids = json.loads(raw_wl)
        except json.JSONDecodeError:
            wl_ids = []

        if wl_ids:
            group_msgs = await get_messages_since(since, chat_ids=wl_ids)
            if group_msgs:
                grouped = {}
                for m in group_msgs:
                    title = m["chat_title"] or str(m["chat_id"])
                    if title not in grouped:
                        grouped[title] = []
                    grouped[title].append(f"{m['sender_name']}: {m['text'][:150]}")

                summary = await brain.generate_group_summary(grouped)
                if summary:
                    await notify_owner(f"📋 ОБЗОР ГРУПП ЗА ДЕНЬ:\n\n{summary}")

        # Summary по ЛС за день
        dm_data = await get_dm_summary_data(since)
        if dm_data:
            dm_summary = await brain.generate_dm_summary(dm_data)
            if dm_summary:
                await notify_owner(f"💬 ЛС ЗА ДЕНЬ:\n\n{dm_summary}")

        logger.info("Вечерний дайджест отправлен")
    except Exception as e:
        logger.error(f"Ошибка вечернего дайджеста: {e}", exc_info=True)


async def check_deadlines():
    """Каждый час — проверка приближающихся дедлайнов."""
    try:
        tasks = await get_active_tasks()
        today = date.today()

        for t in tasks:
            if not t.get("deadline"):
                continue
            days_left = (t["deadline"].date() - today).days
            if days_left == 0:
                await notify_owner(
                    f"ДЕДЛАЙН СЕГОДНЯ: #{t['id']} {t['description']}"
                )
            elif days_left == 1:
                await notify_owner(
                    f"Дедлайн ЗАВТРА: #{t['id']} {t['description']}"
                )
    except Exception as e:
        logger.error(f"Ошибка проверки дедлайнов: {e}", exc_info=True)


async def weekly_analysis():
    """Воскресенье 10:00 — еженедельный анализ."""
    try:
        tasks = await get_active_tasks()
        stats = await get_db_stats()

        text = (
            f"ЕЖЕНЕДЕЛЬНЫЙ АНАЛИЗ\n\n"
            f"Активных задач: {len(tasks)}\n"
            f"Сообщений в БД: {stats.get('messages', 0)}\n"
            f"Размер БД: {stats.get('db_size', '?')}\n\n"
            f"(Полный паттерн-анализ доступен с Фазы 4)"
        )
        await notify_owner(text)
        logger.info("Еженедельный анализ отправлен")
    except Exception as e:
        logger.error(f"Ошибка еженедельного анализа: {e}", exc_info=True)


async def scheduler_heartbeat():
    """Каждые 5 минут — heartbeat."""
    await heartbeat("scheduler")


# ─── Запуск / остановка ──────────────────────────────────────

async def start_scheduler():
    global scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Утренний брифинг — 02:00 UTC = 09:00 Красноярск
    scheduler.add_job(morning_briefing, CronTrigger(hour=config.BRIEFING_HOUR, minute=0))

    # Батч confidence — 10:00 UTC = 17:00 Красноярск
    scheduler.add_job(confidence_batch, CronTrigger(hour=config.CONFIDENCE_BATCH_HOUR, minute=0))

    # Вечерний дайджест — 14:00 UTC = 21:00 Красноярск
    scheduler.add_job(evening_digest, CronTrigger(hour=config.DIGEST_HOUR, minute=0))

    # Проверка дедлайнов — каждый час
    scheduler.add_job(check_deadlines, CronTrigger(minute=30))

    # Еженедельный анализ — воскресенье 03:00 UTC = 10:00 Красноярск
    scheduler.add_job(weekly_analysis, CronTrigger(
        day_of_week=config.WEEKLY_ANALYSIS_DAY,
        hour=config.WEEKLY_ANALYSIS_HOUR,
        minute=0,
    ))

    # Heartbeat — каждые 5 минут
    scheduler.add_job(scheduler_heartbeat, "interval", seconds=config.HEARTBEAT_INTERVAL_SEC)

    scheduler.start()
    logger.info("Scheduler запущен")


async def stop_scheduler():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("Scheduler остановлен")
