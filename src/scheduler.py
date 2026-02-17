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
    get_tasks_completed_since, get_tasks_created_since,
    cleanup_conversation_history,
)
from src.ai_brain import brain
from src.confidence_manager import send_batch_review

logger = logging.getLogger("jarvis.scheduler")

scheduler: AsyncIOScheduler = None

# Callback для отправки в бот
_notify_callback = None

# A6: Трекинг отправленных уведомлений о дедлайнах
_deadline_notified: dict[int, int] = {}  # task_id → количество уведомлений за сегодня
_deadline_notified_date: date = None


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

        # A10: Реальные данные за последние 12 часов
        since = datetime.now(timezone.utc) - timedelta(hours=12)
        completed_count = await get_tasks_completed_since(since)
        new_count = await get_tasks_created_since(since)

        data = {
            "completed": completed_count,
            "in_progress": len(tasks),
            "new_tasks": new_count,
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
    """Каждый час — проверка приближающихся дедлайнов.
    A6: Дедупликация — max 1 раз "завтра", max 2 раза "сегодня"."""
    global _deadline_notified, _deadline_notified_date
    try:
        today = date.today()
        # Сброс счётчика при новом дне
        if _deadline_notified_date != today:
            _deadline_notified = {}
            _deadline_notified_date = today

        tasks = await get_active_tasks()

        for t in tasks:
            if not t.get("deadline"):
                continue
            task_id = t["id"]
            days_left = (t["deadline"].date() - today).days
            sent_count = _deadline_notified.get(task_id, 0)

            if days_left == 0 and sent_count < 2:
                await notify_owner(
                    f"ДЕДЛАЙН СЕГОДНЯ: #{task_id} {t['description']}"
                )
                _deadline_notified[task_id] = sent_count + 1
            elif days_left == 1 and sent_count < 1:
                await notify_owner(
                    f"Дедлайн ЗАВТРА: #{task_id} {t['description']}"
                )
                _deadline_notified[task_id] = sent_count + 1
    except Exception as e:
        logger.error(f"Ошибка проверки дедлайнов: {e}", exc_info=True)


async def weekly_analysis():
    """Воскресенье 10:00 — еженедельный анализ."""
    try:
        tasks = await get_active_tasks()
        stats = await get_db_stats()

        # Статистика за неделю
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        completed_week = await get_tasks_completed_since(week_ago)
        created_week = await get_tasks_created_since(week_ago)

        # Топ отправителей за неделю
        messages_week = await get_messages_since(week_ago, limit=1000)
        sender_counts = {}
        for m in messages_week:
            name = m.get("sender_name", "?")
            sender_counts[name] = sender_counts.get(name, 0) + 1
        top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_str = "\n".join(f"  {name}: {count} сообщ." for name, count in top_senders)

        text = (
            f"ЕЖЕНЕДЕЛЬНЫЙ АНАЛИЗ\n\n"
            f"Активных задач: {len(tasks)}\n"
            f"Создано за неделю: {created_week}\n"
            f"Закрыто за неделю: {completed_week}\n"
            f"Сообщений в БД: {stats.get('messages', 0)}\n"
            f"Размер БД: {stats.get('db_size', '?')}\n\n"
            f"Топ отправителей за неделю:\n{top_str}"
        )
        await notify_owner(text)
        logger.info("Еженедельный анализ отправлен")
    except Exception as e:
        logger.error(f"Ошибка еженедельного анализа: {e}", exc_info=True)


async def cleanup_old_conversations():
    """Каждый час — очистка старой истории диалога."""
    try:
        await cleanup_conversation_history(max_age_hours=4)
    except Exception as e:
        logger.error(f"Ошибка очистки conversation_history: {e}", exc_info=True)


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

    # Очистка старой истории диалога — каждый час
    scheduler.add_job(cleanup_old_conversations, CronTrigger(minute=15))

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
