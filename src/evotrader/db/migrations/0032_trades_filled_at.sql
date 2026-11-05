-- Migration 0032: trades.filled_at
--
-- When the broker filled the order: its last execution's time, or the order's
-- last transaction when it lists none. A row keeps the time it was recorded
-- (timestamp), which for a resting stop or take-profit is when it was PLACED;
-- the holding period was worked out from that, so a take-profit that filled
-- days after it was placed read as held for a few seconds. NULL until the
-- reconciliation sees the order filled, and for rows filled before this
-- column existed.

ALTER TABLE trades ADD COLUMN filled_at TEXT;
