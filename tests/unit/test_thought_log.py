"""Tests for the agent thought logger database operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from evotrader.db.connection import Database
from evotrader.db.thought_log import ThoughtLogger


@pytest.fixture
def thought_logger(db: Database) -> ThoughtLogger:
    return ThoughtLogger(db)


class TestThoughtLogger:
    async def test_record_and_retrieve_thought(self, thought_logger: ThoughtLogger) -> None:
        # Record a simple thought
        inserted_id = await thought_logger.record_event(
            session_id="test_session_123",
            agent_name="market_intelligence",
            event_type="thought",
            content="RSI is 32, market seems range-bound.",
        )
        assert inserted_id > 0

        # Retrieve thoughts for this session
        thoughts = await thought_logger.get_recent_thoughts(limit=10, session_id="test_session_123")
        assert len(thoughts) == 1
        assert thoughts[0]["agent_name"] == "market_intelligence"
        assert thoughts[0]["event_type"] == "thought"
        assert thoughts[0]["content"] == "RSI is 32, market seems range-bound."

    async def test_record_and_retrieve_tool_call(self, thought_logger: ThoughtLogger) -> None:
        # Record a tool call
        inserted_id = await thought_logger.record_event(
            session_id="test_session_456",
            agent_name="strategy",
            event_type="tool_call",
            content="get_equity_historicals",
            meta={"args": {"symbol": "SPY", "span": "day"}},
        )
        assert inserted_id > 0

        # Retrieve thoughts
        thoughts = await thought_logger.get_recent_thoughts(limit=10, session_id="test_session_456")
        assert len(thoughts) == 1
        assert thoughts[0]["agent_name"] == "strategy"
        assert thoughts[0]["event_type"] == "tool_call"
        assert thoughts[0]["content"] == "get_equity_historicals"
        assert "args" in thoughts[0]["meta"]

    async def test_get_unique_sessions(self, thought_logger: ThoughtLogger) -> None:
        # Record a trading run
        await thought_logger.record_event(
            session_id="session_trading",
            agent_name="orchestrator",
            event_type="tool_response",
            content="record_trade",
        )

        # Record an evolution run
        await thought_logger.record_event(
            session_id="session_evolution",
            agent_name="evolution",
            event_type="thought",
            content="Thinking about prompt evolutionary mutations...",
        )

        sessions = await thought_logger.get_unique_sessions()
        assert len(sessions) >= 2

        # Find session_trading
        trading_session = next(s for s in sessions if s["session_id"] == "session_trading")
        assert trading_session["has_trades"] == 1
        assert trading_session["is_evolution"] == 0

        # Find session_evolution
        evolution_session = next(s for s in sessions if s["session_id"] == "session_evolution")
        assert evolution_session["has_trades"] == 0
        assert evolution_session["is_evolution"] == 1

    async def test_get_unique_sessions_with_cycle_complete(
        self, thought_logger: ThoughtLogger
    ) -> None:
        # Record a complete cycle event
        await thought_logger.record_event(
            session_id="session_complete_cycle",
            agent_name="orchestrator",
            event_type="cycle_complete",
            content="Cycle finished with status: complete",
            meta={
                "status": "complete",
                "stages_completed": ["market_intelligence", "news_sentiment", "strategy"],
                "stages_skipped": ["risk_manager", "execution"],
                "duration_ms": 1200,
                "error": None,
            },
        )

        sessions = await thought_logger.get_unique_sessions()
        session_data = next(s for s in sessions if s["session_id"] == "session_complete_cycle")
        assert session_data["cycle_status"] == "complete"
        assert session_data["stages_completed"] == [
            "market_intelligence",
            "news_sentiment",
            "strategy",
        ]

    async def test_get_recent_thoughts_filtered(self, thought_logger: ThoughtLogger) -> None:
        # Record thoughts from different agents and different event types
        await thought_logger.record_event(
            session_id="session_filter",
            agent_name="orchestrator",
            event_type="thought",
            content="orchestrator thought",
        )
        await thought_logger.record_event(
            session_id="session_filter",
            agent_name="strategy",
            event_type="thought",
            content="strategy thought",
        )
        await thought_logger.record_event(
            session_id="session_filter",
            agent_name="strategy",
            event_type="tool_call",
            content="strategy tool call",
        )

        # 1. Filter by agent_name
        res = await thought_logger.get_recent_thoughts(
            session_id="session_filter", agent_name="strategy"
        )
        assert len(res) == 2
        assert all(r["agent_name"] == "strategy" for r in res)

        # 2. Filter by event_type
        res = await thought_logger.get_recent_thoughts(
            session_id="session_filter", event_type="thought"
        )
        assert len(res) == 2
        assert all(r["event_type"] == "thought" for r in res)

        # 3. Filter by agent_name and event_type
        res = await thought_logger.get_recent_thoughts(
            session_id="session_filter", agent_name="strategy", event_type="tool_call"
        )
        assert len(res) == 1
        assert res[0]["content"] == "strategy tool call"

    async def test_get_final_thoughts(self, thought_logger: ThoughtLogger) -> None:
        # Record chronological events for different agents in a session
        # Agent A: thought 1, then thought 2
        await thought_logger.record_event(
            session_id="session_final",
            agent_name="orchestrator",
            event_type="thought",
            content="orchestrator thought 1",
        )
        await thought_logger.record_event(
            session_id="session_final",
            agent_name="orchestrator",
            event_type="thought",
            content="orchestrator thought 2",
        )
        # Agent B: thought 1, tool_call, then thought 2
        await thought_logger.record_event(
            session_id="session_final",
            agent_name="strategy",
            event_type="thought",
            content="strategy thought 1",
        )
        await thought_logger.record_event(
            session_id="session_final",
            agent_name="strategy",
            event_type="tool_call",
            content="some_tool",
        )
        await thought_logger.record_event(
            session_id="session_final",
            agent_name="strategy",
            event_type="thought",
            content="strategy thought 2",
        )

        # Retrieve final thoughts
        final_thoughts = await thought_logger.get_final_thoughts(session_id="session_final")

        # Should contain exactly one (the latest) thought per agent
        assert len(final_thoughts) == 2

        # Verify orchestrator final thought
        orch_thought = next(t for t in final_thoughts if t["agent_name"] == "orchestrator")
        assert orch_thought["content"] == "orchestrator thought 2"

        # Verify strategy final thought
        strat_thought = next(t for t in final_thoughts if t["agent_name"] == "strategy")
        assert strat_thought["content"] == "strategy thought 2"

    async def test_logging_mcp_toolset_error(self, tmp_path: Path) -> None:
        import json
        from unittest.mock import AsyncMock, MagicMock

        from google.adk.agents.readonly_context import ReadonlyContext
        from google.adk.sessions import Session
        from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams

        from evotrader.agents.factory import LoggingMcpToolset
        from evotrader.config import AppConfig

        config = AppConfig()
        config.db_dir = tmp_path

        db = Database(config.db_path)
        await db.initialize()

        params = StreamableHTTPConnectionParams(url="http://invalid-mcp-url/mcp")
        toolset = LoggingMcpToolset(
            connection_params=params,
            provider_name="robinhood_official",
            config=config,
        )

        # Mock the internal executor to raise an exception
        toolset._execute_with_session = AsyncMock(
            side_effect=ConnectionError("Failed to connect to MCP server")
        )

        # Build mock readonly context
        mock_session = Session(id="test_mcp_session", app_name="evotrader", user_id="trader")
        mock_inv_ctx = MagicMock()
        mock_inv_ctx.session = mock_session
        mock_inv_ctx.agent = MagicMock()
        mock_inv_ctx.agent.name = "market_intelligence"
        readonly_context = ReadonlyContext(mock_inv_ctx)

        # Call get_tools — should return empty list (graceful degradation) not raise
        result = await toolset.get_tools(readonly_context)
        assert result == []

        # Check DB to verify error events were still recorded
        logger_db = ThoughtLogger(db)
        thoughts = await logger_db.get_recent_thoughts(limit=10, session_id="test_mcp_session")
        assert len(thoughts) == 2

        # Tool response details (latest event is first in recent_thoughts order)
        assert thoughts[0]["event_type"] == "tool_response"
        assert thoughts[0]["content"] == "connect_robinhood_official"
        assert thoughts[0]["agent_name"] == "market_intelligence"
        meta_resp = json.loads(thoughts[0]["meta"])
        assert meta_resp["success"] == 0
        assert "Failed to connect to MCP server" in meta_resp["response"]["error"]

        # Tool call details
        assert thoughts[1]["event_type"] == "tool_call"
        assert thoughts[1]["content"] == "connect_robinhood_official"

        await db.close()
