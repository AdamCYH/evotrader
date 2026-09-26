-- Where a trade row's FILLED status came from.
--
-- NOTE for future migrations: the runner splits this file on the semicolon
-- character, so a semicolon inside a comment is treated as a statement break
-- and the rest of the comment is executed as SQL. Do not use one in a comment.
--
-- On 2026-09-25 the executor called record_trade with "status": "FILLED" for a
-- resting stop the broker had answered as state=unconfirmed with no executions.
-- The journal booked realized P&L against real lots, opened a phantom short and
-- rebased the cost basis. Three later cycles read pending_count 0 and tried to
-- re-place a stop the broker already held. Nothing in the row recorded that its
-- FILLED status was an executor claim rather than broker evidence, so
-- reconciliation had no way to single those rows out and re-ask.
--
--   'broker'         -- the broker reported state=filled (evidence being
--                      average_price or cumulative_quantity), or
--                      reconciliation promoted the row.
--   'executor_claim' -- the status came from the executor payload with no fill
--                      evidence. Re-checkable, and the broker answer overrides.
--   'sync'           -- synthesised by reconciliation to match broker position.
--
-- NULL means the row predates this column. Treat it as unverified, not as
-- broker-confirmed.
ALTER TABLE trades ADD COLUMN fill_source TEXT;

CREATE INDEX IF NOT EXISTS idx_trades_fill_source
    ON trades(fill_source, order_status);
