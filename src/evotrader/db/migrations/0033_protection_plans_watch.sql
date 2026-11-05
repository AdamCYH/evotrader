-- Migration 0033: protection_plans.watch
--
-- A plan used to end at its first check: covered (the shares were already
-- under a stop or a take-profit) or placed. A protective order that ended
-- after that moment went unseen until the next cycle. A plan now stays
-- 'watching' for the follow-up's window and ends 'done'. ``watch`` holds, as
-- JSON, the protective orders it watches (each with its role, quantity and
-- level) and the ones it has put back.

ALTER TABLE protection_plans ADD COLUMN watch TEXT;
