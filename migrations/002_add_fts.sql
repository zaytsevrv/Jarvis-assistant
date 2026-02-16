-- Миграция 002: Полнотекстовый поиск (русская морфология)

-- tsvector столбец с автоматическим обновлением
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('russian', coalesce(text, ''))) STORED;

-- GIN-индекс для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_messages_tsv ON messages USING GIN(tsv);

-- Индекс по sender_name для поиска по контактам
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_name);
