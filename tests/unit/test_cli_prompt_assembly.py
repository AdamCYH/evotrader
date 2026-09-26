"""The hosted agent's task prompt must not discard the market snapshot.

See: data/evolution/reviews/20260915_224744_cli_migration_prompt_assembly_drops_market_data.md

`_task_prompt` walks the session NEWEST-FIRST and used to `break` when a block
exceeded the remaining budget. Two facts combined to make that fire on the one
payload that matters:

1. `_MAX_TOOL_RESULT_CHARS` is charged per function_response PART, but the
   budget is charged per EVENT — and the orchestrator issues its data-gathering
   calls in parallel, so four tool responses arrive as parts of ONE event.
2. The market snapshot is emitted EARLIEST in a cycle, so a newest-first walk
   reaches it LAST.

Result, live on 2026-09-15 15:30Z: `gather_market_data` ran, and the strategy
agent opened its reply with "I was handed only the news report." It refused to
trade rather than guess, so the cycle cost nothing — but abstention was luck,
not design.
"""

from __future__ import annotations

import logging
import types
from typing import Any

from google.genai import types as genai_types

from evotrader.agents.cli_agent import (
    _CRITICAL_TOOL_RESULTS,
    _MAX_EVENT_BLOCK_CHARS,
    _MAX_PROMPT_CHARS,
    CliBackedAgent,
)


def _event(author: str, text: str) -> Any:
    return types.SimpleNamespace(
        author=author,
        content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
    )


def _ctx(events: list[Any] | None = None) -> Any:
    return types.SimpleNamespace(
        session=types.SimpleNamespace(events=events or []),
        user_content=None,
        invocation_id="inv-1",
        branch=None,
    )


def _agent() -> CliBackedAgent:
    from evotrader.models.config import CliRuntimeConfig

    return CliBackedAgent(
        name="strategy",
        backend=types.SimpleNamespace(run=None),
        runtime=CliRuntimeConfig(model="opus", billing="subscription"),
        instruction_text="You are the Strategy Agent.",
    )


def _tool_result(name: str, filler: int) -> str:
    return f'<tool_result name="{name}">' + ("x" * filler) + "</tool_result>"


class TestTheMarketSnapshotSurvives:
    def test_a_huge_newer_event_does_not_annihilate_the_snapshot(self) -> None:
        """The exact 2026-09-15 shape: a giant parallel-call event, newer, and
        the snapshot older and smaller behind it."""
        snapshot = _event("orchestrator", _tool_result("gather_market_data", 500))
        # A later event larger than the whole budget — e.g. the option chain
        # plus three siblings returning as parts of one event.
        giant = _event("orchestrator", _tool_result("gather_option_chain", _MAX_PROMPT_CHARS))
        news = _event("news_sentiment", _tool_result("get_news", 500))

        # Session order is oldest-first; the walk reverses it.
        prompt = _agent()._task_prompt(_ctx([snapshot, giant, news]))

        assert "gather_market_data" in prompt, (
            "the oldest, smallest, most decision-critical payload was dropped"
        )

    def test_an_oversized_event_is_truncated_not_dropped_whole(self) -> None:
        giant = _event(
            "orchestrator", _tool_result("gather_market_data", _MAX_EVENT_BLOCK_CHARS * 2)
        )
        prompt = _agent()._task_prompt(_ctx([giant]))
        assert "gather_market_data" in prompt
        assert "event truncated at" in prompt

    def test_no_single_event_can_consume_the_whole_budget(self) -> None:
        """The per-PART cap does not bound a multi-part event."""
        giant = _event(
            "orchestrator", _tool_result("gather_option_chain", _MAX_EVENT_BLOCK_CHARS * 3)
        )
        snapshot = _event("orchestrator", _tool_result("gather_market_data", 200))
        prompt = _agent()._task_prompt(_ctx([snapshot, giant]))
        assert "gather_market_data" in prompt
        assert len(prompt) <= _MAX_PROMPT_CHARS + _MAX_EVENT_BLOCK_CHARS

    def test_ordinary_sessions_keep_chronological_order(self) -> None:
        """The walk is newest-first but the prompt must read oldest-first."""
        a = _event("orchestrator", "FIRST")
        b = _event("news_sentiment", "SECOND")
        prompt = _agent()._task_prompt(_ctx([a, b]))
        assert prompt.index("FIRST") < prompt.index("SECOND")


class TestMissingCriticalResultsAreLoud:
    def test_absent_market_data_logs_an_error(self, caplog) -> None:
        news_only = _event("news_sentiment", _tool_result("get_news", 100))
        with caplog.at_level(logging.ERROR):
            _agent()._task_prompt(_ctx([news_only]))
        assert "MISSING critical tool result" in caplog.text
        assert "gather_market_data" in caplog.text

    def test_present_market_data_logs_no_error(self, caplog) -> None:
        snapshot = _event("orchestrator", _tool_result("gather_market_data", 100))
        with caplog.at_level(logging.ERROR):
            _agent()._task_prompt(_ctx([snapshot]))
        assert "MISSING critical tool result" not in caplog.text

    def test_gather_market_data_is_the_critical_one(self) -> None:
        assert "gather_market_data" in _CRITICAL_TOOL_RESULTS


class TestStrategyAgentCanRecoverTheDataItself:
    """Finding 4. Redundancy is the point — the agent should be able to
    re-read the snapshot when the pipe drops it."""

    def test_strategy_tools_include_read_only_retrieval(self) -> None:
        from evotrader.agents.factory import _STRATEGY_TOOLS

        names = {t.__name__ for t in _STRATEGY_TOOLS}
        assert "gather_market_data" in names
        assert "get_open_positions" in names

    def test_strategy_tools_cannot_move_money(self) -> None:
        """The sandbox rationale must survive the addition."""
        from evotrader.agents.factory import _STRATEGY_TOOLS

        names = {t.__name__ for t in _STRATEGY_TOOLS}
        forbidden = {n for n in names if n.startswith(("place_", "cancel_", "submit_"))}
        assert not forbidden
        assert "record_trade" not in names
