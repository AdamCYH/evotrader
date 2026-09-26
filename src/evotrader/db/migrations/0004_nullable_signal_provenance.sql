-- Migration: Make algo_signal, hybrid_score nullable to preserve NULL vs 0.0 distinction.
-- Confidence stays NOT NULL (defaults to 1.0 in application code).
--
-- SQLite does not support ALTER COLUMN, so we must recreate the table.
-- This migration is idempotent: it cleans up any partial prior run first.
--
-- IMPORTANT: The INSERT uses COALESCE to backfill safe defaults for columns
-- that are NOT NULL in the new schema but may contain NULLs in legacy data.

PRAGMA foreign_keys=OFF;

-- Clean up from a previous failed attempt if trades_new was left behind.
DROP TABLE IF EXISTS trades_new;

CREATE TABLE trades_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT    NOT NULL,
    ticker            TEXT    NOT NULL,
    direction         TEXT    NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    action            TEXT    NOT NULL CHECK (action IN ('OPEN', 'CLOSE', 'STOP_LOSS', 'TAKE_PROFIT')),
    quantity          REAL    NOT NULL CHECK (quantity > 0),
    price             REAL    NOT NULL CHECK (price > 0),
    order_type        TEXT    NOT NULL,
    fill_price        REAL,
    slippage          REAL,

    -- Option Details (optional, nullable)
    option_id         TEXT,
    option_type       TEXT,
    strike            REAL,
    expiration        TEXT,

    -- Decision context (algo_signal, hybrid_score now nullable)
    algo_version      TEXT    NOT NULL,
    regime            TEXT    NOT NULL,
    algo_signal       REAL,
    llm_signal        REAL,
    hybrid_score      REAL,
    confidence        REAL    NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reasoning         TEXT    NOT NULL,

    -- Outcome (filled when position closes)
    related_trade_id  INTEGER REFERENCES trades_new(id),
    realized_pnl      REAL,
    holding_period_s  INTEGER,

    -- Market state at decision time (JSON blob)
    market_snapshot   TEXT    NOT NULL,

    -- Cycle linkage
    session_id        TEXT,

    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Copy data with COALESCE to backfill safe defaults for NOT NULL columns
-- that may contain NULLs in legacy data.
INSERT INTO trades_new (
    id, timestamp, ticker, direction, action, quantity, price, order_type,
    fill_price, slippage,
    option_id, option_type, strike, expiration,
    algo_version, regime, algo_signal, llm_signal, hybrid_score,
    confidence, reasoning,
    related_trade_id, realized_pnl, holding_period_s,
    market_snapshot, session_id, created_at
)
SELECT
    id, timestamp, ticker, direction, action, quantity, price, order_type,
    fill_price, slippage,
    option_id, option_type, strike, expiration,
    COALESCE(algo_version, 'unknown'),
    COALESCE(regime, 'unknown'),
    algo_signal,          -- now nullable, keep as-is
    llm_signal,
    hybrid_score,         -- now nullable, keep as-is
    COALESCE(confidence, 1.0),  -- backfill legacy NULLs with safe default
    COALESCE(reasoning, 'Automated trade execution'),
    related_trade_id, realized_pnl, holding_period_s,
    COALESCE(market_snapshot, '{}'),
    session_id,
    COALESCE(created_at, datetime('now'))
FROM trades;

DROP TABLE trades;

ALTER TABLE trades_new RENAME TO trades;

-- Recreate indexes from initial migration
CREATE INDEX IF NOT EXISTS idx_trades_ticker    ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_session   ON trades(session_id);

PRAGMA foreign_keys=ON;
