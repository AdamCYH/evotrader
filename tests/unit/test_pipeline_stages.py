"""A pipeline stage must be recorded however ADK happened to reach the agent.

The cycle status gates on ``_MANDATORY_STAGES``, and the evolution agent reads
those rows when judging whether a cycle was healthy — so a stage that runs but is
not recorded is a silent data-quality problem, not a cosmetic one.

This broke when the strategy agent moved onto a CLI runtime: ADK invokes an
``LlmAgent`` sub-agent through a function call named after it, but a custom
``BaseAgent`` through ``transfer_to_agent``. Every hosted cycle recorded as
``partial`` with ``strategy`` missing, despite having produced a full decision.
"""

from __future__ import annotations

from evotrader.main import _MANDATORY_STAGES, _PIPELINE_STAGES, _stage_for_call


def test_a_direct_sub_agent_call_is_a_stage():
    assert _stage_for_call("strategy", {}) == "strategy"
    assert _stage_for_call("news_sentiment", None) == "news_sentiment"


def test_a_transferred_sub_agent_is_the_same_stage():
    """The shape ADK uses for a custom BaseAgent sub-agent."""
    assert _stage_for_call("transfer_to_agent", {"agent_name": "strategy"}) == "strategy"
    assert _stage_for_call("transfer_to_agent", {"agent_name": "execution"}) == "execution"


def test_a_transfer_to_something_that_is_not_a_stage_is_ignored():
    assert _stage_for_call("transfer_to_agent", {"agent_name": "some_helper"}) is None


def test_a_malformed_transfer_does_not_raise():
    assert _stage_for_call("transfer_to_agent", None) is None
    assert _stage_for_call("transfer_to_agent", {}) is None


def test_ordinary_tools_are_not_stages():
    assert _stage_for_call("get_option_quotes", {}) is None
    assert _stage_for_call("record_trade", {"ticker": "MSTR"}) is None


def test_both_invocation_shapes_satisfy_the_mandatory_set():
    """A hosted strategy agent must be able to produce a `complete` cycle."""
    hosted = {
        _stage_for_call("gather_market_data", {}),
        _stage_for_call("transfer_to_agent", {"agent_name": "strategy"}),
    }
    assert _MANDATORY_STAGES.issubset(hosted)

    api = {
        _stage_for_call("gather_market_data", {}),
        _stage_for_call("strategy", {}),
    }
    assert _MANDATORY_STAGES.issubset(api)


def test_every_mandatory_stage_is_a_known_pipeline_stage():
    assert set(_PIPELINE_STAGES) >= _MANDATORY_STAGES
