-- EvoTrader — Migration 0005
-- Add persistent pending_orders table.
--
-- When the execution agent places a limit order that is not immediately
-- filled, the full trade proposal is persisted here instead of only
-- living in an in-memory dict (which is lost on process restart).
-- On each cycle start, pending orders are checked against the broker
-- and journaled once they reach a terminal state.

CREATE TABLE IF NOT EXISTS pending_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT    NOT NULL UNIQUE,
    session_id   TEXT,
    trade_json   TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'PENDING',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pending_orders_order_id ON pending_orders(order_id);
CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders(status);
