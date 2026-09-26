-- Migration 0012: Add dynamic indicators_json column to market_snapshots
--
-- Persists full dynamic indicator sets, including custom or self-evolved signals,
-- eliminating the need for schema changes when strategies add new indicators.

ALTER TABLE market_snapshots ADD COLUMN indicators_json TEXT;
