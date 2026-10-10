-- Migration 0028: protection_plans
--
-- An entry's protection (the stop and the take-profit tranche the risk manager
-- approved with it) existed only as prose in the executor's reasoning. When the
-- entry filled after its cycle ended, nothing placed that protection until the
-- next cycle. record_trade now keeps the plan as data, one row per entry order,
-- and the protection follow-up (tools.protection_followup) places it when the
-- fill lands.
--
-- status: planned (waiting for the fill), placed, covered (the shares were
-- already protected), refused (a check failed, see status_reason), failed (the
-- broker refused an order), expired (the entry did not fill, or the plan grew
-- too old to act on).

CREATE TABLE IF NOT EXISTS protection_plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    session_id      TEXT,
    entry_trade_id  INTEGER,
    entry_order_id  TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    entry_quantity  REAL    NOT NULL,
    stop_price      REAL    NOT NULL,
    stop_qty        REAL    NOT NULL,
    tp_limit_price  REAL,
    tp_qty          REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'planned',
    status_reason   TEXT,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_protection_plans_status ON protection_plans(status);

CREATE INDEX IF NOT EXISTS idx_protection_plans_entry ON protection_plans(entry_order_id);
