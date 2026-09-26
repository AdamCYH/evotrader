from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evotrader.agents.factory import (
    CachingLiteLLMClient,
    FilteredMcpToolset,
    create_news_sentiment_agent,
    create_strategy_agent,
)
from evotrader.config import AppConfig


@pytest.mark.asyncio
async def test_caching_client_separates_static_prefix_and_caches_history():
    """Verify the CachingLiteLLMClient wiring reaches litellm with breakpoints.

    Breakpoint-placement rules are covered exhaustively in
    ``test_anthropic_cache.py``; this test guards the ADK integration point —
    that ``LiteLlm.llm_client`` actually runs our transform before the call.
    """
    static_text = "You are the Strategy Agent. Make decisions."
    temporal_text = (
        "## Current Temporal Context\n- **Current time**: Wednesday, June 17, 2026, 10:00 AM ET"
    )
    full_content = f"{static_text}\n\n{temporal_text}"

    captured_kwargs = {}

    async def mock_super(self, model, messages, tools, **kwargs):
        captured_kwargs.update({"model": model, "messages": messages, **kwargs})
        return {"choices": [{"message": {"content": "ok"}}]}

    messages = [
        {"role": "system", "content": full_content},
        {"role": "user", "content": "Run trade analysis " + "x" * 5000},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "y" * 5000},
    ]

    client = CachingLiteLLMClient(AppConfig().settings.caching)
    with patch(
        "google.adk.models.lite_llm.LiteLLMClient.acompletion",
        new=mock_super,
    ):
        await client.acompletion(
            model="anthropic/claude-opus-5",
            messages=messages,
            tools=None,
        )

    sys_content = captured_kwargs["messages"][0]["content"]
    assert isinstance(sys_content, list), (
        "System content must be a list of blocks for Anthropic prompt caching"
    )
    assert len(sys_content) == 2, f"Expected 2 blocks (static and dynamic), got {len(sys_content)}"

    # Block 1 is the static prefix and carries the breakpoint (covers tools too).
    assert sys_content[0]["type"] == "text"
    assert sys_content[0]["text"] == static_text
    assert sys_content[0]["cache_control"] == {"type": "ephemeral"}

    # Block 2 is the volatile temporal context and must sit after the breakpoint.
    assert sys_content[1]["type"] == "text"
    assert "Wednesday, June 17, 2026" in sys_content[1]["text"]
    assert "cache_control" not in sys_content[1]

    # The conversation history must be cached as well — this is what stops the
    # growing tool-call history being re-billed at full price every turn.
    assert captured_kwargs["messages"][3]["cache_control"] == {"type": "ephemeral"}
    assert captured_kwargs["messages"][1]["cache_control"] == {"type": "ephemeral"}

    assert "prompt-caching-2024-07-31" in captured_kwargs["extra_headers"]["anthropic-beta"]


@pytest.mark.asyncio
async def test_caching_client_is_a_noop_when_disabled():
    from evotrader.models.config import CachingConfig

    captured = {}

    async def mock_super(self, model, messages, tools, **kwargs):
        captured.update({"messages": messages, **kwargs})
        return {}

    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "x" * 5000},
    ]
    client = CachingLiteLLMClient(CachingConfig(enabled=False))
    with patch(
        "google.adk.models.lite_llm.LiteLLMClient.acompletion",
        new=mock_super,
    ):
        await client.acompletion(model="anthropic/claude-opus-5", messages=messages, tools=None)

    assert captured["messages"][0]["content"] == "rules"
    assert "cache_control" not in captured["messages"][1]
    assert "extra_headers" not in captured


@pytest.mark.asyncio
async def test_filtered_mcp_toolset():
    """Verify FilteredMcpToolset filters exposed tool schemas for the LLM while calling underlying toolset."""
    mock_parent = MagicMock()
    tool1 = MagicMock()
    tool1.name = "cancel_option_order"
    tool2 = MagicMock()
    tool2.name = "place_option_order"
    tool3 = MagicMock()
    tool3.name = "cancel_equity_order"
    tool4 = MagicMock()
    tool4.name = "add_to_watchlist"

    mock_parent.get_tools = AsyncMock(return_value=[tool1, tool2, tool3, tool4])
    mock_parent._connection_params = None

    filtered = FilteredMcpToolset(
        mock_parent,
        allowed_tool_names={"cancel_option_order", "cancel_equity_order"},
    )

    tools = await filtered.get_tools()
    tool_names = [t.name for t in tools]

    assert tool_names == ["cancel_option_order", "cancel_equity_order"]
    mock_parent.get_tools.assert_called_once()


def test_agent_tool_schema_pruning():
    """Verify that News Sentiment and Strategy agents receive only their required tools."""
    config = AppConfig()

    # 1. Strategy Agent: only memory and learning tools.
    # Reads the tool set whichever runtime is configured — a hosted agent carries
    # `local_tools`, an LlmAgent carries `tools` — because the constraint is about
    # what the agent can reach, not which engine happens to serve it.
    strategy = create_strategy_agent(config)
    strategy_tools = getattr(strategy, "tools", None) or strategy.local_tools
    strategy_tool_names = [
        getattr(t, "__name__", getattr(t, "name", str(t))) for t in strategy_tools
    ]
    assert "get_active_algorithm" not in strategy_tool_names
    assert "get_performance_summary" not in strategy_tool_names
    assert "query_past_trades" in strategy_tool_names
    assert "query_user_notes" in strategy_tool_names
    assert "store_learning" in strategy_tool_names

    # Read-only market/position retrieval IS allowed, and is deliberate: a
    # hosted agent starts cold, so when prompt assembly drops a payload it
    # otherwise has no way to notice or recover (review 20260915_224744,
    # finding 4). What must never appear is anything that MUTATES state.
    assert "gather_market_data" in strategy_tool_names
    assert "get_open_positions" in strategy_tool_names

    # The actual safety constraint — strategy proposes, it never executes.
    # Asserted as a property rather than a name list so a newly added tool
    # cannot quietly slip through a stale enumeration.
    forbidden_prefixes = ("place_", "cancel_", "submit_", "update_order")
    mutating = [
        n
        for n in strategy_tool_names
        if n.startswith(forbidden_prefixes) or n in {"record_trade", "check_risk_limits"}
    ]
    assert not mutating, f"strategy agent must not reach mutating tools: {mutating}"

    # 2. News Sentiment Agent: only research MCP tools + trade history & earnings history
    news = create_news_sentiment_agent(config)
    news_tool_names = [getattr(t, "__name__", getattr(t, "name", str(t))) for t in news.tools]
    assert "get_earnings_history" in news_tool_names
    assert "get_trade_history" in news_tool_names
    assert "query_user_notes" in news_tool_names


@pytest.mark.asyncio
async def test_gather_option_chain_contract_curation():
    """Verify gather_option_chain returns curated delta bands without losing key fields."""
    from unittest.mock import patch

    from evotrader.agents.tools import gather_option_chain

    # Mock _call_mcp_tool responses
    async def mock_mcp(name, args):
        if name == "get_option_chains":
            return {"data": {"chains": [{"expiration_dates": ["2026-09-18"]}]}}
        elif name == "get_equity_quotes":
            return {
                "data": {
                    "results": [
                        {"quote": {"last_trade_price": 500.0, "updated_at": "2026-08-13T20:00:00Z"}}
                    ]
                }
            }
        elif name == "get_option_instruments":
            return {
                "data": {
                    "instruments": [
                        {
                            "id": "c1",
                            "type": "call",
                            "strike_price": 500.0,
                            "expiration_date": "2026-09-18",
                        },
                        {
                            "id": "c2",
                            "type": "call",
                            "strike_price": 505.0,
                            "expiration_date": "2026-09-18",
                        },
                        {
                            "id": "c3",
                            "type": "call",
                            "strike_price": 510.0,
                            "expiration_date": "2026-09-18",
                        },
                        {
                            "id": "p1",
                            "type": "put",
                            "strike_price": 500.0,
                            "expiration_date": "2026-09-18",
                        },
                        {
                            "id": "p2",
                            "type": "put",
                            "strike_price": 495.0,
                            "expiration_date": "2026-09-18",
                        },
                        {
                            "id": "p3",
                            "type": "put",
                            "strike_price": 490.0,
                            "expiration_date": "2026-09-18",
                        },
                    ]
                }
            }
        elif name == "get_option_quotes":
            return {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "instrument_id": "c1",
                                "bid_price": 10.0,
                                "ask_price": 10.5,
                                "delta": 0.50,
                                "open_interest": 500,
                                "volume": 200,
                            }
                        },
                        {
                            "quote": {
                                "instrument_id": "c2",
                                "bid_price": 7.0,
                                "ask_price": 7.5,
                                "delta": 0.40,
                                "open_interest": 400,
                                "volume": 150,
                            }
                        },
                        {
                            "quote": {
                                "instrument_id": "c3",
                                "bid_price": 5.0,
                                "ask_price": 5.5,
                                "delta": 0.30,
                                "open_interest": 300,
                                "volume": 100,
                            }
                        },
                        {
                            "quote": {
                                "instrument_id": "p1",
                                "bid_price": 9.5,
                                "ask_price": 10.0,
                                "delta": -0.50,
                                "open_interest": 500,
                                "volume": 200,
                            }
                        },
                        {
                            "quote": {
                                "instrument_id": "p2",
                                "bid_price": 6.5,
                                "ask_price": 7.0,
                                "delta": -0.40,
                                "open_interest": 400,
                                "volume": 150,
                            }
                        },
                        {
                            "quote": {
                                "instrument_id": "p3",
                                "bid_price": 4.5,
                                "ask_price": 5.0,
                                "delta": -0.30,
                                "open_interest": 300,
                                "volume": 100,
                            }
                        },
                    ]
                }
            }
        return None

    with patch("evotrader.agents.tools._call_mcp_tool", side_effect=mock_mcp):
        res = await gather_option_chain("QQQ")
        contracts = res.get("contracts", [])
        assert len(contracts) > 0

        # Verify all quantitative fields exist without loss
        for c in contracts:
            assert "option_id" in c
            assert "type" in c
            assert "strike" in c
            assert "bid" in c
            assert "ask" in c
            assert "delta" in c
            assert "volume" in c
            assert "open_interest" in c
            assert "dte" in c

        # Verify budget_picks is populated
        budget_picks = res.get("budget_picks", {})
        assert "call" in budget_picks
        assert "put" in budget_picks
        assert (
            budget_picks["call"]["option_id"] == "c3"
        )  # Cheapest ask (5.5) with delta >= 0.20 and OI >= 50
        assert (
            budget_picks["put"]["option_id"] == "p3"
        )  # Cheapest ask (5.0) with delta >= 0.20 and OI >= 50


def test_prefer_latest_sort_is_schema_driven():
    """Recency ordering is injected only when the tool's own schema offers it."""
    from unittest.mock import MagicMock

    from evotrader.agents.factory import _prefer_latest_sort
    from evotrader.models.config import CurationConfig

    supports = MagicMock()
    supports.name = "NEWS_SENTIMENT"
    supports.inputSchema = {
        "properties": {"sort": {"enum": ["LATEST", "RELEVANCE"]}, "tickers": {}}
    }

    args = _prefer_latest_sort({"tickers": "QQQ"}, supports, CurationConfig())
    assert args == {"tickers": "QQQ", "sort": "LATEST"}


def test_prefer_latest_respects_an_explicit_sort():
    from unittest.mock import MagicMock

    from evotrader.agents.factory import _prefer_latest_sort
    from evotrader.models.config import CurationConfig

    tool = MagicMock()
    tool.inputSchema = {"properties": {"sort": {"enum": ["LATEST", "RELEVANCE"]}}}

    args = _prefer_latest_sort({"sort": "RELEVANCE"}, tool, CurationConfig())
    assert args["sort"] == "RELEVANCE"


def test_prefer_latest_is_inert_when_unsupported():
    """Self-disabling on any server that doesn't offer the option."""
    from unittest.mock import MagicMock

    from evotrader.agents.factory import _prefer_latest_sort
    from evotrader.models.config import CurationConfig

    for schema in (
        {"properties": {"tickers": {}}},  # no sort param
        {"properties": {"sort": {"type": "string"}}},  # no enum
        {"properties": {"sort": {"enum": ["RELEVANCE"]}}},  # no LATEST
        {},
        None,
    ):
        tool = MagicMock()
        tool.inputSchema = schema
        assert _prefer_latest_sort({"a": 1}, tool, CurationConfig()) == {"a": 1}


def test_prefer_latest_can_be_disabled():
    from unittest.mock import MagicMock

    from evotrader.agents.factory import _prefer_latest_sort
    from evotrader.models.config import CurationConfig

    tool = MagicMock()
    tool.inputSchema = {"properties": {"sort": {"enum": ["LATEST"]}}}

    args = _prefer_latest_sort({}, tool, CurationConfig(news_prefer_latest=False))
    assert "sort" not in args


def test_provider_roles_are_derived_from_config_not_hardcoded():
    from evotrader.agents.factory import _provider_roles
    from evotrader.config import AppConfig

    config = AppConfig()
    config.settings.mcp.roles = {
        "trading": "my_broker",
        "research": ["feed_a", "feed_b"],
    }

    assert _provider_roles(config, "my_broker") == ("trading",)
    assert _provider_roles(config, "feed_a") == ("research",)
    assert _provider_roles(config, "feed_b") == ("research",)
    assert _provider_roles(config, "not_configured") == ()
