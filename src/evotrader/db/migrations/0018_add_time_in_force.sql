-- Migration 0018: Persist time_in_force on trades.
--
-- A protective order placed without a duration defaults to a DAY order at the
-- broker and silently expires at the close. Stop #180 (2026-09-14) carried
-- time_in_force=gtc and rested three sessions. Stop #185 (2026-09-17) carried
-- none, expired overnight, and reconcile stamped it "Order cancelled" —
-- indistinguishable from an agent cancel. The next morning the strategy agent
-- read "the safety net isn't there", sold the 3-share MSTR long at $136.50,
-- and MSTR closed +16% at $153.44.
--
-- Recording the duration makes an expiring stop visible BEFORE it expires:
-- the coverage check can report "expires at close" instead of "covered".
-- NULL means the order predates this column or carried no duration.

ALTER TABLE trades ADD COLUMN time_in_force TEXT;
