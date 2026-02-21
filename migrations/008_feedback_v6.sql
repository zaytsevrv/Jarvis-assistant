-- Миграция 008: v6 — расширение classification_feedback для прозрачности и обратной связи
-- predicted_confidence — уровень уверенности AI при классификации
-- user_reason — причина одобрения/отклонения от владельца

ALTER TABLE classification_feedback
  ADD COLUMN IF NOT EXISTS predicted_confidence INTEGER,
  ADD COLUMN IF NOT EXISTS user_reason TEXT;
