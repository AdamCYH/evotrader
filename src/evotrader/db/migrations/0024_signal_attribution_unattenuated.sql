-- The composite before the hour was applied.
--
-- NOTE the runner splits this file on the semicolon character, so a semicolon
-- inside a comment is executed as SQL. Do not use one in a comment.
--
-- The low-participation attenuation scales the composite by how many channels
-- could speak, and that count changes with the clock, because intraday channels
-- are out of scope pre-market and after-hours. On 2026-09-25 MSTR held
-- identical channel votes all day and the composite read 0.3800 at 08:30 ET,
-- 0.2297 from 11:30 to 15:30, and 0.3231 at 17:00. Scoring a channel by
-- algo_strength across hours therefore mixes two different denominators.
--
-- algo_strength is unchanged and is still the traded number. These record what
-- it was before the hour, so cross-hour comparisons can be made on like terms.
ALTER TABLE signal_attribution ADD COLUMN composite_unattenuated REAL;

ALTER TABLE signal_attribution ADD COLUMN participation_scale REAL;
