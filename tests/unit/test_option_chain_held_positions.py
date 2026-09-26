"""Regression tests: option chain must include held positions.

Guards against the scenario where held option contracts drift outside the
offset-based strike selection band, causing the strategy agent to lose
visibility of their live marks and becoming unable to propose exits.

See: data/evolution/reviews/20260707_210138_option_chain_must_include_held_positions.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from evotrader.agents import tools as tools_module

# ---------------------------------------------------------------------------
# Fixtures — mock MCP + journal
# ---------------------------------------------------------------------------


def _make_instrument(
    inst_id: str, strike: float, opt_type: str = "call", expiration: str = "2026-08-07"
) -> dict:
    """Build a minimal option instrument dict."""
    return {
        "id": inst_id,
        "type": opt_type,
        "strike_price": str(strike),
        "expiration_date": expiration,
        "state": "active",
        "tradability": "tradable",
    }


def _make_open_trade(
    trade_id: int,
    option_id: str,
    ticker: str = "QQQ",
    direction: str = "LONG",
    qty: float = 1.0,
    price: float = 5.0,
    option_type: str = "call",
    strike: float = 759.0,
    expiration: str = "2026-08-07",
) -> dict:
    """Build a minimal open trade journal row."""
    return {
        "id": trade_id,
        "ticker": ticker,
        "direction": direction,
        "action": "OPEN",
        "quantity": qty,
        "remaining_quantity": qty,
        "price": price,
        "option_id": option_id,
        "option_type": option_type,
        "strike": strike,
        "expiration": expiration,
        "timestamp": "2026-07-06T14:30:00Z",
    }


def _make_quote(
    inst_id: str, bid: float = 0.50, ask: float = 0.70, mark: float = 0.60, delta: float = 0.25
) -> dict:
    """Build a quote result for an option instrument."""
    return {
        "quote": {
            "instrument_id": inst_id,
            "bid_price": str(bid),
            "ask_price": str(ask),
            "adjusted_mark_price": str(mark),
            "last_trade_price": str(mark),
            "volume": "100",
            "open_interest": "200",
            "implied_volatility": "0.35",
            "delta": str(delta),
            "gamma": "0.005",
            "theta": "-0.05",
            "vega": "0.10",
        }
    }


def _make_equity_quote(ticker: str = "QQQ", price: float = 720.0) -> dict:
    return {
        "data": {
            "results": [
                {
                    "quote": {
                        "bid": str(price - 0.01),
                        "ask": str(price + 0.01),
                        "last_trade_price": str(price),
                        "last_extended_hours_trade_price": None,
                        "previous_close": str(price - 5),
                        "adjusted_previous_close": str(price - 5),
                        "volume": "40000000",
                        "updated_at": "2026-07-07T14:30:00Z",
                    }
                }
            ]
        }
    }


@pytest.fixture(autouse=True)
def _reset_tools_state():
    """Reset module-level state between tests."""
    tools_module.OPTION_ID_TO_TICKER.clear()
    tools_module._open_positions_cache = None
    yield
    tools_module.OPTION_ID_TO_TICKER.clear()
    tools_module._open_positions_cache = None


# ---------------------------------------------------------------------------
# gather_option_chain — Held position injection
# ---------------------------------------------------------------------------


class TestOptionChainHeldPositions:
    """Verify held option positions are force-included in the option chain."""

    @pytest.mark.asyncio
    async def test_held_position_injected(self) -> None:
        """A held position outside the offset band must appear in contracts
        with is_held_position=True."""
        # Offset band covers strikes ~720-900 (1.0x-1.25x of 720).
        # Held position at strike 759 (inst_id "held-759c") is within band
        # but we'll use a far-OTM strike that the offset won't pick.
        held_id = "held-far-otm"
        held_trade = _make_open_trade(1, held_id, strike=950.0)

        # Build 5 instruments in the offset band + the held one NOT in the list
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(5)]

        # Mock MCP calls
        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                # Return quotes for all requested IDs
                results = []
                for iid in args.get("instrument_ids", []):
                    results.append(_make_quote(iid))
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[held_trade])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        contracts = result["contracts"]
        held_contracts = [c for c in contracts if c.get("is_held_position")]
        assert len(held_contracts) == 1, f"Expected 1 held contract, got {len(held_contracts)}"
        assert held_contracts[0]["option_id"] == held_id
        # It should have live quote data (bid/ask/mark)
        assert held_contracts[0]["bid"] is not None
        assert held_contracts[0]["mark"] is not None

    @pytest.mark.asyncio
    async def test_held_position_bypasses_cap(self) -> None:
        """Held positions must not be subject to the 20-contract cap."""
        # Create enough instruments to hit the 20 cap
        instruments = [_make_instrument(f"inst-{i}", 700.0 + i * 2) for i in range(25)]
        # Two held positions not in the instrument list
        held_trades = [
            _make_open_trade(1, "held-a", strike=950.0),
            _make_open_trade(2, "held-b", strike=960.0, option_type="put"),
        ]

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=held_trades)

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        contracts = result["contracts"]
        held_contracts = [c for c in contracts if c.get("is_held_position")]
        non_held = [c for c in contracts if not c.get("is_held_position")]

        # The 20-cap applies to offset-selected instruments
        assert len(non_held) <= 20
        # Both held positions must be present
        assert len(held_contracts) == 2
        held_ids = {c["option_id"] for c in held_contracts}
        assert held_ids == {"held-a", "held-b"}

    @pytest.mark.asyncio
    async def test_held_position_not_duplicated(self) -> None:
        """If a held position is already in the offset selection, it should
        NOT be duplicated but should still be flagged as held."""
        # The held position's strike is right at the underlying price
        # so offset selection will pick it up
        held_id = "inst-0"
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(5)]
        held_trade = _make_open_trade(1, held_id, strike=720.0)

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[held_trade])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        contracts = result["contracts"]
        ids = [c["option_id"] for c in contracts]
        # No duplicate
        assert ids.count(held_id) == 1

    @pytest.mark.asyncio
    async def test_no_held_positions_unchanged(self) -> None:
        """With no open trades, gather_option_chain works exactly as before."""
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(5)]

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        contracts = result["contracts"]
        held = [c for c in contracts if c.get("is_held_position")]
        assert len(held) == 0
        # All contracts should have is_held_position = False
        assert all(c.get("is_held_position") is False for c in contracts)

    @pytest.mark.asyncio
    async def test_is_held_position_flag(self) -> None:
        """Every contract must have the is_held_position field."""
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(3)]
        held_trade = _make_open_trade(1, "held-x", strike=950.0)

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[held_trade])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        for c in result["contracts"]:
            assert "is_held_position" in c, f"Missing is_held_position on {c['option_id']}"


# ---------------------------------------------------------------------------
# gather_option_chain — Budget picks
# ---------------------------------------------------------------------------


class TestBudgetPicks:
    """Verify budget_picks annotation for small accounts."""

    @pytest.mark.asyncio
    async def test_budget_picks_present(self) -> None:
        """Return value must include budget_picks with call/put keys."""
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(3)]

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        assert "budget_picks" in result
        assert "call" in result["budget_picks"]
        assert "put" in result["budget_picks"]


# ---------------------------------------------------------------------------
# gather_market_data — Open positions with live marks
# ---------------------------------------------------------------------------


class TestGatherMarketDataOpenPositions:
    """Verify gather_market_data includes open positions with live marks."""

    @pytest.mark.asyncio
    async def test_open_positions_included(self) -> None:
        """gather_market_data should include open_positions with live marks."""
        held_id = "opt-abc-123"
        held_trade = _make_open_trade(1, held_id, price=6.725, strike=759.0)
        option_quote = _make_quote(held_id, bid=0.50, ask=0.70, mark=0.60, delta=0.15)

        # Build minimal candle data (need >= 10)
        candles = []
        from datetime import timedelta

        base_date = datetime(2026, 5, 1, tzinfo=UTC)
        for i in range(60):
            d = base_date + timedelta(days=i)
            candles.append(
                {
                    "open_price": 710 + i * 0.2,
                    "high_price": 712 + i * 0.2,
                    "low_price": 709 + i * 0.2,
                    "close_price": 711 + i * 0.2,
                    "volume": 1000000,
                    "begins_at": d.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_equity_historicals":
                return {"data": {"results": [{"bars": candles}]}}
            if tool_name == "get_accounts":
                return {"data": {"accounts": [{"account_number": "12345", "type": "margin"}]}}
            if tool_name == "get_portfolio":
                return {"data": {"cash": 100.0, "total_value": 1000.0, "buying_power": 300.0}}
            if tool_name == "get_option_quotes":
                results = [option_quote]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[held_trade])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
            patch.object(tools_module, "_algo_registry", None),
            patch.object(tools_module, "_config", None),
        ):
            result = await tools_module.gather_market_data("QQQ")

        assert "open_positions" in result
        positions = result["open_positions"]
        assert positions is not None
        assert len(positions) == 1

        pos = positions[0]
        assert pos["option_id"] == held_id
        assert pos["entry_price"] == 6.725
        assert pos["direction"] == "LONG"
        assert pos["current_mark"] == 0.60
        assert pos["current_bid"] == 0.50
        assert pos["current_ask"] == 0.70
        # P&L: (0.60 - 6.725) * 1 * 100 = -612.50
        assert pos["unrealized_pnl"] == -612.5

    @pytest.mark.asyncio
    async def test_no_positions_returns_none(self) -> None:
        """With no open trades, open_positions should be None."""
        candles = []
        from datetime import timedelta

        base_date = datetime(2026, 5, 1, tzinfo=UTC)
        for i in range(60):
            d = base_date + timedelta(days=i)
            candles.append(
                {
                    "open_price": 710 + i * 0.2,
                    "high_price": 712 + i * 0.2,
                    "low_price": 709 + i * 0.2,
                    "close_price": 711 + i * 0.2,
                    "volume": 1000000,
                    "begins_at": d.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_equity_historicals":
                return {"data": {"results": [{"bars": candles}]}}
            if tool_name == "get_accounts":
                return {"data": {"accounts": [{"account_number": "12345", "type": "margin"}]}}
            if tool_name == "get_portfolio":
                return {"data": {"cash": 100.0, "total_value": 1000.0, "buying_power": 300.0}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
            patch.object(tools_module, "_algo_registry", None),
            patch.object(tools_module, "_config", None),
        ):
            result = await tools_module.gather_market_data("QQQ")

        assert result.get("open_positions") is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestOptionChainEdgeCases:
    """Edge cases for held position injection."""

    @pytest.mark.asyncio
    async def test_journal_error_does_not_crash(self) -> None:
        """If journal.get_open_trades() raises, the chain should still return
        normally — the error is appended to errors[]."""
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(3)]

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(side_effect=RuntimeError("DB broken"))

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        # Should still return contracts (just without held positions)
        assert result["contracts_count"] > 0
        # Error should be logged
        assert result.get("errors") is not None
        assert any("held option positions" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_held_position_different_ticker_ignored(self) -> None:
        """Held positions for a different ticker should not be injected."""
        instruments = [_make_instrument(f"inst-{i}", 720.0 + i * 5) for i in range(3)]
        # Held position for SPY, not QQQ
        held_trade = _make_open_trade(1, "spy-held", ticker="SPY", strike=500.0)

        async def mock_mcp(tool_name: str, args: dict) -> dict | None:
            if tool_name == "get_option_chains":
                return {"data": {"chains": [{"expiration_dates": ["2026-08-07"]}]}}
            if tool_name == "get_equity_quotes":
                return _make_equity_quote()
            if tool_name == "get_option_instruments":
                return {"data": {"instruments": instruments}}
            if tool_name == "get_option_quotes":
                results = [_make_quote(iid) for iid in args.get("instrument_ids", [])]
                return {"data": {"results": results}}
            return None

        mock_journal = AsyncMock()
        mock_journal.get_open_trades = AsyncMock(return_value=[held_trade])

        with (
            patch.object(tools_module, "_call_mcp_tool", side_effect=mock_mcp),
            patch.object(tools_module, "_journal", mock_journal),
        ):
            result = await tools_module.gather_option_chain("QQQ")

        held = [c for c in result["contracts"] if c.get("is_held_position")]
        assert len(held) == 0
