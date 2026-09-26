import datetime
import json

import pytest

from evotrader.agents.tools import (
    gather_market_data,
    normalize_market_snapshot,
    run_composite_strategy,
)
from evotrader.models.market import MarketSnapshot


def test_normalize_market_snapshot_valid():
    """Test that a fully valid snapshot is normalized correctly without errors."""
    valid_data = {
        "ticker": "QQQ",
        "timestamp": "2026-06-21T10:00:00Z",
        "quote": {
            "ticker": "QQQ",
            "bid": 739.0,
            "ask": 740.0,
            "last": 739.5,
            "volume": 5000000.0,
            "timestamp": "2026-06-21T10:00:00Z",
        },
        "indicators": {
            "rsi_14": 55.0,
            "macd_line": 1.2,
            "macd_signal": 1.0,
            "macd_histogram": 0.2,
            "bollinger_upper": 750.0,
            "bollinger_middle": 735.0,
            "bollinger_lower": 720.0,
            "bollinger_width": 0.04,
            "vwap": 738.0,
            "ibs": 0.6,
            "atr_14": 5.0,
            "relative_volume": 1.1,
        },
        "regime": {
            "regime": "range_bound",
            "confidence": 0.8,
            "reasoning": "Standard verification",
        },
    }

    normalized = normalize_market_snapshot(valid_data)

    # Assert top-level fields
    assert normalized["ticker"] == "QQQ"
    assert normalized["timestamp"] == "2026-06-21T10:00:00Z"

    # Assert quote fields
    assert normalized["quote"]["ticker"] == "QQQ"
    assert normalized["quote"]["last"] == 739.5

    # Assert indicators
    assert normalized["indicators"]["rsi_14"] == 55.0
    assert normalized["indicators"]["macd_line"] == 1.2

    # Assert regime
    assert normalized["regime"]["regime"] == "range_bound"
    assert normalized["regime"]["confidence"] == 0.8
    assert normalized["regime"]["reasoning"] == "Standard verification"

    # Check that it validates as a MarketSnapshot model
    snapshot = MarketSnapshot.model_validate(normalized)
    assert snapshot.ticker == "QQQ"


def test_normalize_market_snapshot_malformed():
    """Test that a malformed snapshot from LLM with wrong field names and missing fields is normalized properly."""
    malformed_data = {
        "symbol": "QQQ",
        "time": "2026-06-21T10:00:00Z",
        "quote": {"symbol": "QQQ", "last_close_price": 740.62},
        "indicators": {
            "rsi_14_day": 58.61,
            "macd": 7.749,
            "signal": 8.564,
            "histogram": -0.815,
            "bollinger_middle_band": 726.88,
            "bollinger_upper_band": 756.86,
            "bollinger_lower_band": 696.89,
            "atr_14_day": 15.63,
            "sma_20_day": 726.88,
            "ema_9_day": 727.39,
            "ema_21_day": 721.50,
        },
        "regime": "range_bound",
    }

    normalized = normalize_market_snapshot(malformed_data)

    # Field translations
    assert normalized["ticker"] == "QQQ"
    assert normalized["timestamp"] == "2026-06-21T10:00:00Z"

    # Quote normalization
    assert normalized["quote"]["ticker"] == "QQQ"
    assert normalized["quote"]["last"] == 740.62
    assert normalized["quote"]["bid"] == 740.62  # Fallback to last
    assert normalized["quote"]["ask"] == 740.62  # Fallback to last

    # Indicators normalization
    assert normalized["indicators"]["rsi_14"] == 58.61
    assert normalized["indicators"]["macd_line"] == 7.749
    assert normalized["indicators"]["macd_signal"] == 8.564
    assert normalized["indicators"]["macd_histogram"] == -0.815
    assert normalized["indicators"]["bollinger_middle"] == 726.88
    assert normalized["indicators"]["bollinger_upper"] == 756.86
    assert normalized["indicators"]["bollinger_lower"] == 696.89
    assert normalized["indicators"]["atr_14"] == 15.63
    assert normalized["indicators"]["sma_20"] == 726.88
    assert normalized["indicators"]["ema_9"] == 727.39
    assert normalized["indicators"]["ema_21"] == 721.50

    # Regime normalization
    assert normalized["regime"]["regime"] == "range_bound"
    assert normalized["regime"]["confidence"] == 1.0
    assert normalized["regime"]["reasoning"] == "Inferred from text input"

    # Validate with Pydantic
    snapshot = MarketSnapshot.model_validate(normalized)
    assert snapshot.ticker == "QQQ"
    assert snapshot.indicators.rsi_14 == 58.61


def test_run_composite_strategy_with_malformed_json():
    """Test that run_composite_strategy can parse and execute successfully on malformed snapshot JSON strings."""
    malformed_json = """
    {
      "quote": {
        "symbol": "QQQ",
        "price": 739.68
      },
      "indicators": {
        "rsi_14_day": 58.61,
        "macd": 7.749,
        "signal": 8.564,
        "histogram": -0.815,
        "bollinger_middle_band": 726.88,
        "bollinger_upper_band": 756.86,
        "bollinger_lower_band": 696.89,
        "atr_14_day": 15.63,
        "sma_20_day": 726.88,
        "ema_9_day": 727.39,
        "ema_21_day": 721.50
      },
      "regime": "range_bound"
    }
    """

    # This should not raise a ValidationError and should return a dict containing composite_signal
    result = run_composite_strategy(malformed_json)
    assert "composite_signal" in result
    assert isinstance(result["composite_signal"], float)
    assert "algo_version" in result
    assert "sub_signals" in result


def test_run_composite_strategy_with_trailing_chars():
    """Test that run_composite_strategy handles trailing braces gracefully via balanced JSON extraction."""
    malformed_json_trailing = (
        '{"quote": {"symbol": "QQQ", "price": 739.68}, "indicators": {}, "regime": ""}}'
    )

    # This should repair the JSON and execute successfully
    result = run_composite_strategy(malformed_json_trailing)
    assert "composite_signal" in result
    assert "error" not in result
    assert isinstance(result["algo_version"], str)


def test_run_composite_strategy_completely_invalid():
    """Test that run_composite_strategy handles completely invalid JSON string by returning a fallback error dict."""
    result = run_composite_strategy("completely invalid JSON block")
    assert "composite_signal" in result
    assert result["composite_signal"] == 0.0
    assert "error" in result
    assert "Invalid market snapshot" in result["error"]
    assert result["algo_version"] == "error_fallback"


@pytest.mark.asyncio
async def test_gather_market_data_success():
    """Test gather_market_data fetches quotes/historicals, computes indicators, and runs strategy successfully."""
    from unittest.mock import patch

    mock_quote_response = {
        "data": {
            "results": [
                {
                    "quote": {
                        "bid_price": "739.0",
                        "ask_price": "740.0",
                        "last_trade_price": "739.5",
                        "volume": "5000000",
                        "updated_at": "2026-06-21T10:00:00Z",
                    }
                }
            ]
        }
    }

    # Generate 65 daily candles for indicator computation
    mock_candles = []
    base_price = 700.0
    for i in range(65):
        mock_candles.append(
            {
                "begins_at": (
                    datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=70 - i)
                ).isoformat(),
                "open_price": str(base_price + i),
                "high_price": str(base_price + i + 2),
                "low_price": str(base_price + i - 2),
                "close_price": str(base_price + i + 1),
                "volume": "1000000",
            }
        )
    mock_hist_response = {"data": {"results": [{"bars": mock_candles}]}}

    async def mock_call_mcp_tool(tool_name, arguments):
        if tool_name == "get_equity_quotes":
            assert arguments == {"symbols": ["QQQ"]}
            return mock_quote_response
        elif tool_name == "get_equity_historicals":
            assert arguments["symbols"] == ["QQQ"]
            assert arguments["interval"] in ("day", "5minute")
            return mock_hist_response
        return None

    with patch("evotrader.agents.tools._call_mcp_tool", side_effect=mock_call_mcp_tool):
        result = await gather_market_data("QQQ")

        # Verify structure
        assert "ticker" in result
        assert result["ticker"] == "QQQ"
        assert "quote" in result
        assert result["quote"]["last"] == 739.5
        assert "indicators" in result
        assert "rsi_14" in result["indicators"]
        assert "algo_signal" in result
        assert "composite_signal" in result["algo_signal"]


@pytest.mark.asyncio
async def test_record_trade_stop_loss_not_mislabeled_as_short_sell():
    """Verify that stop sell orders with side='sell' are recorded as STOP_LOSS LONG, not OPEN SHORT."""
    from unittest.mock import AsyncMock, patch

    from evotrader.agents import tools as tools_module
    from evotrader.models.trade import TradeAction, TradeDirection

    mock_journal = AsyncMock()
    mock_journal.get_open_trades.return_value = []
    mock_journal.record_trade.return_value = [107]
    mock_journal.save_pending_order = AsyncMock()

    trade_data = {
        "ticker": "QQQ",
        "side": "sell",
        "order_type": "stop",
        "stop_price": 698.0,
        "quantity": 1.0,
        "reasoning": "Stop loss triggered at 698",
        "algo_signal": 0.0,
        "hybrid_score": 0.0,
        "regime": "range_bound",
        "algo_version": "v1",
    }

    with patch.object(tools_module, "_journal", mock_journal):
        res = await tools_module.record_trade(json.dumps(trade_data))
        assert "error" not in res
        assert mock_journal.record_trade.called
        proposal = mock_journal.record_trade.call_args.kwargs["proposal"]
        assert proposal.action == TradeAction.STOP_LOSS
        assert proposal.direction == TradeDirection.LONG


@pytest.mark.asyncio
async def test_record_trade_explicit_sell_short_parsed_correctly():
    """Verify explicit sell_short actions continue to record as OPEN SHORT."""
    from unittest.mock import AsyncMock, patch

    from evotrader.agents import tools as tools_module
    from evotrader.models.trade import TradeAction, TradeDirection

    mock_journal = AsyncMock()
    mock_journal.get_open_trades.return_value = []
    mock_journal.record_trade.return_value = [108]
    mock_journal.save_pending_order = AsyncMock()

    trade_data = {
        "ticker": "QQQ",
        "action": "sell_short",
        "quantity": 1.0,
        "limit_price": 720.0,
        "reasoning": "Opening short position",
        "algo_signal": -0.8,
        "hybrid_score": -0.8,
        "regime": "bearish",
        "algo_version": "v1",
    }

    with patch.object(tools_module, "_journal", mock_journal):
        res = await tools_module.record_trade(json.dumps(trade_data))
        assert "error" not in res
        assert mock_journal.record_trade.called
        proposal = mock_journal.record_trade.call_args.kwargs["proposal"]
        assert proposal.action == TradeAction.OPEN
        assert proposal.direction == TradeDirection.SHORT
