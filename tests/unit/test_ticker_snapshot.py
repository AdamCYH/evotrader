"""Pricing a second instrument must be generic, not a special case.

SMST was added as a bearish vehicle with config, constitution, instructions and
a console badge — but no price data. The strategy agent's tools were all local,
so on 2026-09-17 it reported: "I had no SMST price data this cycle, so I could
not state a real stop trigger. I won't enter a leveraged position with a stop
I've only estimated from theory."

The agent was right to refuse. The fix is a symbol-agnostic lookup gated on the
constitution's allowed list — NOT an "inverse ticker" accessor, which would bake
in a two-instrument assumption and break on the third.
"""

from __future__ import annotations

import pytest

from evotrader.agents.tools import get_ticker_snapshot
from evotrader.tools.asset_context import bind_asset_context, reset_asset_context


@pytest.fixture
def bound(monkeypatch):
    import logging

    logging.disable(logging.WARNING)
    from evotrader.config import AppConfig

    config = AppConfig()
    config.settings.asset.primary_ticker = "ABC"
    config.constitution.trading_rules.allowed_tickers = ["ABC", "XYZ"]
    bind_asset_context(config)
    yield config
    reset_asset_context()
    logging.disable(logging.NOTSET)


# ── The permission gate ───────────────────────────────────────────


async def test_a_symbol_outside_the_constitution_is_refused(bound):
    out = await get_ticker_snapshot("TSLA")
    assert "error" in out
    assert "not a permitted ticker" in out["error"]


async def test_the_refusal_names_what_is_permitted(bound):
    """An agent told only 'no' has to guess; told the list, it can proceed."""
    out = await get_ticker_snapshot("TSLA")
    assert "ABC" in out["error"] and "XYZ" in out["error"]
    assert out["permitted_tickers"] == ["ABC", "XYZ"]


async def test_nothing_is_permitted_when_no_context_is_bound():
    """Unbound must fail closed — never price a symbol nobody authorised."""
    reset_asset_context()
    out = await get_ticker_snapshot("ABC")
    assert "error" in out


@pytest.mark.parametrize("given", ["xyz", "  XYZ  ", "XyZ"])
async def test_symbols_are_normalised_before_the_check(bound, monkeypatch, given):
    """A permitted ticker must not be refused over whitespace or case."""
    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _stub_mcp)
    out = await get_ticker_snapshot(given)
    assert "error" not in out
    assert out["ticker"] == "XYZ"


# ── Genericity ────────────────────────────────────────────────────


async def test_the_tool_has_no_notion_of_primary_or_inverse(bound, monkeypatch):
    """Any permitted symbol is served identically — that is the whole point.

    An `inverse_ticker` accessor would have worked today and broken on the third
    instrument. This must treat the primary and any other permitted symbol the
    same way.
    """
    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _stub_mcp)
    primary = await get_ticker_snapshot("ABC")
    other = await get_ticker_snapshot("XYZ")
    assert set(primary) == set(other), "the shape must not depend on which symbol"
    assert primary["ticker"] == "ABC" and other["ticker"] == "XYZ"


async def test_a_newly_permitted_symbol_needs_no_code_change(monkeypatch):
    """Add a fourth ticker to the constitution and it is served immediately."""
    import logging

    logging.disable(logging.WARNING)
    from evotrader.config import AppConfig

    config = AppConfig()
    config.settings.asset.primary_ticker = "ABC"
    config.constitution.trading_rules.allowed_tickers = ["ABC", "XYZ", "DEF", "GHI"]
    bind_asset_context(config)
    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _stub_mcp)
    try:
        for sym in ("ABC", "XYZ", "DEF", "GHI"):
            assert "error" not in await get_ticker_snapshot(sym)
    finally:
        reset_asset_context()
        logging.disable(logging.NOTSET)


# ── What it returns ───────────────────────────────────────────────


async def test_it_supplies_what_a_stop_needs(bound, monkeypatch):
    """A stop needs a price AND a volatility measure, not just a price."""
    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _stub_mcp)
    out = await get_ticker_snapshot("XYZ")
    assert out["quote"]["last"] > 0
    assert out["atr_14"] and out["atr_14"] > 0
    assert out["atr_pct_of_price"] and 0 < out["atr_pct_of_price"] < 1


async def test_it_returns_no_trading_signal(bound, monkeypatch):
    """Direction comes from the traded instrument, never from the vehicle.

    A composite signal computed on a hedging vehicle's own price series
    backtests at -10.6%; the edge lives in the instrument, not the vehicle.
    """
    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _stub_mcp)
    out = await get_ticker_snapshot("XYZ")
    for forbidden in ("signal", "composite", "regime", "algo_signal", "direction"):
        assert forbidden not in out, f"snapshot must not carry {forbidden!r}"


async def test_a_failed_quote_reports_rather_than_raising(bound, monkeypatch):
    """ "I have no price" is actionable; an exception loses the whole cycle."""

    async def _dead(*a, **k):
        return None

    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _dead)
    out = await get_ticker_snapshot("XYZ")
    assert out["quote"]["last"] == 0.0
    assert any("quote" in e.lower() for e in out["errors"])


# ── The footgun on the analysis path ──────────────────────────────


async def test_gather_market_data_flags_a_non_primary_analysis(bound, monkeypatch):
    """Its composite signal is only validated for the configured instrument."""
    from evotrader.agents.tools import gather_market_data

    monkeypatch.setattr("evotrader.agents.tools._call_mcp_tool", _stub_mcp)
    primary = await gather_market_data("ABC")
    other = await gather_market_data("XYZ")
    assert primary["is_primary_instrument"] is True
    assert other["is_primary_instrument"] is False
    assert other["configured_primary"] == "ABC"


# ── Stub ──────────────────────────────────────────────────────────


async def _stub_mcp(tool_name: str, args: dict):
    """Minimal MCP responses: a quote and 90 daily candles with real range."""
    symbol = (args.get("symbols") or ["?"])[0]
    if tool_name == "get_equity_quotes":
        return {
            "data": {
                "results": [
                    {
                        "quote": {
                            "symbol": symbol,
                            "bid_price": "99.50",
                            "ask_price": "100.50",
                            "last_trade_price": "100.00",
                            "previous_close": "98.00",
                            "adjusted_previous_close": "98.00",
                            "volume": "1000000",
                        }
                    }
                ]
            }
        }
    if tool_name == "get_equity_historicals":
        bars = []
        for i in range(90):
            base = 100.0 + (i % 7) - 3
            bars.append(
                {
                    "begins_at": f"2026-06-{(i % 28) + 1:02d}T00:00:00Z",
                    "open_price": f"{base:.2f}",
                    "high_price": f"{base + 2.5:.2f}",
                    "low_price": f"{base - 2.5:.2f}",
                    "close_price": f"{base + 0.5:.2f}",
                    "volume": "1000000",
                }
            )
        return {"data": {"results": [{"symbol": symbol, "bars": bars}]}}
    return None
