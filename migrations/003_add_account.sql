-- Миграция 003: Поле account для мульти-аккаунта Telegram

-- Добавляем столбец account (метка аккаунта: "основной", "9514" и т.д.)
ALTER TABLE messages ADD COLUMN IF NOT EXISTS account TEXT DEFAULT '';

-- Индекс для фильтрации по аккаунту
CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account);

-- Обновляем UNIQUE constraint: telegram_msg_id уникален в рамках (chat_id + account)
ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_telegram_msg_id_chat_id_key;
ALTER TABLE messages ADD CONSTRAINT messages_telegram_msg_id_chat_id_account_key
    UNIQUE(telegram_msg_id, chat_id, account);
