"""Regression tests: practice mode passes read-only market data through, and nothing of the account.

A practice cycle on 2026-10-02 was refused ``get_earnings_calendar``,
``get_equity_price_book``, ``get_option_historicals`` and ``get_equity_tax_lots``
("not supported in simulation mode"), so the earnings event context, the
order-book depth check and the IV trend ran blind in practice mode while they
work live. The proxy now passes every read-only market-data tool to the real
broker: a tool that places nothing, changes nothing and takes no account number
describes the market, so its answer is the same whichever account asks. A tool
that reads the real account (positions, orders, tax lots, realized P&L,
watchlists, screeners) is still answered by the simulated broker or refused:
practice mode never shows the live account.

Every broker here is a stand-in that records what it is asked; nothing is real.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.agents import tools
from evotrader.sim import SimBroker, SimBrokerProxy
from evotrader.sim.sim_proxy import McpToolResponse
from evotrader.tools.tax_lots import check_wash_sale_risk, fetch_tax_lots

# Made up: what the stand-in broker calls its one account.
_REAL_ACCOUNT = "TEST-REAL-ACCOUNT"
_START = "2026-09-01T00:00:00Z"

# Read-only market data: describes the market and takes no account number.
MARKET_DATA_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("get_equity_quotes", {"symbols": ["SPY"]}),
    ("get_equity_historicals", {"symbols": ["SPY"], "start_time": _START}),
    ("get_equity_price_book", {"symbols": ["SPY"]}),
    (
        "get_equity_technical_indicators",
        {"symbol": "SPY", "type": "rsi", "interval": "day", "start_time": _START},
    ),
    ("get_option_chains", {"underlying_symbol": "SPY"}),
    ("get_option_instruments", {"chain_symbol": "SPY"}),
    ("get_option_quotes", {"instrument_ids": ["test-contract-1"]}),
    ("get_option_historicals", {"instrument_ids": ["test-contract-1"], "start_time": _START}),
    ("get_equity_fundamentals", {"symbols": ["SPY"]}),
    ("get_financials", {"symbols": ["AAPL"]}),
    ("get_earnings_calendar", {"days": 7}),
    ("get_earnings_results", {"symbol": "AAPL"}),
    ("get_indexes", {}),
    ("get_index_quotes", {"instrument_ids": ["test-index-1"]}),
    ("search", {"query": "apple"}),
]

# Read-only, but about the owner's account: never passed through.
REAL_ACCOUNT_TOOLS = [
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_option_positions",
    "get_equity_orders",
    "get_option_orders",
    "get_equity_tax_lots",
    "get_realized_pnl",
    "get_pnl_trade_history",
    "get_option_level_upgrade_info",
    "get_watchlists",
    "get_watchlist_items",
    "get_option_watchlist",
    "get_popular_watchlists",
    "get_scans",
    "get_scanner_filter_specs",
    "run_scan",
]

# Writes the sim does not simulate, and a tool no broker has.
NOT_SIMULATED_TOOLS = [
    "exercise_option",
    "cancel_option_exercise",
    "create_watchlist",
    "add_to_watchlist",
    "create_scan",
    "place_crypto_order",
]


class _RealBroker:
    """A stand-in for the real broker's session: records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **_: Any
    ) -> McpToolResponse:
        self.calls.append((name, dict(arguments or {})))
        if name == "get_accounts":
            accounts = [{"account_number": _REAL_ACCOUNT, "agentic_allowed": True}]
            return McpToolResponse(json.dumps({"data": {"accounts": accounts}}))
        return McpToolResponse(json.dumps({"data": {"answered_by": "real broker", "tool": name}}))

    @property
    def tools_called(self) -> list[str]:
        return [name for name, _ in self.calls]


def _toolset(real: _RealBroker) -> MagicMock:
    toolset = MagicMock()
    toolset._mcp_session_manager.create_session = AsyncMock(return_value=real)
    return toolset


def _proxied(broker: SimBroker, real: _RealBroker) -> tuple[SimBrokerProxy, MagicMock]:
    """The agents' toolset as practice mode patches it (see main.py)."""
    toolset = _toolset(real)
    proxy = SimBrokerProxy(broker, real_mcp_toolset=toolset)
    proxy.attach_to_toolset(toolset)
    return proxy, toolset


@pytest.fixture
async def broker(tmp_path: Path) -> AsyncIterator[SimBroker]:
    b = SimBroker(tmp_path / "sim_broker.db")
    b.slippage_model = "none"
    await b.initialize()
    await b.deposit(10_000.0)
    yield b
    await b.close()


@pytest.fixture
def real() -> _RealBroker:
    return _RealBroker()


@pytest.fixture
async def session(broker: SimBroker, real: _RealBroker) -> Any:
    """The session the patched toolset hands out: the proxy's wrapper around the real one."""
    _, toolset = _proxied(broker, real)
    return await toolset._mcp_session_manager.create_session()


async def _call(session: Any, name: str, args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    res = await session.call_tool(name, args)
    return res, json.loads(res.content[0].text)


@pytest.mark.parametrize(("name", "args"), MARKET_DATA_TOOLS, ids=[n for n, _ in MARKET_DATA_TOOLS])
async def test_market_data_reaches_the_real_broker_unchanged(
    session: Any, real: _RealBroker, name: str, args: dict[str, Any]
) -> None:
    res, body = await _call(session, name, dict(args))
    assert res.isError is False
    assert body["data"] == {"answered_by": "real broker", "tool": name}
    assert real.calls == [(name, args)]


@pytest.mark.parametrize("name", REAL_ACCOUNT_TOOLS)
async def test_nothing_about_the_real_account_is_asked_for(
    session: Any, real: _RealBroker, broker: SimBroker, name: str
) -> None:
    args = {
        "account_number": broker.account_number,
        "symbol": "SPY",
        "list_id": "test-list",
        "scan_id": "test-scan",
    }
    res, body = await _call(session, name, args)
    assert real.calls == [], f"{name} reached the real broker"
    if res.isError:
        assert body["error"] == f"Error: Tool '{name}' is not supported in simulation mode."
    else:
        # The simulated broker's own answer, with nothing of the real one in it.
        assert "real broker" not in json.dumps(body)
        assert _REAL_ACCOUNT not in json.dumps(body)


@pytest.mark.parametrize("name", NOT_SIMULATED_TOOLS)
async def test_a_tool_the_sim_does_not_know_is_refused(
    session: Any, real: _RealBroker, name: str
) -> None:
    res, body = await _call(session, name, {"symbol": "BTC"})
    assert res.isError is True
    assert body == {
        "data": None,
        "error": f"Error: Tool '{name}' is not supported in simulation mode.",
        "isError": True,
    }
    assert real.calls == []


def test_the_pass_through_set_names_no_account_tool() -> None:
    """The proxy's two routes never overlap, and no account tool is on the pass-through one."""
    assert not SimBrokerProxy.ALLOWED_PASSTHROUGH_TOOLS & SimBrokerProxy.INTERCEPTED_TOOLS
    assert not SimBrokerProxy.ALLOWED_PASSTHROUGH_TOOLS & set(REAL_ACCOUNT_TOOLS)
    assert {name for name, _ in MARKET_DATA_TOOLS} <= SimBrokerProxy.ALLOWED_PASSTHROUGH_TOOLS


async def test_the_practice_accounts_name_never_reaches_the_real_broker(
    session: Any, real: _RealBroker, broker: SimBroker
) -> None:
    """The one pass-through that takes an account number asks about the real
    account by its own number, found once from the broker's account list."""
    args = {"account_number": broker.account_number, "symbols": ["SPY"]}
    res, _ = await _call(session, "get_equity_tradability", dict(args))
    assert res.isError is False
    assert real.calls == [
        ("get_accounts", {}),
        ("get_equity_tradability", {"account_number": _REAL_ACCOUNT, "symbols": ["SPY"]}),
    ]

    await _call(session, "get_equity_tradability", dict(args))
    assert real.tools_called.count("get_accounts") == 1
    assert broker.account_number not in json.dumps(real.calls)


async def test_tax_lots_are_the_practice_accounts_own(
    session: Any, real: _RealBroker, broker: SimBroker
) -> None:
    broker._get_live_bid_ask = AsyncMock(return_value=(100.0, 100.0))  # type: ignore[method-assign]
    await broker.place_equity_order(
        {"symbol": "SPY", "side": "buy", "type": "market", "quantity": 10}
    )

    args = {"account_number": broker.account_number, "symbol": "SPY"}
    res, held = await _call(session, "get_equity_tax_lots", args)
    assert res.isError is False
    assert held["data"]["tax_lots"] == [
        {
            "symbol": "SPY",
            "quantity": "10.0000",
            "cost_basis": "1000.00",
            "cost_per_share": "100.0000",
            "sold_date": None,
        }
    ]

    _, not_held = await _call(session, "get_equity_tax_lots", {**args, "symbol": "QQQ"})
    assert not_held["data"]["tax_lots"] == []
    assert real.calls == []


async def test_the_pipelines_own_calls_take_the_same_route(
    broker: SimBroker, real: _RealBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deterministic tools reach the broker through ``_call_mcp_tool``, which
    in practice mode holds the patched toolset: market data passes through, the
    account stays simulated, and the wash-sale check gets an answer."""
    proxy, toolset = _proxied(broker, real)
    monkeypatch.setattr(tools, "_sim_proxy", proxy)
    monkeypatch.setattr(tools, "_mcp_toolset", toolset)

    calendar = await tools._call_mcp_tool("get_earnings_calendar", {"days": 7})
    assert calendar == {"data": {"answered_by": "real broker", "tool": "get_earnings_calendar"}}

    lots = await fetch_tax_lots("SPY", tools._call_mcp_tool, account_number=broker.account_number)
    assert lots == []
    guard = SimpleNamespace(enabled=True, lookback_days=30, mode="warn")
    assert check_wash_sale_risk("SPY", lots, guard)["wash_sale_risk"] is False  # type: ignore[arg-type]

    assert real.calls == [("get_earnings_calendar", {"days": 7})]
