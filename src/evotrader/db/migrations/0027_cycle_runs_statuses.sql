-- Migration 0027: cycle_runs.status accepts every status the code writes
--
-- 0001 allowed RUNNING, SUCCESS and FAILED only. The evolution services also
-- write TIMED_OUT, CANCELLED and SKIPPED (a run abandoned for an exhausted
-- subscription quota). Each of those final updates raised an IntegrityError
-- that the caller caught and logged, so the run stayed RUNNING for good. The
-- scheduler now records a scheduled run that could not start as SKIPPED too.
--
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt
-- with every row copied as it is. A copy left by an interrupted earlier run is
-- dropped first. The copy, the drop and the rename run in one transaction
-- (the runner commits after the last statement).

DROP TABLE IF EXISTS cycle_runs_new;

CREATE TABLE cycle_runs_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL UNIQUE,
    timestamp   TEXT    NOT NULL,
    cycle_type  TEXT    NOT NULL CHECK (cycle_type IN ('TRADING', 'EVOLUTION')),
    status      TEXT    NOT NULL CHECK (status IN (
                    'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'CANCELLED', 'TIMED_OUT')),
    error       TEXT,
    summary     TEXT
);

INSERT INTO cycle_runs_new (id, session_id, timestamp, cycle_type, status, error, summary)
SELECT id, session_id, timestamp, cycle_type, status, error, summary FROM cycle_runs;

DROP TABLE cycle_runs;

ALTER TABLE cycle_runs_new RENAME TO cycle_runs;

CREATE INDEX IF NOT EXISTS idx_cycle_runs_session ON cycle_runs(session_id);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_ts ON cycle_runs(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_type_ts ON cycle_runs(cycle_type, timestamp DESC);
