-- Миграция 006: remind_at, deadline_notifications, api_costs, recurrence

-- 1. Поля для time-based напоминаний в задачах
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS remind_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS recurrence TEXT;  -- NULL | 'daily' | 'weekly' | 'monthly'

-- Индекс для быстрой выборки задач с напоминаниями
CREATE INDEX IF NOT EXISTS idx_tasks_remind_at
    ON tasks (remind_at)
    WHERE remind_at IS NOT NULL AND reminder_sent = FALSE AND status = 'active';

-- 2. Таблица для трекинга дедлайн-уведомлений (в БД, не в памяти)
CREATE TABLE IF NOT EXISTS deadline_notifications (
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    notif_date  DATE NOT NULL,
    count       INTEGER DEFAULT 1,
    PRIMARY KEY (task_id, notif_date)
);

-- 3. Таблица для учёта стоимости API-вызовов
CREATE TABLE IF NOT EXISTS api_costs (
    id          SERIAL PRIMARY KEY,
    method      TEXT NOT NULL,           -- 'ask_with_tools' | 'classify' | 'briefing' | 'digest'
    model       TEXT NOT NULL,           -- 'sonnet' | 'haiku'
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    cost_usd    NUMERIC(10, 6) DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_costs_created ON api_costs (created_at DESC);

-- 4. user_preferences: дефолт если нет
INSERT INTO settings (key, value)
VALUES ('user_preferences', '{"address": "ты", "emoji": true, "style": "business-casual"}')
ON CONFLICT (key) DO NOTHING;
