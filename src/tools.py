"""
JARVIS Tools — инструменты для Anthropic tool_use.

Каждый tool — обёртка над существующими функциями из db.py.
Модель сама решает когда вызывать инструменты.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src import config
from src.db import (
    create_task,
    get_active_tasks,
    complete_task,
    cancel_task,
    search_messages,
    get_messages_since,
    get_setting,
    set_setting,
    has_similar_active_task,
    build_message_link,
)

logger = logging.getLogger("jarvis.tools")


# ─── Определения tools для Anthropic API ─────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "create_task",
        "description": (
            "Создать задачу или напоминание. Используй когда пользователь явно просит: "
            "'напомни', 'запиши', 'зафиксируй', 'создай задачу'. "
            "НЕ создавай задачу если пользователь просто делится информацией."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Описание задачи. Копируй формулировку пользователя, не перефразируй."
                },
                "task_type": {
                    "type": "string",
                    "enum": ["task", "promise_mine", "promise_incoming"],
                    "description": "task — обычная задача/напоминание, promise_mine — я пообещал, promise_incoming — мне пообещали"
                },
                "deadline": {
                    "type": "string",
                    "description": "Дедлайн в формате YYYY-MM-DD. Если 'завтра' — вычисли дату. Если не указан — null."
                },
                "who": {
                    "type": "string",
                    "description": "Кто должен выполнить (имя). Если задача для самого пользователя — null."
                },
                "remind_at": {
                    "type": "string",
                    "description": (
                        "Конкретное время напоминания в формате YYYY-MM-DDTHH:MM (по Красноярску UTC+7). "
                        "Если пользователь сказал 'в 11:00' или 'завтра в 11' — обязательно заполни. "
                        "Если время не указано — null."
                    )
                },
                "recurrence": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "Повторение задачи. Заполняй только если пользователь явно попросил 'каждую неделю', 'ежемесячно' и т.д."
                },
            },
            "required": ["description", "task_type"],
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "Показать активные задачи. Используй когда пользователь спрашивает: "
            "'какие задачи', 'что на сегодня', 'что в работе', 'список дел'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_type": {
                    "type": "string",
                    "enum": ["all", "task", "promise_mine", "promise_incoming"],
                    "description": "Фильтр по типу. По умолчанию all."
                },
            },
        },
    },
    {
        "name": "complete_task",
        "description": (
            "Отметить задачу как выполненную. Используй когда пользователь говорит: "
            "'сделано', 'выполнено', 'забрал', 'готово' — и из контекста понятно какая задача."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID задачи из списка задач."
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel_task",
        "description": (
            "Удалить/отменить задачу. Используй когда пользователь говорит: "
            "'убери', 'удали', 'отмени задачу'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID задачи для отмены."
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "update_task",
        "description": (
            "Изменить задачу — описание, дедлайн или ответственного. "
            "Используй когда: 'перенеси на 20-е', 'поменяй описание'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID задачи."
                },
                "new_description": {
                    "type": "string",
                    "description": "Новое описание (если меняется)."
                },
                "new_deadline": {
                    "type": "string",
                    "description": "Новый дедлайн YYYY-MM-DD (если меняется)."
                },
                "new_who": {
                    "type": "string",
                    "description": "Новый ответственный (если меняется)."
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Поиск по всей базе сообщений. Используй когда пользователь спрашивает: "
            "'что писал Козлов', 'найди про оплату', 'когда обсуждали'. "
            "Ищет по полнотекстовому поиску с русской морфологией."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (ключевые слова)."
                },
                "limit": {
                    "type": "integer",
                    "description": "Максимум результатов (по умолчанию 20)."
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_chat_summary",
        "description": (
            "Получить сводку по чату/группе за период. Используй когда: "
            "'что обсуждали в Логистике', 'сводка по группе', 'что нового в канале'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": "ID чата. Если не знаешь — используй search_memory."
                },
                "hours": {
                    "type": "integer",
                    "description": "За сколько часов (по умолчанию 24)."
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "manage_whitelist",
        "description": (
            "Управление whitelist чатов для мониторинга. "
            "Используй когда: 'добавь этот канал', 'убери из мониторинга', 'покажи whitelist'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "remove"],
                    "description": "list — показать, add — добавить, remove — убрать."
                },
                "chat_id": {
                    "type": "integer",
                    "description": "ID чата (для add/remove)."
                },
            },
            "required": ["action"],
        },
    },
]


# ─── Исполнение tools ─────────────────────────────────────────

async def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Выполняет tool и возвращает результат как строку для модели."""
    try:
        handler = _TOOL_HANDLERS.get(tool_name)
        if not handler:
            return json.dumps({"error": f"Неизвестный инструмент: {tool_name}"}, ensure_ascii=False)
        result = await handler(tool_input)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Ошибка выполнения tool {tool_name}: {e}", exc_info=True)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _tool_create_task(params: dict) -> dict:
    description = params["description"]
    task_type = params.get("task_type", "task")
    deadline_str = params.get("deadline")
    remind_at_str = params.get("remind_at")
    who = params.get("who")
    recurrence = params.get("recurrence")

    # UTC+7 для Красноярска
    TZ_OFFSET = timezone(timedelta(hours=7))

    deadline = None
    if deadline_str:
        try:
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d").replace(tzinfo=TZ_OFFSET)
        except ValueError:
            return {"error": f"Некорректный формат даты: {deadline_str}. Нужен YYYY-MM-DD."}

    remind_at = None
    if remind_at_str:
        try:
            # Формат YYYY-MM-DDTHH:MM, интерпретируем как Красноярское время
            remind_at = datetime.fromisoformat(remind_at_str).replace(tzinfo=TZ_OFFSET)
        except ValueError:
            return {"error": f"Некорректный формат времени: {remind_at_str}. Нужен YYYY-MM-DDTHH:MM."}

    # Улучшенный дедуп — возвращаем данные существующей задачи
    existing = await _find_similar_task(description)
    if existing:
        return {
            "status": "duplicate",
            "message": "Похожая задача уже существует.",
            "existing_task": {
                "id": existing["id"],
                "description": existing["description"],
                "deadline": existing["deadline"].strftime("%d.%m.%Y") if existing.get("deadline") else None,
                "remind_at": existing["remind_at"].strftime("%d.%m %H:%M") if existing.get("remind_at") else None,
            }
        }

    task_id = await create_task(
        task_type=task_type,
        description=description,
        who=who,
        deadline=deadline,
        confidence=100,
        source="owner_dialog",
        remind_at=remind_at,
        recurrence=recurrence,
    )

    if task_id is None:
        return {"status": "duplicate", "message": "Похожая задача уже существует."}

    remind_confirm = ""
    if remind_at:
        remind_confirm = remind_at.strftime("%d.%m в %H:%M")

    return {
        "status": "created",
        "task_id": task_id,
        "description": description,
        "deadline": deadline_str,
        "remind_at": remind_at_str,
        "remind_confirm": remind_confirm,
        "who": who,
        "recurrence": recurrence,
    }


async def _find_similar_task(description: str) -> Optional[dict]:
    """Ищет похожую активную задачу, возвращает dict или None."""
    tasks = await get_active_tasks()
    desc_lower = description.lower().strip()[:70]
    for t in tasks:
        t_desc = t["description"].lower().strip()[:70]
        if desc_lower in t_desc or t_desc in desc_lower:
            return t
    return None


async def _tool_list_tasks(params: dict) -> dict:
    tasks = await get_active_tasks()
    filter_type = params.get("filter_type", "all")

    if filter_type != "all":
        tasks = [t for t in tasks if t["type"] == filter_type]

    if not tasks:
        return {"status": "empty", "message": "Активных задач нет.", "tasks": []}

    result = []
    for t in tasks:
        item = {
            "id": t["id"],
            "type": t["type"],
            "description": t["description"],
            "who": t.get("who"),
            "created_at": t["created_at"].strftime("%d.%m.%Y") if t.get("created_at") else None,
        }
        if t.get("deadline"):
            item["deadline"] = t["deadline"].strftime("%d.%m.%Y")
        if t.get("remind_at"):
            item["remind_at"] = t["remind_at"].strftime("%H:%M %d.%m")
        if t.get("recurrence"):
            item["recurrence"] = t["recurrence"]
        link = build_message_link(t.get("chat_id", 0), t.get("orig_tg_msg_id", 0))
        if link:
            item["link"] = link
        result.append(item)

    return {"status": "ok", "count": len(result), "tasks": result}


async def _tool_complete_task(params: dict) -> dict:
    task_id = params["task_id"]
    # Проверяем что задача существует и активна
    tasks = await get_active_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        return {"error": f"Задача #{task_id} не найдена или уже завершена."}

    await complete_task(task_id)
    return {"status": "completed", "task_id": task_id, "description": task["description"]}


async def _tool_cancel_task(params: dict) -> dict:
    task_id = params["task_id"]
    tasks = await get_active_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        return {"error": f"Задача #{task_id} не найдена или уже завершена."}

    await cancel_task(task_id)
    return {"status": "cancelled", "task_id": task_id, "description": task["description"]}


async def _tool_update_task(params: dict) -> dict:
    from src.db import get_pool

    task_id = params["task_id"]
    tasks = await get_active_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        return {"error": f"Задача #{task_id} не найдена или уже завершена."}

    updates = []
    values = []
    param_idx = 1

    if "new_description" in params and params["new_description"]:
        param_idx += 1
        updates.append(f"description = ${param_idx}")
        values.append(params["new_description"])

    if "new_deadline" in params and params["new_deadline"]:
        try:
            dl = datetime.strptime(params["new_deadline"], "%Y-%m-%d")
            param_idx += 1
            updates.append(f"deadline = ${param_idx}")
            values.append(dl)
        except ValueError:
            return {"error": f"Некорректная дата: {params['new_deadline']}"}

    if "new_who" in params and params["new_who"]:
        param_idx += 1
        updates.append(f"who = ${param_idx}")
        values.append(params["new_who"])

    if not updates:
        return {"error": "Нечего обновлять — не указаны новые значения."}

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = $1",
            task_id, *values,
        )

    return {
        "status": "updated",
        "task_id": task_id,
        "updated_fields": list(params.keys()),
    }


async def _tool_search_memory(params: dict) -> dict:
    query = params["query"]
    limit = params.get("limit", 20)

    results = await search_messages(query, limit=limit)

    if not results:
        return {"status": "empty", "message": f"Ничего не найдено по запросу: {query}"}

    messages = []
    for m in results[:limit]:
        link = build_message_link(m.get("chat_id", 0), m.get("telegram_msg_id", 0))
        msg = {
            "sender": m.get("sender_name", "?"),
            "chat": m.get("chat_title", "?"),
            "text": m.get("text", "")[:500],
            "date": m["timestamp"].strftime("%d.%m.%Y %H:%M") if m.get("timestamp") else "?",
        }
        if link:
            msg["link"] = link
        messages.append(msg)

    return {"status": "ok", "count": len(messages), "messages": messages}


async def _tool_get_chat_summary(params: dict) -> dict:
    chat_id = params["chat_id"]
    hours = params.get("hours", 24)

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages = await get_messages_since(since, chat_ids=[chat_id], limit=200)

    if not messages:
        return {"status": "empty", "message": f"Нет сообщений за последние {hours}ч в этом чате."}

    formatted = []
    for m in messages:
        formatted.append({
            "sender": m.get("sender_name", "?"),
            "text": m.get("text", "")[:300],
            "time": m["timestamp"].strftime("%H:%M") if m.get("timestamp") else "?",
        })

    return {
        "status": "ok",
        "chat_id": chat_id,
        "chat_title": messages[0].get("chat_title", "?") if messages else "?",
        "period_hours": hours,
        "message_count": len(formatted),
        "messages": formatted,
    }


async def _tool_manage_whitelist(params: dict) -> dict:
    action = params["action"]
    chat_id = params.get("chat_id")

    raw = await get_setting("whitelist", "[]")
    try:
        wl = json.loads(raw)
    except json.JSONDecodeError:
        wl = []

    if action == "list":
        return {"status": "ok", "whitelist": wl, "count": len(wl)}

    if action == "add":
        if not chat_id:
            return {"error": "Не указан chat_id для добавления."}
        if chat_id in wl:
            return {"status": "already_exists", "message": f"Чат {chat_id} уже в whitelist."}
        wl.append(chat_id)
        await set_setting("whitelist", json.dumps(wl))
        return {"status": "added", "chat_id": chat_id, "total": len(wl)}

    if action == "remove":
        if not chat_id:
            return {"error": "Не указан chat_id для удаления."}
        if chat_id not in wl:
            return {"status": "not_found", "message": f"Чат {chat_id} не в whitelist."}
        wl.remove(chat_id)
        await set_setting("whitelist", json.dumps(wl))
        return {"status": "removed", "chat_id": chat_id, "total": len(wl)}

    return {"error": f"Неизвестное действие: {action}"}


async def _tool_update_preferences(params: dict) -> dict:
    """Сохраняет пользовательские настройки в БД навсегда."""
    import json as _json
    key = params.get("key")
    value = params.get("value")

    ALLOWED_KEYS = {
        "address": ["ты", "вы"],
        "emoji": [True, False, "true", "false"],
        "style": ["formal", "casual", "business-casual"],
    }

    if key not in ALLOWED_KEYS:
        return {"error": f"Недопустимый ключ настройки: {key}. Доступны: {list(ALLOWED_KEYS.keys())}"}

    allowed_values = ALLOWED_KEYS[key]
    # Нормализация булевых
    if key == "emoji":
        value = value in [True, "true", "True", 1]

    raw = await get_setting("user_preferences", '{"address": "ты", "emoji": true, "style": "business-casual"}')
    try:
        prefs = _json.loads(raw)
    except Exception:
        prefs = {"address": "ты", "emoji": True, "style": "business-casual"}

    prefs[key] = value
    await set_setting("user_preferences", _json.dumps(prefs, ensure_ascii=False))

    labels = {"address": "обращение", "emoji": "эмодзи", "style": "стиль"}
    return {
        "status": "saved",
        "message": f"Настройка '{labels.get(key, key)}' сохранена навсегда: {value}",
        "preferences": prefs,
    }


# Маппинг имя → обработчик
_TOOL_HANDLERS = {
    "create_task": _tool_create_task,
    "list_tasks": _tool_list_tasks,
    "complete_task": _tool_complete_task,
    "cancel_task": _tool_cancel_task,
    "update_task": _tool_update_task,
    "search_memory": _tool_search_memory,
    "get_chat_summary": _tool_get_chat_summary,
    "manage_whitelist": _tool_manage_whitelist,
    "update_preferences": _tool_update_preferences,
}
