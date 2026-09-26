-- Migration 0021: mark attribution rows written without the strategy agent
--
-- Rows are written by the strategy agent, so a cycle that never reached it left
-- no row at all. On 2026-09-24 at 11:30 ET a model-provider 503 ended the cycle
-- after the market data was gathered, and the algorithm's call for that cycle
-- (composite +0.2050 long, with swing_failure_reversal's second-ever firing
-- inside it) dropped out of the calibration record.
--
-- The post-cycle hook now writes the algorithm's call from the snapshot the
-- system stored for that session. agent_absent = 1 marks such rows, so the
-- calibration counts them for the algorithm and leaves the agent-side
-- witnesses (llm, final) unscored rather than reading them as a flat call.

ALTER TABLE signal_attribution ADD COLUMN agent_absent INTEGER NOT NULL DEFAULT 0;
