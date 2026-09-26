-- Migration 0013: Performance indexes for high-frequency queries
--
-- 1. Index for foreign key lookup on trades(related_trade_id) to eliminate
--    automatic index construction / Bloom filters during get_open_trades()
CREATE INDEX IF NOT EXISTS idx_trades_related ON trades(related_trade_id);

-- 2. Composite index on trades(action, order_status, timestamp) to accelerate
--    FIFO open trade matching and filtering
CREATE INDEX IF NOT EXISTS idx_trades_action_status_ts ON trades(action, order_status, timestamp);

-- 3. Composite index on market_snapshots(ticker, timestamp DESC) for instant
--    latest technical snapshot retrieval
CREATE INDEX IF NOT EXISTS idx_snapshot_ticker_ts ON market_snapshots(ticker, timestamp DESC);

-- 4. Indexes on cycle_runs for chronological history pagination and type filtering
CREATE INDEX IF NOT EXISTS idx_cycle_runs_ts ON cycle_runs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cycle_runs_type_ts ON cycle_runs(cycle_type, timestamp DESC);

-- 5. Covering index for thought log aggregation per session
CREATE INDEX IF NOT EXISTS idx_thought_session_event ON agent_thought_log(session_id, event_type);
