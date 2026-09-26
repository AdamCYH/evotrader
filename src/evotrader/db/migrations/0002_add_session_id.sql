-- 0002_add_session_id_evolution.sql
-- Safely add session_id column if it doesn't already exist.
-- SQLite ALTER TABLE ADD COLUMN succeeds even if the table has data, but fails if column already exists.
-- However, we must ensure we don't crash if the column exists. But since this is a controlled migration,
-- it will only run if 0002 is not in schema_version.

ALTER TABLE evolution_log ADD COLUMN session_id TEXT;
