import logging
from datetime import datetime, date

from src import config
from src.db import (
    add_to_confidence_queue,
    create_task,
    get_pending_confidence,
    get_setting,
    resolve_confidence,
    mark_message_processed,
)
from src.ai_brain import brain

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


# ─── Основная логика классификации ───────────────────────────

async def process_classification(
    db_msg_id: int,
    text: str,
    sender_name: str,
    chat_title: str,
    chat_id: int,
):
    """Классификация сообщения AI и обработка по уровню confidence."""
    try:
        result = await brain.classify_message(text, sender_name, chat_title)

        msg_type = result.get("type", "info")
        confidence = result.get("confidence", 0)
        summary = result.get("summary", text[:100])
        deadline_str = result.get("deadline")
        who = result.get("who")
        is_urgent = result.get("is_urgent", False)

        # Парсинг дедлайна
        deadline = None
        if deadline_str:
            try:
                deadline = datetime.fromisoformat(deadline_str)
            except (ValueError, TypeError):
                pass

        # Три зоны confidence
        if confidence > config.CONFIDENCE_HIGH:
            # >80% — молча создаёт задачу
            if msg_type in ("task", "promise_mine", "promise_incoming"):
                task_id = await create_task(
                    task_type=msg_type,
                    description=summary,
                    who=who,
                    deadline=deadline,
                    confidence=confidence,
                    source=f"telegram:{chat_title}",
                    source_msg_id=db_msg_id,
                    chat_id=chat_id,
                )
                logger.info(f"Задача #{task_id} создана (confidence {confidence}%): {summary}")

        elif confidence >= config.CONFIDENCE_LOW:
            # 50-80% — в очередь confidence
            if msg_type in ("task", "promise_mine", "promise_incoming", "question"):
                # Проверяем: срочное?
                if is_urgent:
                    await _handle_urgent(
                        db_msg_id, chat_id, sender_name, text, msg_type, confidence
                    )
                else:
                    await add_to_confidence_queue(
                        message_id=db_msg_id,
                        chat_id=chat_id,
                        sender_name=sender_name,
                        text_preview=text[:150],
                        predicted_type=msg_type,
                        confidence=confidence,
                        is_urgent=False,
                    )
                    logger.info(f"В confidence-очередь (confidence {confidence}%): {summary}")

        # <50% — молча сохраняет как info, ничего не делает
        else:
            logger.debug(f"Пропущено (confidence {confidence}%): {summary}")

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
        task_id = await create_task(
            task_type=actual_type,
            description=row["text_preview"] or f"Задача от {row['sender_name']}",
            who=row["sender_name"] if actual_type == "promise_incoming" else None,
            confidence=100,  # Подтверждено пользователем
            source=f"confidence:{queue_id}",
            source_msg_id=row["message_id"],
            chat_id=row["chat_id"],
        )
        if task_id:
            logger.info(f"Задача #{task_id} создана из confidence #{queue_id}")
        else:
            logger.info(f"Дубль задачи из confidence #{queue_id}")
