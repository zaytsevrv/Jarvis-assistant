-- Миграция 005: Подготовка к pgvector (Фаза 4)
-- Добавляем пустую колонку embedding для будущего семантического поиска
-- Колонка НЕ заполняется сейчас — только резервируем место в схеме

-- Пробуем создать расширение (если доступно на сервере)
-- Если pgvector не установлен — колонка будет JSONB как fallback
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
    -- Если расширение создалось — добавляем vector-колонку
    ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding vector(1536);
    RAISE NOTICE 'pgvector: колонка embedding vector(1536) добавлена';
EXCEPTION WHEN OTHERS THEN
    -- pgvector не установлен — ничего не делаем, добавим позже
    RAISE NOTICE 'pgvector не установлен — пропускаем колонку embedding';
END $$;
