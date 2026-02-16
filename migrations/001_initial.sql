-- Миграция 001: Начальная схема (Фаза 1)

-- Контакты
CREATE TABLE IF NOT EXISTS contacts (
    id              SERIAL PRIMARY KEY,
    telegram_id     BIGINT UNIQUE,
    name            TEXT,
    phone           TEXT,
    chat_type       TEXT DEFAULT 'private',
    monitored       BOOLEAN DEFAULT FALSE,
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT
);

-- Сообщения Telegram
CREATE TABLE IF NOT EXISTS messages (
    id              SERIAL PRIMARY KEY,
    telegram_msg_id BIGINT,
    chat_id         BIGINT NOT NULL,
    chat_title      TEXT,
    sender_id       BIGINT,
    sender_name     TEXT,
    text            TEXT,
    media_type      TEXT,
    timestamp       TIMESTAMPTZ NOT NULL,
    processed       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(telegram_msg_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_processed ON messages(processed);

-- Задачи и обещания
CREATE TABLE IF NOT EXISTS tasks (
    id              SERIAL PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('task', 'promise_mine', 'promise_incoming')),
    description     TEXT NOT NULL,
    who             TEXT,
    deadline        TIMESTAMPTZ,
    status          TEXT DEFAULT 'active' CHECK (status IN ('active', 'done', 'cancelled', 'overdue')),
    confidence      INTEGER DEFAULT 100,
    source          TEXT,
    source_msg_id   INTEGER REFERENCES messages(id),
    chat_id         BIGINT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);

-- Настройки (key-value)
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Здоровье модулей (heartbeat) — одна строка на модуль
CREATE TABLE IF NOT EXISTS health_checks (
    module          TEXT PRIMARY KEY,
    status          TEXT DEFAULT 'ok',
    error           TEXT,
    timestamp       TIMESTAMPTZ DEFAULT NOW()
);

-- Обратная связь по классификации (самообучение)
CREATE TABLE IF NOT EXISTS classification_feedback (
    id              SERIAL PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id),
    predicted_type  TEXT,
    actual_type     TEXT,
    context         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Confidence-очередь (неуверенные классификации)
CREATE TABLE IF NOT EXISTS confidence_queue (
    id              SERIAL PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id),
    chat_id         BIGINT,
    sender_name     TEXT,
    text_preview    TEXT,
    predicted_type  TEXT,
    confidence      INTEGER,
    is_urgent       BOOLEAN DEFAULT FALSE,
    resolved        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_confidence_resolved ON confidence_queue(resolved);

-- Дневные сводки
CREATE TABLE IF NOT EXISTS daily_summaries (
    id              SERIAL PRIMARY KEY,
    date            DATE UNIQUE NOT NULL,
    summary         TEXT NOT NULL,
    stats           JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Дефолтные настройки
INSERT INTO settings (key, value) VALUES
    ('ai_mode', 'api'),
    ('confidence_daily_limit', '10'),
    ('confidence_batch_hour', '16'),
    ('whitelist', '[]')
ON CONFLICT (key) DO NOTHING;
