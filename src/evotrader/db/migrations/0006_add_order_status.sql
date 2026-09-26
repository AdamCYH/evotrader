-- Migration: Add order lifecycle tracking to the trades table.
--
-- Previously, pending (unfilled) limit orders were saved ONLY to the
-- pending_orders table and never reached the trades table. This caused:
--   1. The real decision context (algo_signal, regime, reasoning) to be lost
--   2. The reconciliation service to create regime='reconciliation' entries
--   3. The evolution agent to see all trades as forced broker-sync closes
--
-- Fix: All trades are now journaled immediately, with order_status tracking
-- their lifecycle (PENDING → FILLED / REJECTED / CANCELLED / FAILED).
--
-- This migration is idempotent — each ALTER TABLE ADD COLUMN is safe to
-- re-run because SQLite's "duplicate column name" error is caught by the
-- migration runner.

-- Track the broker order lifecycle state
ALTER TABLE trades ADD COLUMN order_status TEXT DEFAULT 'FILLED';

-- Store the broker order ID for linking pending → terminal state updates
ALTER TABLE trades ADD COLUMN order_id TEXT;

-- Store rejection/cancellation reasons from the broker so the evolution
-- agent can diagnose systematic issues (buying power, PDT, etc.)
ALTER TABLE trades ADD COLUMN broker_status_reason TEXT;

-- Index for efficiently finding trades by lifecycle state
CREATE INDEX IF NOT EXISTS idx_trades_order_status ON trades(order_status);

-- Index for looking up trades by broker order ID
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);
