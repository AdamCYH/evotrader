-- Migration 0011: Add algorithm signals, composite score, and market regime to market_snapshots
--
-- This migration enables the Market Technicals & Decision Signals panel on the web console
-- to display and chart live composite scores, sub-strategy breakdowns, and detected regimes.

ALTER TABLE market_snapshots ADD COLUMN composite_signal REAL;
ALTER TABLE market_snapshots ADD COLUMN regime TEXT;
ALTER TABLE market_snapshots ADD COLUMN regime_confidence REAL;
ALTER TABLE market_snapshots ADD COLUMN sub_signals_json TEXT;
