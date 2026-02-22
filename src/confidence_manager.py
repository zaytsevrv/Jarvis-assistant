import asyncio
import logging
from datetime import datetime, date, timedelta, timezone

from src import config
from src.db import (
    add_to_confidence_queue,
    create_task,
    has_similar_active_task,
    get_pending_confidence,
    get_setting,
    resolve_confidence,
    mark_message_processed,
    get_context_for_classification,
    get_recent_chat_messages,
)
from src.ai_brain import brain


# v6: Метки типов для прозрачных уведомлений
_TYPE_LABELS = {
    "task_from_me": "Задача от вас",
    "task_for_me": "Задача для вас",
    "promise_mine": "Ваше обещание",
    "promise_incoming": "Чужое обещание",
    "info": "Информация",
    "question": "Вопрос",
    "spam": "Спам/мусор",
}


def _type_label(t: str) -> str:
    return _TYPE_LABELS.get(t, t)

logger = logging.getLogger("jarvis.confidence")

# Callback для уведомлений в бот
_notify_callback = None


def set_notify_callback(callback):
    global _notify_callback
    _notify_callback = callback


async def notify_owner(text: str, **kwargs):
    if _notify_callback:
        await _notify_callback(text, **kwargs)


# Счётчик вопросов за сегодня
_today_questions = 0
_today_date = None


def _reset_daily_counter():
    global _today_questions, _today_date
    today = date.today()
    if _today_date != today:
        _today_questions = 0
        _today_date = today


# ─── B3: Отложенное уведомление для MEDIUM (5 мин буфер) ────

_MEDIUM_DELAY_SEC = 300  # 5 минут


async def _delayed_medium_notify(
    chat_id: int,
    summary: str,
    notify_text: str,
    notify_kwargs: dict,
):
    """B3: Ждёт 5 минут, проверяет не разрешилась ли задача сама,
    затем отправляет уведомление если задача всё ещё актуальна."""
    await asyncio.sleep(_MEDIUM_DELAY_SEC)
    try:
        since = datetime.now(timezone.utc) - timedelta(seconds=_MEDIUM_DELAY_SEC + 30)
        recent = await get_recent_chat_messages(chat_id, since, limit=8)
        if recent:
            messages_text = "\n".join(
                f"[{'ВЛАДЕЛЕЦ' if config.is_owner(m.get('sender_id', 0)) else m.get('sender_name', '?')}]: {(m.get('text') or '')[:150]}"
                for m in recent
            )
            prompt = (
                f"Задача: \"{summary}\"\n\n"
                f"Сообщения в диалоге за последние 5 минут:\n{messages_text}\n\n"
                f"Была ли задача выполнена, отменена или стала неактуальной "
                f"судя по этим сообщениям? Ответь одним словом: YES или NO."
            )
            answer = await brain.ask(prompt, model="haiku")
            if "YES" in answer.upper():
                logger.info(f"B3: MEDIUM задача разрешилась за 5 мин, уведомление отменено: {summary[:60]}")
                return
    except Exception as e:
        logger.warning(f"B3: ошибка проверки разрешения задачи: {e}")

    # Задача не разрешилась — отправляем уведомление
    try:
        await notify_owner(notify_text, **notify_kwargs)
        logger.info(f"B3: MEDIUM уведомление отправлено после задержки: {summary[:60]}")
    except Exception as e:
        logger.error(f"B3: ошибка отправки MEDIUM уведомления: {e}")


# ─── Основная логика классификации ───────────────────────────

async def process_classification(
    db_msg_id: int,
    text: str,
    sender_name: str,
    chat_title: str,
    chat_id: int,
    sender_id: int = 0,
    account_label: str = "",
):
    """Классификация сообщения AI и обработка по уровню confidence.
    v6: прозрачность для ВСЕХ 3 зон + original_type + авто-remind + feedback."""
    try:
        # B1: загружаем расширенный контекст (10 сообщений до текущего включительно)
        context_messages = await get_context_for_classification(chat_id, db_msg_id, limit=10)

        # v4: определяем направление — от владельца или к владельцу
        owner_is_sender = config.is_owner(sender_id) if sender_id else False

        result = await brain.classify_message(
            text, sender_name, chat_title,
            context_messages=context_messages,
            owner_is_sender=owner_is_sender,
        )

        msg_type = result.get("type", "info")
        confidence = result.get("confidence", 0)
        summary = result.get("summary", text[:100])
        deadline_str = result.get("deadline")
        who = result.get("who")
        is_urgent = result.get("is_urgent", False)
        assignee = result.get("assignee")  # v4: кому назначена задача

        # Парсинг дедлайна (всегда UTC-aware для PostgreSQL TIMESTAMPTZ)
        deadline = None
        if deadline_str:
            try:
                deadline = datetime.fromisoformat(deadline_str)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # v6: сохраняем original_type ДО нормализации (для уведомлений и feedback)
        original_type = msg_type

        # v6: track_completion для исходящих задач И чужих обещаний
        track = original_type in ("task_from_me", "promise_incoming")

        # v6: авто-remind_at для входящих задач и своих обещаний
        remind_at = None
        if original_type in ("task_for_me", "promise_mine"):
            if deadline:
                remind_at = deadline - timedelta(hours=2)
            else:
                remind_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # Нормализуем тип для БД (DB constraint: task, promise_mine, promise_incoming)
        db_type = msg_type
        if db_type in ("task_from_me", "task_for_me", "question"):
            db_type = "task"

        # Deep link: для ЛС chat_id = user_id → tg://user открывает чат
        # Для групп нужен telegram_msg_id (не передаётся), оставляем пустым
        if chat_id and chat_id > 0:
            link_html = f' <a href="tg://user?id={chat_id}">📎</a>'
        else:
            link_html = ""

        # v6: Три зоны confidence — ВСЕ прозрачны для владельца
        if confidence > config.CONFIDENCE_HIGH:
            # >90% — создаёт задачу + уведомляет
            if db_type in ("task", "promise_mine", "promise_incoming"):
                # v9: дедупликация для автоматической классификации (убрана из create_task)
                if await has_similar_active_task(summary):
                    logger.info(f"Дубль задачи пропущен (classify HIGH): {summary[:60]}")
                    return
                task_id = await create_task(
                    task_type=db_type,
                    description=summary,
                    who=who or assignee,
                    deadline=deadline,
                    remind_at=remind_at,
                    confidence=confidence,
                    source=f"telegram:{chat_title}",
                    source_msg_id=db_msg_id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    account=account_label,
                    track_completion=track,
                )
                logger.info(f"Задача #{task_id} создана (confidence {confidence}%): {summary}")

                # v6: Прозрачное уведомление (HIGH)
                await notify_owner(
                    f"🔔 <b>Авто-задача #{task_id}</b> ({confidence}%)\n"
                    f"📝 {summary}\n"
                    f"👤 {sender_name} → {who or assignee or '?'}\n"
                    f"🗂 {_type_label(original_type)}\n"
                    f"📱 {account_label}{link_html}",
                    reply_markup_type="classify_high",
                    task_id=task_id,
                    message_id=db_msg_id,
                    extra={"original_type": original_type, "confidence": confidence,
                           "task_id": task_id, "zone": "high",
                           "summary": summary, "who": who or assignee,
                           "sender_id": sender_id, "sender_name": sender_name,
                           "chat_id": chat_id, "chat_title": chat_title,
                           "account": account_label, "db_type": db_type},
                )
            else:
                # HIGH но info/question/spam — просто лог
                logger.info(f"Классификация HIGH {original_type} ({confidence}%): {summary}")

        elif confidence >= config.CONFIDENCE_LOW:
            # 50-90% — НЕ создаёт задачу, спрашивает владельца (B3: через 5 мин)
            if db_type in ("task", "promise_mine", "promise_incoming", "question"):
                notify_text = (
                    f"❓ <b>Похоже на задачу</b> ({confidence}%)\n"
                    f"📝 {summary}\n"
                    f"👤 {sender_name}\n"
                    f"🗂 {_type_label(original_type)}\n"
                    f"📱 {account_label}{link_html}"
                )
                notify_kwargs = dict(
                    reply_markup_type="classify_medium",
                    message_id=db_msg_id,
                    extra={"original_type": original_type, "confidence": confidence,
                           "summary": summary, "who": who or assignee,
                           "deadline_str": deadline_str,
                           "sender_id": sender_id, "sender_name": sender_name,
                           "chat_id": chat_id, "chat_title": chat_title,
                           "account": account_label, "track": track,
                           "remind_at_iso": remind_at.isoformat() if remind_at else None,
                           "db_type": db_type, "zone": "medium"},
                )
                # B3: срочные уведомляем сразу, остальные — через 5 минут
                if is_urgent:
                    await notify_owner(notify_text, **notify_kwargs)
                    logger.info(f"Classify MEDIUM СРОЧНОЕ → владелец ({confidence}%): {summary}")
                else:
                    asyncio.create_task(_delayed_medium_notify(chat_id, summary, notify_text, notify_kwargs))
                    logger.info(f"Classify MEDIUM → отложено 5 мин ({confidence}%): {summary}")
            else:
                logger.debug(f"Классификация MEDIUM {original_type} ({confidence}%): {summary}")

        # <50% — уведомляет информационно
        else:
            await notify_owner(
                f"ℹ️ <b>{_type_label(original_type)}</b> ({confidence}%)\n"
                f"📝 {summary}\n"
                f"👤 {sender_name}\n"
                f"📱 {account_label}{link_html}",
                reply_markup_type="classify_low",
                message_id=db_msg_id,
                extra={"original_type": original_type, "confidence": confidence,
                       "summary": summary, "sender_name": sender_name,
                       "sender_id": sender_id, "chat_id": chat_id,
                       "chat_title": chat_title, "account": account_label,
                       "zone": "low"},
            )
            logger.debug(f"Classify LOW ({confidence}%): {summary}")

    except Exception as e:
        logger.error(f"Ошибка классификации: {e}", exc_info=True)


# ─── Срочное — спрашивает сразу ──────────────────────────────

async def _handle_urgent(
    db_msg_id: int,
    chat_id: int,
    sender_name: str,
    text: str,
    predicted_type: str,
    confidence: int,
):
    """Срочный confidence-вопрос — отправляет СРАЗУ, не ждёт 16:00."""
    _reset_daily_counter()
    global _today_questions

    limit = int(await get_setting("confidence_daily_limit", str(config.CONFIDENCE_DAILY_LIMIT)))
    if _today_questions >= limit:
        # Лимит исчерпан — молча в очередь
        await add_to_confidence_queue(
            message_id=db_msg_id,
            chat_id=chat_id,
            sender_name=sender_name,
            text_preview=text[:150],
            predicted_type=predicted_type,
            confidence=confidence,
            is_urgent=True,
        )
        return

    _today_questions += 1

    type_label = {
        "task": "задача",
        "promise_mine": "моё обещание",
        "promise_incoming": "чужое обещание",
        "question": "вопрос",
    }.get(predicted_type, predicted_type)

    queue_id = await add_to_confidence_queue(
        message_id=db_msg_id,
        chat_id=chat_id,
        sender_name=sender_name,
        text_preview=text[:150],
        predicted_type=predicted_type,
        confidence=confidence,
        is_urgent=True,
    )

    await notify_owner(
        f"СРОЧНОЕ: {sender_name}: \"{text[:150]}\"\n"
        f"Уверенность: {confidence}%. Это {type_label}?",
        reply_markup_type="urgent_confidence",
        queue_id=queue_id,
    )


# ─── Батч-разбор (вызывается из scheduler в 16:00) ──────────

async def send_batch_review():
    """Отправка батча неуверенных классификаций за день."""
    pending = await get_pending_confidence(limit=config.CONFIDENCE_DAILY_LIMIT)

    if not pending:
        logger.info("Батч confidence: нет вопросов")
        return

    # Формируем сообщение
    lines = [f"За сегодня я засомневался в {len(pending)} сообщениях:\n"]
    for i, item in enumerate(pending, 1):
        type_label = {
            "task": "задача",
            "promise_mine": "обещание",
            "promise_incoming": "обещание",
            "question": "вопрос",
        }.get(item["predicted_type"], item["predicted_type"])

        time_str = item["created_at"].strftime("%H:%M") if item["created_at"] else ""
        lines.append(
            f"{i}. [ ] {item['sender_name']} ({time_str}): "
            f"\"{item['text_preview'][:80]}\" — {type_label}?"
        )

    text = "\n".join(lines)

    await notify_owner(
        text,
        reply_markup_type="batch_confidence",
        queue_ids=[item["id"] for item in pending],
    )

    logger.info(f"Батч confidence отправлен: {len(pending)} вопросов")


# ─── Обработка ответа пользователя ───────────────────────────

async def resolve_batch_all_tasks(queue_ids: list[int]):
    """Пользователь нажал 'Все задачи' — A4: реально создаём задачи."""
    for qid in queue_ids:
        await _resolve_and_create(qid, "task")
    logger.info(f"Батч: все {len(queue_ids)} подтверждены как задачи")


async def resolve_batch_nothing(queue_ids: list[int]):
    """Пользователь нажал 'Ничего'."""
    for qid in queue_ids:
        await resolve_confidence(qid, "info")
    logger.info(f"Батч: все {len(queue_ids)} отклонены")


async def resolve_single(queue_id: int, actual_type: str):
    """Пользователь ответил на один вопрос — A4: создаём задачу если тип task."""
    if actual_type in ("task", "promise_mine", "promise_incoming"):
        await _resolve_and_create(queue_id, actual_type)
    else:
        await resolve_confidence(queue_id, actual_type)
    logger.info(f"Confidence #{queue_id} → {actual_type}")


async def _resolve_and_create(queue_id: int, actual_type: str):
    """Резолвит confidence и РЕАЛЬНО создаёт задачу в БД."""
    from src.db import get_pool

    # Получаем данные из confidence_queue
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT message_id, chat_id, sender_name, text_preview, predicted_type "
            "FROM confidence_queue WHERE id = $1",
            queue_id,
        )

    await resolve_confidence(queue_id, actual_type)

    if row:
        desc = row["text_preview"] or f"Задача от {row['sender_name']}"
        if await has_similar_active_task(desc):
            logger.info(f"Дубль задачи из confidence #{queue_id}")
            return
        task_id = await create_task(
            task_type=actual_type,
            description=desc,
            who=row["sender_name"] if actual_type == "promise_incoming" else None,
            confidence=100,  # Подтверждено пользователем
            source=f"confidence:{queue_id}",
            source_msg_id=row["message_id"],
            chat_id=row["chat_id"],
        )
        if task_id:
            logger.info(f"Задача #{task_id} создана из confidence #{queue_id}")
