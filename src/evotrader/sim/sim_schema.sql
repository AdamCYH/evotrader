-- Simulated brokerage account
CREATE TABLE IF NOT EXISTS sim_accounts (
    account_number  TEXT PRIMARY KEY,
    cash_balance    REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fund transfer history (deposits / withdrawals)
CREATE TABLE IF NOT EXISTS sim_transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number  TEXT NOT NULL REFERENCES sim_accounts(account_number),
    transfer_type   TEXT NOT NULL CHECK (transfer_type IN ('DEPOSIT', 'WITHDRAWAL')),
    amount          REAL NOT NULL CHECK (amount > 0),
    balance_after   REAL NOT NULL,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Order book (all orders placed through sim)
CREATE TABLE IF NOT EXISTS sim_orders (
    id              TEXT PRIMARY KEY,          -- UUID order ID
    account_number  TEXT NOT NULL REFERENCES sim_accounts(account_number),
    asset_type      TEXT NOT NULL CHECK (asset_type IN ('EQUITY', 'OPTION')),
    ticker          TEXT NOT NULL,             -- Underlying ticker
    option_id       TEXT,                      -- For options: the instrument ID
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type      TEXT NOT NULL CHECK (order_type IN ('market', 'limit', 'stop', 'stop_limit')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    limit_price     REAL,
    stop_price      REAL,
    time_in_force   TEXT,                      -- 'gfd' (day) or 'gtc'; NULL on older rows
    status          TEXT NOT NULL DEFAULT 'filled' CHECK (status IN (
                        'pending', 'filled', 'partially_filled', 'cancelled', 'rejected')),
    fill_price      REAL,
    filled_quantity REAL DEFAULT 0,
    total_cost      REAL,                     -- Total cash impact (+ for buys, - for sells)
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    filled_at       TEXT,
    
    -- Option-specific fields
    option_type     TEXT CHECK (option_type IN ('call', 'put')),
    strike          REAL,
    expiration      TEXT,
    position_effect TEXT CHECK (position_effect IN ('open', 'close'))
);

-- Current positions (updated on each fill)
CREATE TABLE IF NOT EXISTS sim_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number  TEXT NOT NULL REFERENCES sim_accounts(account_number),
    asset_type      TEXT NOT NULL CHECK (asset_type IN ('EQUITY', 'OPTION')),
    ticker          TEXT NOT NULL,
    option_id       TEXT,
    quantity        REAL NOT NULL,             -- Negative for short positions
    avg_cost_basis  REAL NOT NULL,
    current_price   REAL,
    last_updated    TEXT NOT NULL DEFAULT (datetime('now')),
    
    -- Option-specific
    option_type     TEXT CHECK (option_type IN ('call', 'put')),
    strike          REAL,
    expiration      TEXT,
    
    UNIQUE(account_number, asset_type, ticker, option_id)
);

CREATE INDEX IF NOT EXISTS idx_sim_orders_status ON sim_orders(status);
CREATE INDEX IF NOT EXISTS idx_sim_orders_ticker ON sim_orders(ticker);
CREATE INDEX IF NOT EXISTS idx_sim_positions_account ON sim_positions(account_number);
