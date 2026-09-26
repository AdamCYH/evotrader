"""The one agentic-account portfolio lookup that six tools used to repeat.

gather_market_data, check_risk_limits, check_option_risk_limits,
reconcile_pending_orders, get_market_status and reconcile_broker_pnl each had a
copy of "get accounts → pick the agentic one → get its portfolio". These pin the
shared helpers' contract so the six callers keep behaving the same way.
"""

from __future__ import annotations

import pytest

from evotrader.agents import tools

_ACCOUNTS = {
    "data": {
        "accounts": [
            {"account_number": "IND1", "is_default": True},
            {"account_number": "AGT1", "agentic_allowed": True},
        ]
    }
}


def _broker(portfolio: dict | None, accounts: dict | None = _ACCOUNTS):
    calls = []

    async def mcp(tool_name, arguments):
        calls.append((tool_name, arguments))
        if tool_name == "get_accounts":
            return accounts
        if tool_name == "get_portfolio":
            return portfolio
        return None

    return mcp, calls


@pytest.mark.parametrize("bp", [{"buying_power": "3000.50"}, 3000.5, "3000.50"])
async def test_reads_the_agentic_account_in_either_buying_power_shape(monkeypatch, bp) -> None:
    mcp, calls = _broker(
        {"data": {"cash": "3000.50", "total_value": "5000.25", "buying_power": bp}}
    )
    monkeypatch.setattr(tools, "_call_mcp_tool", mcp)
    out = await tools._agentic_portfolio()
    assert out == {
        "account_number": "AGT1",
        "cash_balance": 3000.5,
        "total_value": 5000.25,
        "buying_power": 3000.5,
    }
    assert ("get_portfolio", {"account_number": "AGT1"}) in calls, (
        "the agentic account, not the default"
    )


async def test_nothing_from_the_broker_is_none(monkeypatch) -> None:
    for accounts, portfolio in [
        (None, {"data": {}}),
        ({"data": {"accounts": []}}, {"data": {}}),
        (_ACCOUNTS, None),
    ]:
        mcp, _ = _broker(portfolio, accounts)
        monkeypatch.setattr(tools, "_call_mcp_tool", mcp)
        assert await tools._agentic_portfolio() is None


async def test_account_number_alone(monkeypatch) -> None:
    mcp, calls = _broker(None)
    monkeypatch.setattr(tools, "_call_mcp_tool", mcp)
    assert await tools._agentic_account_number() == "AGT1"
    assert [c[0] for c in calls] == ["get_accounts"], (
        "no portfolio call when only the number is needed"
    )


async def test_broker_errors_reach_the_caller(monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tools, "_call_mcp_tool", boom)
    with pytest.raises(ConnectionError):
        await tools._agentic_portfolio()
