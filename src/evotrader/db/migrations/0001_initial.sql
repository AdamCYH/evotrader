-- EvoTrader — SQLite Schema
-- Baseline schema (0001_initial)

-- ═══════════════════════════════════════════════════════════════
-- Trade Journal: Every trade decision, execution, and outcome
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT    NOT NULL,
    ticker            TEXT    NOT NULL DEFAULT 'SPY',

    -- Position
    direction         TEXT    NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    action            TEXT    NOT NULL CHECK (action IN ('OPEN', 'CLOSE', 'STOP_LOSS', 'TAKE_PROFIT')),
    quantity          REAL    NOT NULL CHECK (quantity > 0),
    price             REAL    NOT NULL CHECK (price > 0),
    order_type        TEXT    NOT NULL,
    fill_price        REAL,
    slippage          REAL,

    -- Option Details (optional, nullable)
    option_id         TEXT,
    option_type       TEXT,
    strike            REAL,
    expiration        TEXT,

    -- Decision context
    algo_version      TEXT    NOT NULL,
    regime            TEXT    NOT NULL,
    algo_signal       REAL    NOT NULL,
    llm_signal        REAL,
    hybrid_score      REAL    NOT NULL,
    confidence        REAL    NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reasoning         TEXT    NOT NULL,

    -- Outcome (filled when position closes)
    related_trade_id  INTEGER REFERENCES trades(id),
    realized_pnl      REAL,
    holding_period_s  INTEGER,

    -- Market state at decision time (JSON blob)
    market_snapshot   TEXT    NOT NULL,

    -- Cycle linkage
    session_id        TEXT,   -- Links trade to the agent cycle that created it

    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_ticker    ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_action    ON trades(action);
CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades(direction);
CREATE INDEX IF NOT EXISTS idx_trades_related   ON trades(related_trade_id);


-- ═══════════════════════════════════════════════════════════════
-- Daily Metrics: Aggregated performance per trading day
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_metrics (
    date               TEXT PRIMARY KEY,
    portfolio_value    REAL    NOT NULL,
    cash_balance       REAL    NOT NULL,
    daily_pnl          REAL    NOT NULL,
    daily_return_pct   REAL    NOT NULL,
    cumulative_return  REAL    NOT NULL DEFAULT 0,
    win_count          INTEGER NOT NULL DEFAULT 0,
    loss_count         INTEGER NOT NULL DEFAULT 0,
    win_rate           REAL,
    avg_win            REAL,
    avg_loss           REAL,
    profit_factor      REAL,
    sharpe_30d         REAL,
    sortino_30d        REAL,
    max_drawdown       REAL    NOT NULL DEFAULT 0,
    algo_version       TEXT    NOT NULL DEFAULT '',
    regime_summary     TEXT,   -- JSON: {"trending_bull": 5, "range_bound": 3, ...}
    trades_count       INTEGER NOT NULL DEFAULT 0
);


-- ═══════════════════════════════════════════════════════════════
-- Evolution Log: Every algorithm/instruction change
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS evolution_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT    NOT NULL,
    change_type       TEXT    NOT NULL CHECK (change_type IN (
                          'ALGORITHM_PARAMS', 'ALGORITHM_NEW',
                          'INSTRUCTION_UPDATE', 'REGIME_WEIGHTS',
                          'CODE_REVIEW', 'KNOWLEDGE_UPDATE')),
    risk_level        TEXT    NOT NULL DEFAULT 'LOW' CHECK (risk_level IN (
                          'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    target_component  TEXT    NOT NULL,
    old_version       TEXT    NOT NULL,
    new_version       TEXT    NOT NULL,
    reasoning         TEXT    NOT NULL,
    expected_impact   TEXT,
    metrics_before    TEXT    NOT NULL,  -- JSON
    metrics_after     TEXT,              -- JSON (backtest results)
    code_diffs        TEXT,              -- JSON (for CODE_REVIEW proposals)
    status            TEXT    NOT NULL DEFAULT 'PROPOSED' CHECK (status IN (
                          'PROPOSED', 'BACKTESTED', 'VALIDATED', 'ACTIVE',
                          'ROLLED_BACK', 'ARCHIVED', 'PENDING_REVIEW')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_evolution_status ON evolution_log(status);
CREATE INDEX IF NOT EXISTS idx_evolution_type   ON evolution_log(change_type);


-- ═══════════════════════════════════════════════════════════════
-- Configuration Snapshots: Audit trail of config changes
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS config_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    config_yaml TEXT    NOT NULL,
    reason      TEXT    NOT NULL
);


-- ═══════════════════════════════════════════════════════════════
-- Audit Log: Every tool call for compliance
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    agent_name  TEXT    NOT NULL,
    tool_name   TEXT    NOT NULL,
    arguments   TEXT,   -- JSON
    result      TEXT,   -- JSON (truncated for large results)
    duration_ms INTEGER,
    success     INTEGER NOT NULL DEFAULT 1  -- 0 = failed
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_tool      ON audit_log(tool_name);


-- ═══════════════════════════════════════════════════════════════
-- Agent Thought Log: Persistent store of agent reflections and decisions
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS agent_thought_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    session_id  TEXT,
    agent_name  TEXT    NOT NULL,
    event_type  TEXT    NOT NULL CHECK (event_type IN ('thought', 'tool_call', 'tool_response', 'cycle_complete')),
    content     TEXT    NOT NULL,
    meta        TEXT              -- JSON string with details (tool name, args, response, success, etc.)
);

CREATE INDEX IF NOT EXISTS idx_thought_timestamp ON agent_thought_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_thought_session   ON agent_thought_log(session_id);


-- ═══════════════════════════════════════════════════════════════
-- Cycle Runs: Track both trading and evolution status and errors
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS cycle_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL UNIQUE,
    timestamp   TEXT    NOT NULL,
    cycle_type  TEXT    NOT NULL CHECK (cycle_type IN ('TRADING', 'EVOLUTION')),
    status      TEXT    NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    error       TEXT,
    summary     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_session ON cycle_runs(session_id);


-- Stores a timeseries of historical market data and technical indicators 
-- evaluated by the agent. Crucial for live technical chart rendering.
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    ticker TEXT NOT NULL,
    close_price REAL,
    rsi_14 REAL,
    bollinger_upper REAL,
    bollinger_middle REAL,
    bollinger_lower REAL,
    bollinger_width REAL,
    vwap REAL,
    vwap_dist REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_ts ON market_snapshots(timestamp);
