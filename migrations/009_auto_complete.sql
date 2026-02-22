-- v9: auto_complete_on_remind — напоминания автоматически закрываются после срабатывания
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS auto_complete_on_remind BOOLEAN DEFAULT FALSE;
