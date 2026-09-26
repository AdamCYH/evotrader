-- 0003_add_trades_options.sql
-- Add option columns and session_id to trades table for backward compatibility.
-- These were previously handled by ad-hoc migrations in connection.py.

ALTER TABLE trades ADD COLUMN option_id TEXT;
ALTER TABLE trades ADD COLUMN option_type TEXT;
ALTER TABLE trades ADD COLUMN strike REAL;
ALTER TABLE trades ADD COLUMN expiration TEXT;
ALTER TABLE trades ADD COLUMN session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
