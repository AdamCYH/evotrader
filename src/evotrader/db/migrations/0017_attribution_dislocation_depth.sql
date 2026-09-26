-- Migration 0017: Record dislocation depth on signal attribution rows.
--
-- The open question "is 0.165 ATR the right trigger for intraday_vwap_zscore,
-- or should it be 0.30?" is currently unanswerable, and the obvious answer is
-- wrong in an instructive way: the channel is 2-for-2 on its firings and those
-- winners sat at 0.20 and 0.29 ATR. Both are MODEST deviations, so naively
-- raising the trigger to filter noise would have excluded the two largest
-- winners on record (+20.7%, +14.9%).
--
-- n=2 cannot calibrate anything, and the 2-year backtest cannot resolve the
-- difference either — the measured placebo floor puts the minimum detectable
-- effect at ~3.72pp. What is missing is not a better parameter search but a
-- measurement: per-firing outcomes keyed on the PHYSICAL quantity (ATR of VWAP
-- deviation) rather than the rescaled z-score, which the entry_z/min_std
-- degeneracy makes unit-ambiguous anyway.
--
-- `deviation_atr` is how far price sat from session VWAP, in ATRs.
-- `trigger_atr` is the firing threshold in effect for that same cycle, so a
-- later change to entry_z or min_std does not silently reinterpret old rows.
-- Storing both makes each row self-describing: bucketing realised forward
-- returns by depth needs no assumption about the config at the time.
--
-- Both are NULL for cycles where the authoring channel was not
-- intraday_vwap_zscore, and NULL correctly reads as "not applicable" rather
-- than zero.

ALTER TABLE signal_attribution ADD COLUMN deviation_atr REAL;
ALTER TABLE signal_attribution ADD COLUMN trigger_atr REAL;
