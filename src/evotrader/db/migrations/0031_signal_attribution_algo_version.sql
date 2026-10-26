-- Migration 0031: signal_attribution.algo_version
--
-- Each row now says which algorithm version made the call, so the record can
-- be split by it: which channel authors a call changes with the version (a
-- vote scale, a weight), and a record that pools versions answers a different
-- question than the version running now. Filled from the market snapshot of
-- the same session (SignalAttributionStore.backfill_algo_version), with the
-- "composite_" prefix the snapshot carries removed. NULL where no snapshot
-- named one.

ALTER TABLE signal_attribution ADD COLUMN algo_version TEXT;
