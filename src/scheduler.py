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
    get_timed_reminders, mark_reminder_sent,
    save_deadline_notification, get_deadline_notification_count,
    get_tracked_tasks_to_check, get_recent_chat_messages,
    update_task_last_checked, build_message_link,
)
from src.ai_brain import brain
from src.confidence_manager import send_batch_review

logger = logging.getLogger("jarvis.scheduler")

scheduler: AsyncIOScheduler = None

# Callback для отправки в бот
_notify_callback = None

# K4: Трекинг дедлайн-уведомлений теперь в БД (deadline_notifications), не в памяти


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

            # B4: кросс-референс ЛС с активными задачами
            tasks_with_who = [t for t in tasks if t.get("who")]
            if tasks_with_who:
                try:
                    cross_ref = await brain.generate_cross_reference(dm_data, tasks_with_who)
                    if cross_ref:
                        await notify_owner(f"🔗 <b>СВЯЗИ ЛС ↔ ЗАДАЧИ:</b>\n{cross_ref}")
                except Exception as e:
                    logger.error(f"B4 cross-reference error: {e}", exc_info=True)

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
    """Вечерний дайджест с summary за день + review задач с дедлайном сегодня."""
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

        # v4: Вечерний review — ВСЕ активные задачи с кнопками
        if tasks:
            today = date.today()
            lines = ["📋 <b>АКТИВНЫЕ ЗАДАЧИ — REVIEW:</b>"]
            for t in tasks[:15]:
                who_str = f" [{t['who']}]" if t.get("who") else ""
                deadline_str = ""
                if t.get("deadline"):
                    if t["deadline"].date() < today:
                        deadline_str = f" ⚠️ просрочена ({t['deadline'].strftime('%d.%m')})"
                    elif t["deadline"].date() == today:
                        deadline_str = " 📅 сегодня"
                    else:
                        deadline_str = f" 📅 {t['deadline'].strftime('%d.%m')}"
                lines.append(f"  • #{t['id']} {t['description']}{who_str}{deadline_str}")
            await notify_owner(
                "\n".join(lines),
                reply_markup_type="evening_review",
                review_task_ids=[t["id"] for t in tasks[:10]],
            )

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

            # B4: кросс-референс ЛС с активными задачами
            tasks_with_who = [t for t in tasks if t.get("who")]
            if tasks_with_who:
                try:
                    cross_ref = await brain.generate_cross_reference(dm_data, tasks_with_who)
                    if cross_ref:
                        await notify_owner(f"🔗 <b>СВЯЗИ ЛС ↔ ЗАДАЧИ:</b>\n{cross_ref}")
                except Exception as e:
                    logger.error(f"B4 cross-reference error: {e}", exc_info=True)

        logger.info("Вечерний дайджест отправлен")
    except Exception as e:
        logger.error(f"Ошибка вечернего дайджеста: {e}", exc_info=True)


async def check_timed_reminders():
    """K1: Каждую минуту — проверка задач с remind_at <= NOW()."""
    try:
        tasks = await get_timed_reminders()
        for t in tasks:
            task_id = t["id"]
            description = t["description"]
            remind_at = t["remind_at"]
            who = t.get("who") or ""
            deadline = t.get("deadline")

            # Форматируем уведомление
            lines = [f"⏰ <b>Напоминание:</b> #{task_id} {description}"]
            if who:
                lines.append(f"👤 {who}")
            if deadline:
                lines.append(f"📅 Дедлайн: {deadline.strftime('%d.%m.%Y')}")

            # Deep link на исходное сообщение
            chat_id = t.get("chat_id") or 0
            orig_msg_id = t.get("telegram_msg_id") or t.get("orig_tg_msg_id") or 0
            link = build_message_link(chat_id, orig_msg_id)
            if link:
                lines.append(f'<a href="{link}">📎</a>')

            await notify_owner(
                "\n".join(lines),
                reply_markup_type="reminder",
                task_id=task_id,
            )
            await mark_reminder_sent(task_id)
            logger.info(f"Напоминание отправлено: #{task_id} '{description[:40]}'")
    except Exception as e:
        logger.error(f"Ошибка check_timed_reminders: {e}", exc_info=True)


async def check_tracked_task_single(task: dict):
    """v6: Проверка одной tracked-задачи. Вызывается из scheduler и listener (event-driven)."""
    task_id = task["id"]
    chat_id = task.get("chat_id")
    if not chat_id:
        await update_task_last_checked(task_id)
        return

    # Загружаем сообщения из чата за check_interval_days
    interval = task.get("check_interval_days") or 3
    since = datetime.now(timezone.utc) - timedelta(days=interval)
    chat_msgs = await get_recent_chat_messages(chat_id, since, limit=30)

    chat_title = task.get("source", "").replace("telegram:", "") or f"чат {chat_id}"
    result = await brain.check_task_completion(task, chat_msgs, chat_title)
    status = result["status"]
    evidence = result.get("evidence", "")

    assignee = task.get("sender_name") or task.get("who") or "?"
    desc = task["description"]

    # Deep link
    link = build_message_link(chat_id, task.get("telegram_msg_id") or task.get("orig_tg_msg_id") or 0)
    link_html = f' <a href="{link}">📎</a>' if link else ""

    if status == "completed":
        await notify_owner(
            f"✅ Задача #{task_id} для {assignee}: {desc}{link_html}\n"
            f"Похоже, выполнена: {evidence}",
            reply_markup_type="track_completed",
            task_id=task_id,
        )
    elif status == "not_completed":
        await notify_owner(
            f"⏳ Задача #{task_id} для {assignee}: {desc}{link_html}\n"
            f"Ответа нет.",
            reply_markup_type="track_pending",
            task_id=task_id,
        )
    else:  # unclear
        await notify_owner(
            f"❓ Задача #{task_id} для {assignee}: {desc}{link_html}\n"
            f"Есть активность, но непонятно: {evidence}",
            reply_markup_type="track_pending",
            task_id=task_id,
        )

    await update_task_last_checked(task_id)


async def check_tracked_tasks():
    """v6: Проверка исходящих задач (track_completion=TRUE).
    4×/день: 09:00, 13:00, 17:00, 21:00 Красноярск."""
    try:
        tasks = await get_tracked_tasks_to_check()
        if not tasks:
            logger.info("Мониторинг задач: нечего проверять")
            return

        checked = 0
        for task in tasks:
            try:
                await check_tracked_task_single(task)
                checked += 1
            except Exception as e:
                logger.error(f"Ошибка проверки задачи #{task.get('id')}: {e}", exc_info=True)

        logger.info(f"Мониторинг задач: проверено {checked}/{len(tasks)}")
    except Exception as e:
        logger.error(f"Ошибка check_tracked_tasks: {e}", exc_info=True)


async def check_deadlines():
    """Дневная проверка дедлайнов — 14:00 Красноярск (07:00 UTC).
    Показывает ВСЕ активные задачи с deadline=сегодня + кнопки ✅/➡️.
    Утренние дедлайны — в briefing (09:00), завтрашние — в evening review (21:00)."""
    try:
        today = date.today()
        tasks = await get_active_tasks()

        today_tasks = [t for t in tasks if t.get("deadline") and t["deadline"].date() == today]

        if not today_tasks:
            return  # Нечего уведомлять

        lines = ["⏰ <b>Дедлайны СЕГОДНЯ:</b>"]
        for t in today_tasks:
            who_str = f" [{t['who']}]" if t.get("who") else ""
            # Deep link
            chat_id = t.get("chat_id") or 0
            orig_msg_id = t.get("telegram_msg_id") or t.get("orig_tg_msg_id") or 0
            link = build_message_link(chat_id, orig_msg_id)
            link_html = f' <a href="{link}">📎</a>' if link else ""
            lines.append(f"  • #{t['id']} {t['description']}{who_str}{link_html}")

        await notify_owner(
            "\n".join(lines),
            reply_markup_type="evening_review",
            review_task_ids=[t["id"] for t in today_tasks[:10]],
        )
        logger.info(f"Дневные дедлайны: {len(today_tasks)} задач на сегодня")
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
        await cleanup_conversation_history(max_age_hours=24)
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

    # K1: Проверка time-based напоминаний — каждую минуту
    scheduler.add_job(check_timed_reminders, CronTrigger(minute="*"))

    # v6: Мониторинг исходящих задач — 4×/день (09:05, 13:05, 17:05, 21:05 Красноярск)
    # minute=5 чтобы не пересекаться с briefing (02:00) и digest (14:00)
    scheduler.add_job(check_tracked_tasks, CronTrigger(hour='2,6,10,14', minute=5))

    # Дневная проверка дедлайнов — 07:00 UTC = 14:00 Красноярск
    scheduler.add_job(check_deadlines, CronTrigger(hour=7, minute=0))

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
