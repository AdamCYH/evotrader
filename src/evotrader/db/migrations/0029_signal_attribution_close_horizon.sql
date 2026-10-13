-- Migration 0029: signal_attribution.forward_return_close
--
-- The scorer measured every call one session (and five sessions) after the
-- decision's date. Half the channels are intraday: their targets resolve, or
-- fail, by the same day's close, and measured a day later they read the next
-- session's move instead. forward_return_close is the return from
-- price_at_decision to the close of the decision's own session, in percent
-- like the other two columns. NULL for a decision made after that session's
-- close (it has no close ahead of it) and until the session's daily bar is
-- published.

ALTER TABLE signal_attribution ADD COLUMN forward_return_close REAL;
