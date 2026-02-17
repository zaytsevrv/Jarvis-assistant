import asyncpg
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src import config

logger = logging.getLogger("jarvis.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Глобальный пул соединений
_pool: Optional[asyncpg.Pool] = None


# ─── Подключение ────────────────────────────────────────────

async def init_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        min_size=2,
        max_size=10,
    )
    logger.info("PostgreSQL pool создан")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool закрыт")


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        await init_pool()
    return _pool


# ─── Создание таблиц ────────────────────────────────────────

async def create_tables():
    """Запускает систему миграций: применяет все неприменённые SQL-миграции."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Создаём таблицу версий если не существует
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW(),
                filename TEXT
            )
        """)

        # Текущая версия БД
        current = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        )

        # Собираем файлы миграций
        if not MIGRATIONS_DIR.exists():
            logger.warning(f"Папка миграций не найдена: {MIGRATIONS_DIR}")
            return

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        applied = 0

        for fpath in migration_files:
            # Извлекаем номер из имени: 001_initial.sql → 1
            try:
                version = int(fpath.stem.split("_")[0])
            except (ValueError, IndexError):
                logger.warning(f"Пропуск файла с некорректным именем: {fpath.name}")
                continue

            if version <= current:
                continue

            # Применяем миграцию
            sql = fpath.read_text(encoding="utf-8")
            try:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_version (version, filename) VALUES ($1, $2)",
                    version, fpath.name,
                )
                applied += 1
                logger.info(f"Миграция {fpath.name} применена")
            except Exception as e:
                logger.error(f"Ошибка миграции {fpath.name}: {e}")
                raise

        if applied:
            logger.info(f"Применено миграций: {applied}")
        else:
            logger.info(f"БД актуальна (версия {current})")


# ─── Настройки ───────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return row["value"] if row else default


async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES ($1, $2, NOW())
               ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
            key, value
        )


# ─── Сообщения ───────────────────────────────────────────────

async def save_message(
    telegram_msg_id: int,
    chat_id: int,
    chat_title: str,
    sender_id: int,
    sender_name: str,
    text: str,
    media_type: Optional[str],
    timestamp: datetime,
) -> Optional[int]:
    """Сохраняет сообщение. Возвращает id или None при дубликате."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO messages
               (telegram_msg_id, chat_id, chat_title, sender_id, sender_name,
                text, media_type, timestamp)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (telegram_msg_id, chat_id) DO NOTHING
               RETURNING id""",
            telegram_msg_id, chat_id, chat_title, sender_id, sender_name,
            text, media_type, timestamp,
        )
        return row["id"] if row else None


async def mark_message_processed(msg_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET processed = TRUE WHERE id = $1", msg_id
        )


async def get_recent_messages(chat_id: int, limit: int = 50) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM messages
               WHERE chat_id = $1
               ORDER BY timestamp DESC LIMIT $2""",
            chat_id, limit
        )
        return [dict(r) for r in rows]


async def search_messages(query: str, limit: int = 20) -> list:
    """Полнотекстовый поиск по сообщениям с русской морфологией."""
    pool = await get_pool()
    # Разбиваем запрос на слова, убираем короткие, формируем tsquery
    words = [w.strip() for w in query.split() if len(w.strip()) > 2]
    if not words:
        # Fallback на ILIKE если слова слишком короткие
        return await _search_messages_ilike(query, limit)

    # Объединяем слова через & (AND) для tsquery
    tsquery = " & ".join(words)

    async with pool.acquire() as conn:
        # Проверяем наличие столбца tsv (миграция 002 могла не примениться)
        has_tsv = await conn.fetchval(
            """SELECT EXISTS(
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'messages' AND column_name = 'tsv'
            )"""
        )

        if has_tsv:
            rows = await conn.fetch(
                """SELECT *, ts_rank(tsv, to_tsquery('russian', $1)) AS rank
                   FROM messages
                   WHERE tsv @@ to_tsquery('russian', $1)
                   ORDER BY rank DESC, timestamp DESC
                   LIMIT $2""",
                tsquery, limit
            )
        else:
            # Fallback если миграция FTS ещё не применена
            return await _search_messages_ilike(query, limit)

        return [dict(r) for r in rows]


async def search_messages_by_sender(sender_name: str, limit: int = 30) -> list:
    """Поиск сообщений по имени отправителя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM messages
               WHERE sender_name ILIKE '%' || $1 || '%'
               ORDER BY timestamp DESC LIMIT $2""",
            sender_name, limit
        )
        return [dict(r) for r in rows]


async def _search_messages_ilike(query: str, limit: int = 20) -> list:
    """Fallback поиск через ILIKE (без FTS)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM messages
               WHERE text ILIKE '%' || $1 || '%'
               ORDER BY timestamp DESC LIMIT $2""",
            query, limit
        )
        return [dict(r) for r in rows]


# ─── Выборка сообщений за период ────────────────────────────

async def get_messages_since(since: datetime, chat_ids: list = None, limit: int = 500) -> list:
    """Получает сообщения за период, опционально фильтруя по chat_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if chat_ids:
            rows = await conn.fetch(
                """SELECT chat_id, chat_title, sender_name, text, timestamp
                   FROM messages
                   WHERE timestamp >= $1 AND chat_id = ANY($2::bigint[])
                   ORDER BY chat_id, timestamp
                   LIMIT $3""",
                since, chat_ids, limit
            )
        else:
            rows = await conn.fetch(
                """SELECT chat_id, chat_title, sender_name, text, timestamp
                   FROM messages
                   WHERE timestamp >= $1
                   ORDER BY timestamp DESC
                   LIMIT $2""",
                since, limit
            )
        return [dict(r) for r in rows]


async def get_dm_summary_data(since: datetime, limit: int = 100) -> list:
    """Получает ЛС-сообщения за период (без owner, без blacklist), сгруппированные по отправителю."""
    pool = await get_pool()
    import json as _json
    raw_bl = await get_setting("blacklist", "[]")
    try:
        bl_ids = _json.loads(raw_bl)
    except _json.JSONDecodeError:
        bl_ids = []

    async with pool.acquire() as conn:
        if bl_ids:
            rows = await conn.fetch(
                """SELECT sender_name, COUNT(*) as msg_count,
                          STRING_AGG(LEFT(text, 100), ' | ' ORDER BY timestamp) as previews
                   FROM messages
                   WHERE timestamp >= $1
                     AND sender_id != $2
                     AND sender_id != ALL($3::bigint[])
                     AND chat_id = sender_id
                   GROUP BY sender_name
                   ORDER BY msg_count DESC
                   LIMIT $4""",
                since, config.TELEGRAM_OWNER_ID, bl_ids, limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT sender_name, COUNT(*) as msg_count,
                          STRING_AGG(LEFT(text, 100), ' | ' ORDER BY timestamp) as previews
                   FROM messages
                   WHERE timestamp >= $1
                     AND sender_id != $2
                     AND chat_id = sender_id
                   GROUP BY sender_name
                   ORDER BY msg_count DESC
                   LIMIT $3""",
                since, config.TELEGRAM_OWNER_ID, limit,
            )
        return [dict(r) for r in rows]


# ─── Сборка контекста для AI-запросов ─────────────────────────

# Стоп-слова (не ключевые для поиска)
_STOP_WORDS = {
    "что", "как", "где", "кто", "когда", "зачем", "почему", "какой", "какая", "какие",
    "скажи", "покажи", "найди", "напомни", "расскажи", "объясни", "помоги",
    "мне", "мой", "моя", "мои", "его", "её", "ему", "ей", "нам", "вам", "наш",
    "это", "эти", "этот", "эта", "тот", "тех", "том",
    "для", "без", "при", "над", "под", "между", "перед", "после",
    "уже", "ещё", "еще", "тоже", "также", "очень", "все", "всё", "вся",
    "был", "была", "было", "были", "есть", "нет", "будет",
    "про", "обо", "через", "около",
}


def _extract_names(query: str) -> list[str]:
    """Извлекает потенциальные имена из запроса (слова с заглавной буквы)."""
    words = query.split()
    names = []
    for i, word in enumerate(words):
        clean = re.sub(r'[^\w]', '', word)
        if not clean or len(clean) < 2:
            continue
        # Слово с заглавной буквы, не первое в предложении
        if clean[0].isupper() and clean[1:].islower():
            if i > 0 or len(words) > 1:
                names.append(clean)
    return names


def _extract_keywords(query: str) -> list[str]:
    """Извлекает ключевые слова для поиска, отбрасывая стоп-слова."""
    words = query.lower().split()
    return [w for w in words if len(w) > 2 and w not in _STOP_WORDS]


def _format_messages(messages: list, header: str = "") -> str:
    """Форматирует сообщения в текст для контекста."""
    if not messages:
        return ""
    lines = []
    if header:
        lines.append(header)
    seen = set()
    for m in messages:
        msg_id = m.get("id")
        if msg_id in seen:
            continue
        seen.add(msg_id)
        text = m.get("text", "")
        if not text:
            continue
        ts = m.get("timestamp")
        ts_str = ts.strftime("%d.%m %H:%M") if ts else "?"
        sender = m.get("sender_name", "?")
        chat = m.get("chat_title", "")
        lines.append(f"[{ts_str}] {sender} ({chat}): {text}")
    return "\n".join(lines)


async def build_context(query: str, max_chars: int = 50000) -> str:
    """Собирает релевантный контекст для AI-запроса.

    Стратегия:
    1. Свежие ЛС (всегда, 12ч)
    2. FTS по ключевым словам (без owner/ботов)
    3. Поиск по sender_name если есть имена
    4. Активные задачи
    5. Дедупликация и лимит по символам
    """
    parts = []
    used_chars = 0

    # 1. Свежие ЛС — всегда добавляем
    since = datetime.now(timezone.utc) - timedelta(hours=12)
    dm_data = await get_dm_summary_data(since, limit=20)
    if dm_data:
        dm_lines = ["СВЕЖИЕ ЛС (за 12ч):"]
        for d in dm_data[:15]:
            dm_lines.append(f"  {d['sender_name']} ({d['msg_count']} сообщ.): {d['previews'][:200]}")
        dm_text = "\n".join(dm_lines)
        parts.append(dm_text)
        used_chars += len(dm_text)

    # 2. FTS поиск по ключевым словам
    keywords = _extract_keywords(query)
    if keywords:
        fts_results = await search_messages(" ".join(keywords), limit=30)
        # Фильтруем owner и подозрительных ботов
        fts_results = [m for m in fts_results if m.get("sender_id") != config.TELEGRAM_OWNER_ID]
        fts_text = _format_messages(fts_results, "РЕЛЕВАНТНЫЕ СООБЩЕНИЯ:")
        if fts_text:
            parts.append(fts_text)
            used_chars += len(fts_text)

    # 3. Поиск по именам
    names = _extract_names(query)
    for name in names[:3]:  # максимум 3 имени
        if used_chars >= max_chars * 0.7:
            break
        sender_results = await search_messages_by_sender(name, limit=15)
        sender_text = _format_messages(sender_results, f"СООБЩЕНИЯ ОТ/ПРО {name.upper()}:")
        if sender_text:
            parts.append(sender_text)
            used_chars += len(sender_text)

    # 4. Активные задачи
    tasks = await get_active_tasks()
    if tasks:
        task_lines = ["АКТИВНЫЕ ЗАДАЧИ:"]
        for t in tasks:
            deadline_str = f" | до {t['deadline'].strftime('%d.%m.%Y')}" if t.get("deadline") else ""
            who_str = f" [{t['who']}]" if t.get("who") else ""
            task_lines.append(f"#{t['id']} [{t['type']}] {t['description']}{who_str}{deadline_str}")
        task_text = "\n".join(task_lines)
        parts.append(task_text)
        used_chars += len(task_text)

    if not parts:
        return "(нет релевантных данных в памяти)"

    # Собираем, обрезаем если превышает лимит
    context = "\n\n".join(parts)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...(обрезано)"

    return context


# ─── Задачи ──────────────────────────────────────────────────

async def has_similar_active_task(description: str) -> bool:
    """Проверяет, есть ли уже активная задача с похожим описанием."""
    pool = await get_pool()
    short = description[:50].strip()
    if not short:
        return False
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE status = 'active' AND description ILIKE '%' || $1 || '%')",
            short,
        )
        return exists


async def create_task(
    task_type: str,
    description: str,
    who: Optional[str] = None,
    deadline: Optional[datetime] = None,
    confidence: int = 100,
    source: Optional[str] = None,
    source_msg_id: Optional[int] = None,
    chat_id: Optional[int] = None,
) -> Optional[int]:
    """Создаёт задачу. Возвращает id или None если дубликат."""
    # Дедупликация
    if await has_similar_active_task(description):
        logger.info(f"Дубль задачи пропущен: {description[:60]}")
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO tasks
               (type, description, who, deadline, confidence, source, source_msg_id, chat_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING id""",
            task_type, description, who, deadline, confidence, source, source_msg_id, chat_id,
        )
        return row["id"]


async def get_active_tasks() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM tasks
               WHERE status = 'active'
               ORDER BY deadline ASC NULLS LAST, created_at DESC"""
        )
        return [dict(r) for r in rows]


async def complete_task(task_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE tasks
               SET status = 'done', completed_at = NOW()
               WHERE id = $1""",
            task_id
        )


async def cancel_task(task_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE id = $1", task_id
        )


# ─── Контакты ────────────────────────────────────────────────

async def get_or_create_contact(telegram_id: int, name: str, phone: str = None, chat_type: str = "private") -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM contacts WHERE telegram_id = $1", telegram_id
        )
        if row:
            return dict(row)
        row = await conn.fetchrow(
            """INSERT INTO contacts (telegram_id, name, phone, chat_type)
               VALUES ($1, $2, $3, $4) RETURNING *""",
            telegram_id, name, phone, chat_type,
        )
        return dict(row)


async def is_known_contact(telegram_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM contacts WHERE telegram_id = $1", telegram_id
        )
        return row is not None


# ─── Здоровье (heartbeat) ────────────────────────────────────

async def heartbeat(module: str, status: str = "ok", error: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO health_checks (module, status, error, timestamp)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT (module) DO UPDATE
               SET status = $2, error = $3, timestamp = NOW()""",
            module, status, error
        )


async def get_module_health() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT module, status, error, timestamp FROM health_checks ORDER BY module"
        )
        return [dict(r) for r in rows]


# ─── Confidence-очередь ──────────────────────────────────────

async def add_to_confidence_queue(
    message_id: int,
    chat_id: int,
    sender_name: str,
    text_preview: str,
    predicted_type: str,
    confidence: int,
    is_urgent: bool = False,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO confidence_queue
               (message_id, chat_id, sender_name, text_preview,
                predicted_type, confidence, is_urgent)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id""",
            message_id, chat_id, sender_name, text_preview,
            predicted_type, confidence, is_urgent,
        )
        return row["id"]


async def get_pending_confidence(limit: int = 10) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM confidence_queue
               WHERE resolved = FALSE AND is_urgent = FALSE
               ORDER BY created_at ASC LIMIT $1""",
            limit
        )
        return [dict(r) for r in rows]


async def resolve_confidence(queue_id: int, actual_type: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE confidence_queue SET resolved = TRUE WHERE id = $1",
            queue_id
        )
        row = await conn.fetchrow(
            "SELECT message_id, predicted_type FROM confidence_queue WHERE id = $1",
            queue_id
        )
        if row:
            await conn.execute(
                """INSERT INTO classification_feedback
                   (message_id, predicted_type, actual_type)
                   VALUES ($1, $2, $3)""",
                row["message_id"], row["predicted_type"], actual_type,
            )


# ─── Дневные сводки ──────────────────────────────────────────

async def save_daily_summary(date, summary: str, stats: dict = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json
        stats_json = json.dumps(stats) if stats else None
        await conn.execute(
            """INSERT INTO daily_summaries (date, summary, stats)
               VALUES ($1, $2, $3::jsonb)
               ON CONFLICT (date) DO UPDATE SET summary = $2, stats = $3::jsonb""",
            date, summary, stats_json,
        )


# ─── Статистика ──────────────────────────────────────────────

async def get_known_chats(exclude_private: bool = True) -> list:
    """Возвращает уникальные чаты из БД (chat_id, chat_title, msg_count).
    exclude_private=True убирает ЛС (chat_id == sender_id)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if exclude_private:
            rows = await conn.fetch(
                """SELECT chat_id, MAX(chat_title) as chat_title, COUNT(*) as msg_count
                   FROM messages
                   WHERE chat_id != sender_id
                   GROUP BY chat_id
                   ORDER BY msg_count DESC
                   LIMIT 50"""
            )
        else:
            rows = await conn.fetch(
                """SELECT chat_id, MAX(chat_title) as chat_title, COUNT(*) as msg_count
                   FROM messages
                   GROUP BY chat_id
                   ORDER BY msg_count DESC
                   LIMIT 50"""
            )
        return [dict(r) for r in rows]


async def get_db_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM messages")
        task_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status = 'active'")
        db_size = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        )
        return {
            "messages": msg_count,
            "active_tasks": task_count,
            "db_size": db_size,
        }
