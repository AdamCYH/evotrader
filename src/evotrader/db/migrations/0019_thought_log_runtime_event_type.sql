-- Migration 0019: allow 'runtime' in agent_thought_log.event_type
--
-- Harness diagnostics from the Claude CLI backend (quota checks) were logged
-- as event_type='thought' with the content prefixed '[runtime] '. The backend
-- emits that check AFTER the agent's final report, so the LAST thought row for
-- an agent was the marker rather than its reasoning. get_final_thoughts ranks
-- by id DESC, so get_cycle_digest -- the evolution agent's primary review tool
-- -- showed strategy = '[runtime] quota ok' on 4 of 5 regular-hours cycles on
-- 2026-09-18. The reasoning was in the table the whole time. The digest hid it.
--
-- A diagnostic is not a thought, so it gets its own event_type and no consumer
-- has to match on a string prefix. The original CHECK constraint did not allow
-- one, and SQLite cannot alter a CHECK in place, so the table is rebuilt --
-- same approach as migration 0015. Columns and indexes are otherwise identical
-- to the 0001 definition.
--
-- The 97 rows already written as 'thought' with a '[runtime]' prefix are left
-- exactly as they are. Rewriting history to make a query simpler is the wrong
-- trade, so get_final_thoughts excludes both shapes instead.

CREATE TABLE IF NOT EXISTS agent_thought_log_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    session_id  TEXT,
    agent_name  TEXT    NOT NULL,
    event_type  TEXT    NOT NULL CHECK (event_type IN (
                    'thought', 'tool_call', 'tool_response',
                    'cycle_complete', 'runtime')),
    content     TEXT    NOT NULL,
    meta        TEXT
);

INSERT INTO agent_thought_log_new (
    id, timestamp, session_id, agent_name, event_type, content, meta)
SELECT
    id, timestamp, session_id, agent_name, event_type, content, meta
FROM agent_thought_log;

DROP TABLE agent_thought_log;

ALTER TABLE agent_thought_log_new RENAME TO agent_thought_log;

CREATE INDEX IF NOT EXISTS idx_thought_timestamp ON agent_thought_log(timestamp);

CREATE INDEX IF NOT EXISTS idx_thought_session ON agent_thought_log(session_id);

CREATE INDEX IF NOT EXISTS idx_thought_session_event ON agent_thought_log(session_id, event_type);
