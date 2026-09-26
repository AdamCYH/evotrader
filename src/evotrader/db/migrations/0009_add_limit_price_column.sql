-- Migration: Add explicit limit_price column and fix trade price semantics.
--
-- Previously, the `price` column was overloaded: it stored the agent's
-- requested limit price at initial recording, then was supposed to be
-- updated to the fill price on reconciliation. The `fill_price` column
-- was also contaminated with the limit price due to a tools.py fallback.
--
-- This migration:
--   1. Adds a `limit_price` column to explicitly store the agent's
--      requested price, separate from the execution price.
--   2. Backfills `limit_price` from the current `price` column (which
--      currently holds the limit price for most trades).
--
-- After this migration:
--   - `limit_price`: The price the agent requested (limit/stop price)
--   - `fill_price`:  The actual broker fill price
--   - `price`:       The effective execution price (= fill_price when
--                    available, otherwise limit_price)

ALTER TABLE trades ADD COLUMN limit_price REAL;

-- Backfill: set limit_price = current price for all existing trades.
-- This preserves the original requested price before we fix `price`
-- to reflect the actual execution price.
UPDATE trades SET limit_price = price;
