from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.adk.models import Gemini
from google.adk.models.lite_llm import LiteLlm

from evotrader.agents.factory import (
    create_execution_agent,
    create_news_sentiment_agent,
    create_orchestrator_agent,
    create_risk_manager_agent,
    create_strategy_agent,
)
from evotrader.config import AppConfig


def _llm_agent(agent):
    """The LlmAgent that actually talks to a model.

    An agent hosted on a CLI runtime is not an ``LlmAgent`` — it drives an
    external harness and never enters ADK's LLM flow, so ADK-specific settings
    like transfer flags and retry options do not apply to it directly. They still
    matter for its API fallback, which is what runs whenever the harness is
    unavailable or out of quota, so that is what these checks resolve to.
    """
    fallback = getattr(agent, "fallback_agent", None)
    return fallback if fallback is not None else agent


def test_agent_model_resolution_and_retry_config():
    """Verify that Gemini agents are instantiated with correct HttpRetryOptions and other agents use LiteLlm."""
    config = AppConfig()

    # Helper to check an agent's resolved model
    def check_agent_model(agent, name):
        model = _llm_agent(agent).canonical_model
        if isinstance(model, Gemini):
            assert model.retry_options is not None, f"{name} Gemini model is missing retry_options"
            assert model.retry_options.attempts == 4, f"{name} attempts != 4"
            assert model.retry_options.initial_delay == 1.0, f"{name} initial_delay != 1.0"
            assert model.retry_options.max_delay == 10.0, f"{name} max_delay != 10.0"
        else:
            assert isinstance(model, LiteLlm), f"{name} is not Gemini or LiteLlm"

    # 1. Orchestrator
    orchestrator = create_orchestrator_agent(config)
    check_agent_model(orchestrator, "orchestrator")

    # 2. Risk Manager
    risk_manager = create_risk_manager_agent(config)
    check_agent_model(risk_manager, "risk_manager")

    # 3. News Sentiment
    news_sentiment = create_news_sentiment_agent(config)
    check_agent_model(news_sentiment, "news_sentiment")

    # 4. Strategy
    strategy = create_strategy_agent(config)
    check_agent_model(strategy, "strategy")

    # 5. Execution
    execution = create_execution_agent(config)
    check_agent_model(execution, "execution")


@pytest.mark.asyncio
async def test_orchestrator_after_tool_callback():
    """Verify that orchestrator_after_tool_callback correctly halts the cycle on sub-agent error."""
    config = AppConfig()
    orchestrator = create_orchestrator_agent(config)

    # Extract the registered callback
    callback = orchestrator.after_tool_callback
    assert callback is not None

    mock_tool = MagicMock()
    mock_tool.name = "risk_manager"

    # Case A: Normal sub-agent response (no exception raised)
    res = await callback(
        tool=mock_tool,
        args={},
        tool_context=MagicMock(),
        tool_response="Risk analysis: Approved.",
    )
    assert res is None

    # Case B: Error response starting with 'Error running sub-agent:' (should return safety instructions)
    error_msg = "Error running sub-agent: 503 Service Unavailable"
    res_risk = await callback(
        tool=mock_tool,
        args={},
        tool_context=MagicMock(),
        tool_response=error_msg,
    )
    assert "MUST NOT proceed with executing this trade" in res_risk

    mock_tool.name = "news_sentiment"
    res_news = await callback(
        tool=mock_tool,
        args={},
        tool_context=MagicMock(),
        tool_response=error_msg,
    )
    assert "News sentiment analysis is unavailable" in res_news


@pytest.mark.asyncio
async def test_orchestrator_before_and_after_tool_callback_deduplication():
    """Verify that orchestrator_before_tool_callback/after_tool_callback deduplicates parallel tool calls."""
    from unittest.mock import patch

    config = AppConfig()
    orchestrator = create_orchestrator_agent(config)

    before_cb = orchestrator.before_tool_callback
    after_cb = orchestrator.after_tool_callback
    assert before_cb is not None
    assert after_cb is not None

    mock_tool = MagicMock()
    mock_tool.name = "gather_market_data"
    args = {"ticker": "QQQ"}

    # We will mock time.time() to control the deduplication interval
    with patch("time.time") as mock_time:
        mock_time.return_value = 100.0

        # 1. First call: execution should proceed (returns None)
        res1 = await before_cb(tool=mock_tool, args=args, tool_context=MagicMock())
        assert res1 is None

        # 2. Duplicate call within 2.0s: should return the default safety message if no after_tool_callback has run
        res2 = await before_cb(tool=mock_tool, args=args, tool_context=MagicMock())
        assert "Skipped duplicate parallel tool call" in res2

        # 3. Simulate after_tool_callback finishing for the first execution
        simulated_response = "Market data: QQQ = 480"
        await after_cb(
            tool=mock_tool, args=args, tool_context=MagicMock(), tool_response=simulated_response
        )

        # 4. Another duplicate call within 2.0s: should return the cached response
        res3 = await before_cb(tool=mock_tool, args=args, tool_context=MagicMock())
        assert res3 == simulated_response

        # 5. Non-duplicate call (different args): execution should proceed (returns None)
        other_args = {"ticker": "SPY"}
        res_other = await before_cb(tool=mock_tool, args=other_args, tool_context=MagicMock())
        assert res_other is None

        # 6. Same call but after >2.0s (e.g. 2.1s): execution should proceed (returns None)
        mock_time.return_value = 102.1
        res4 = await before_cb(tool=mock_tool, args=args, tool_context=MagicMock())
        assert res4 is None


def test_sub_agents_disallow_transfer():
    """Verify that all single-turn sub-agents have transfer to parent/peers disabled."""
    config = AppConfig()

    sub_agents = [
        create_news_sentiment_agent(config),
        create_strategy_agent(config),
        create_risk_manager_agent(config),
        create_execution_agent(config),
    ]

    for agent in sub_agents:
        llm = _llm_agent(agent)
        assert llm.disallow_transfer_to_parent is True, (
            f"{agent.name} disallow_transfer_to_parent is not True"
        )
        assert llm.disallow_transfer_to_peers is True, (
            f"{agent.name} disallow_transfer_to_peers is not True"
        )


@pytest.mark.asyncio
async def test_execution_agent_require_trade_approval():
    """Verify that execution agent's before_tool_callback respects require_trade_approval."""
    from unittest.mock import AsyncMock, patch

    config = AppConfig()
    # Ensure require_trade_approval is True
    config.settings.require_trade_approval = True

    agent = create_execution_agent(config)
    before_cb = agent.before_tool_callback
    assert before_cb is not None

    mock_tool = MagicMock()
    mock_tool.name = "place_stock_order"

    # Use first allowed ticker to avoid constitution check failures
    allowed_ticker = config.constitution.trading_rules.allowed_tickers[0]
    args = {"ticker": allowed_ticker, "side": "buy", "quantity": 10, "price": 10.0}

    # 1. With require_trade_approval = True, check_interactive_approval should be called
    with patch(
        "evotrader.web.server.check_interactive_approval", new_callable=AsyncMock
    ) as mock_approve:
        mock_approve.return_value = None
        await before_cb(tool=mock_tool, args=args, tool_context=MagicMock())
        mock_approve.assert_called_once_with(args)

    # 2. With require_trade_approval = False, check_interactive_approval should NOT be called
    config.settings.require_trade_approval = False
    with patch(
        "evotrader.web.server.check_interactive_approval", new_callable=AsyncMock
    ) as mock_approve:
        mock_approve.return_value = None
        await before_cb(tool=mock_tool, args=args, tool_context=MagicMock())
        mock_approve.assert_not_called()
