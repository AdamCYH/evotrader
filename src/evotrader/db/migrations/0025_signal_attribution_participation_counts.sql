-- The two counts the participation scale was computed from.
--
-- NOTE the runner splits this file on the semicolon character, so a semicolon
-- inside a comment is executed as SQL. Do not use one in a comment.
--
-- The scale is (numerator / denominator) / 0.4 when that ratio is under 0.4,
-- else 1.0, counting only channels with weight in the regime. With the two
-- counts beside participation_scale (migration 0024) a row says what the scale
-- was made of. All four are filled from the cycle's own market-data record
-- after the cycle (SignalAttributionStore.backfill_participation), not copied
-- by the strategy agent.
ALTER TABLE signal_attribution ADD COLUMN participation_numerator INTEGER;

ALTER TABLE signal_attribution ADD COLUMN participation_denominator INTEGER;
