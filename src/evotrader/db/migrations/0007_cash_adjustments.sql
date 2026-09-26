-- Migration: Add cash_adjustments table for deposit/withdrawal tracking.
--
-- The equity curve currently uses raw portfolio_value from Robinhood,
-- which includes deposits and withdrawals. A $500 deposit looks like
-- a $500 gain. This table lets the user log external cash flows so
-- the equity curve can subtract them and show actual trading performance.
--
-- Convention: positive amount = deposit, negative = withdrawal.
-- This migration is idempotent (CREATE TABLE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS cash_adjustments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT    NOT NULL,
    amount     REAL    NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cash_adjustments_date ON cash_adjustments(date);
