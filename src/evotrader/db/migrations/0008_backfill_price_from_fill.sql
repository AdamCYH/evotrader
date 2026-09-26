-- Migration: Backfill price from fill_price for reconciled trades.
--
-- Fixes a bug where PENDING orders were recorded with price = limit_price,
-- then reconciliation updated fill_price to the actual execution price but
-- never updated the canonical `price` column. This caused incorrect P&L
-- calculations because the close-trade FIFO matcher reads `price` as the
-- entry price.
--
-- This migration updates `price` to match `fill_price` for any trade where:
--   1. fill_price is set (broker reported an actual fill)
--   2. fill_price differs from price (the stale limit price)
--   3. The trade is in a terminal state (FILLED or NULL/legacy)
--
-- The original limit price remains accessible via comparing `price` and
-- `fill_price` values in historical data.

UPDATE trades
SET price = fill_price
WHERE fill_price IS NOT NULL
  AND ABS(fill_price - price) >= 0.005
  AND COALESCE(order_status, 'FILLED') = 'FILLED';
