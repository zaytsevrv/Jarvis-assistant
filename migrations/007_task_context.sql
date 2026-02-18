-- v4: контекст задач + мониторинг выполнения + deep links
-- sender_id/sender_name — кто написал сообщение, из которого создана задача
-- telegram_msg_id — для deep link на оригинальное сообщение
-- account — лейбл аккаунта (основной/9514)
-- track_completion — мониторить ли выполнение (для исходящих задач)
-- check_interval_days — как часто проверять (по умолчанию 3 дня)
-- last_checked_at — когда последний раз проверяли выполнение

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS sender_id BIGINT,
  ADD COLUMN IF NOT EXISTS sender_name TEXT,
  ADD COLUMN IF NOT EXISTS telegram_msg_id BIGINT,
  ADD COLUMN IF NOT EXISTS account TEXT,
  ADD COLUMN IF NOT EXISTS track_completion BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS check_interval_days INTEGER DEFAULT 3,
  ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;

-- Индекс для scheduler job check_tracked_tasks
CREATE INDEX IF NOT EXISTS idx_tasks_track_completion
  ON tasks (track_completion) WHERE track_completion = TRUE AND status = 'active';
