-- Migration 0015: allow REJECTED in evolution_log.status
--
-- The web UI rejects proposals by calling
-- evolution_store.update_status(version, status="REJECTED") in three places
-- (instructions, algorithm params, and code reviews). But the original CHECK
-- constraint never included REJECTED, so every one of those writes raised
-- an IntegrityError.
--
-- EvolutionLogStore.update_status caught that exception and only logged it,
-- while the endpoint still returned {"status": "success"}. The result: the
-- reject button reported success and silently changed nothing, so rejected
-- proposals stayed PROPOSED forever and accumulated in the review queue.
--
-- This migration recreates the table with REJECTED and SUPERSEDED added.
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt.
-- Everything else is byte-identical to the 0001 definition plus the
-- session_id column added later.

CREATE TABLE IF NOT EXISTS evolution_log_new (
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
    metrics_before    TEXT    NOT NULL,
    metrics_after     TEXT,
    code_diffs        TEXT,
    status            TEXT    NOT NULL DEFAULT 'PROPOSED' CHECK (status IN (
                          'PROPOSED', 'BACKTESTED', 'VALIDATED', 'ACTIVE',
                          'ROLLED_BACK', 'ARCHIVED', 'PENDING_REVIEW',
                          'REJECTED', 'SUPERSEDED')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    session_id        TEXT
);

INSERT INTO evolution_log_new (
    id, timestamp, change_type, risk_level, target_component, old_version,
    new_version, reasoning, expected_impact, metrics_before, metrics_after,
    code_diffs, status, created_at, session_id)
SELECT
    id, timestamp, change_type, risk_level, target_component, old_version,
    new_version, reasoning, expected_impact, metrics_before, metrics_after,
    code_diffs, status, created_at, session_id
FROM evolution_log;

DROP TABLE evolution_log;

ALTER TABLE evolution_log_new RENAME TO evolution_log;
