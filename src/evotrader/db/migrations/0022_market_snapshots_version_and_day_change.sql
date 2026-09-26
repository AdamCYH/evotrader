-- Migration 0022: keep the algorithm version and the day's move on each snapshot
--
-- The console's market panel is rebuilt from market_snapshots on every page
-- load. The live market-data response carries the algorithm version, the day's
-- change and the gap, but the table never stored them, so after a reload the
-- panel could not say which version produced the numbers or how the day stood.

ALTER TABLE market_snapshots ADD COLUMN algo_version TEXT;
ALTER TABLE market_snapshots ADD COLUMN daily_change_pct REAL;
ALTER TABLE market_snapshots ADD COLUMN gap_pct REAL;
