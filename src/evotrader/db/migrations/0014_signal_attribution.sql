-- Migration 0014: Per-witness signal attribution
--
-- The Evolution Agent could previously only score the blended composite. When a
-- month lost money it could not tell WHICH input was wrong, so every proposed
-- fix was a guess.
--
-- This table records each witness's directional call separately, every cycle,
-- traded or not. Rows with no trade are the valuable ones: they are the control
-- group. Forward returns are backfilled once the horizon elapses, which is what
-- makes per-source scoring and conviction calibration possible.

CREATE TABLE IF NOT EXISTS signal_attribution (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    session_id          TEXT,
    ticker              TEXT    NOT NULL,

    -- Witness 1: price-derived algorithm
    algo_direction      REAL,
    algo_strength       REAL,
    algo_author         TEXT,
    participation_ratio REAL,

    -- Witness 2: news / catalyst
    news_direction      REAL,
    news_strength       REAL,
    catalyst            TEXT,

    -- Witness 3: LLM reasoning
    llm_direction       REAL,
    llm_conviction      REAL,
    mechanism           TEXT,
    priced_in_check     TEXT,   -- JSON: known_fact, when_learned, mechanism, falsifier

    -- Resulting decision
    final_direction     REAL,
    final_conviction    REAL,
    risk_budget         REAL,
    position_size       REAL,
    traded              INTEGER NOT NULL DEFAULT 0,

    -- Backfilled once the horizon elapses. NULL until then
    price_at_decision   REAL,
    forward_return_1d   REAL,
    forward_return_5d   REAL,
    scored_at           TEXT,

    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Scoring sweeps read unscored rows past their horizon.
CREATE INDEX IF NOT EXISTS idx_attribution_unscored
    ON signal_attribution(scored_at, timestamp);

-- Per-source and calibration queries read by time.
CREATE INDEX IF NOT EXISTS idx_attribution_ts
    ON signal_attribution(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_attribution_session
    ON signal_attribution(session_id);
