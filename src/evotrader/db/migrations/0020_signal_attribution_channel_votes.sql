-- Migration 0020: record every channel's vote on each attribution row
--
-- signal_attribution stored one free-text algo_author written by the agent,
-- and get_signal_calibration's by_author could therefore only ever score the
-- channel that led the weighted sum. On 2026-09-23 at 11:30 ET the first-ever
-- confirmed reversal from swing_failure_reversal (+0.045) sat under momentum
-- (+0.274) and the row was filed as momentum, so that channel's call could
-- never be scored. The census had to be copied by hand from cycle payloads.
--
-- channel_votes holds a JSON object keyed by channel name, each entry carrying
-- value, weight, applicable and reason. It is filled from the market snapshot
-- the system itself stored for the same session, never transcribed by the
-- agent, and existing rows are backfilled the same way.

ALTER TABLE signal_attribution ADD COLUMN channel_votes TEXT;
