-- Migration 0030: signal_attribution.strategy_instruction_version
--
-- The record kept the algorithm version of each call and not the strategy
-- agent's instruction version, so the llm and final witnesses could not be
-- split by the instructions that produced them. Filled by the harness from the
-- version the cycle ran on (agents.factory.refresh_instructions), never by the
-- agent. NULL on rows written before this column existed and on rows the
-- strategy agent did not write (agent_absent).

ALTER TABLE signal_attribution ADD COLUMN strategy_instruction_version TEXT;
