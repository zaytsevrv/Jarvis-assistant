-- Миграция 004: Таблица истории диалога с ботом
-- Хранит последние сообщения для передачи в messages[] Anthropic API

CREATE TABLE IF NOT EXISTS conversation_history (
    id SERIAL PRIMARY KEY,
    role VARCHAR(10) NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    tool_calls JSONB,          -- сохраняем tool_use вызовы если были
    tool_results JSONB,        -- результаты tool_use
    tokens_used INTEGER,       -- примерная оценка токенов
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Индекс для быстрой выборки последних N сообщений
CREATE INDEX IF NOT EXISTS idx_conversation_history_created
    ON conversation_history (created_at DESC);

-- Автоочистка: удаляем сообщения старше 4 часов (вызывается из scheduler)
-- Не используем pg_cron, вызываем из Python
