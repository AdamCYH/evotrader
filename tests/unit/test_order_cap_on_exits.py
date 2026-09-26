"""The risk manager's check agrees with the gate: the per-order cap limits entries only.

Changed 2026-09-26 (see TestTheOrderValueCapIsAnEntryLimit in
test_risk_gate_exit_path.py). Both gates read the same exit rule, so the
advisory check must not refuse a CLOSE or a protective order that the
pre-execution gate would let through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evotrader.agents import tools
from evotrader.config import AppConfig


@pytest.fixture
def quiet_day(monkeypatch):
    """Starter constitution ($5,000 order cap, SPY allowed), no losses today."""
    from evotrader.tools import market_hours

    journal = AsyncMock()
    journal.get_today_pnl.return_value = 0.0
    journal.get_session_consecutive_losses.return_value = 0
    journal.get_trade_count_today.return_value = 0
    journal.get_open_trades.return_value = []
    journal.get_last_loss_timestamp.return_value = None
    monkeypatch.setattr(tools, "_config", SimpleNamespace(constitution=AppConfig().constitution))
    monkeypatch.setattr(tools, "_journal", journal)
    monkeypatch.setattr(tools, "_metrics", None)
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda **_k: market_hours.MarketSession.REGULAR
    )

    async def no_broker(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_call_mcp_tool", no_broker)


def _order_value(result: dict) -> list[str]:
    return [v for v in result["violations"] if "Order value" in v or "exceeds max order value" in v]


@pytest.mark.parametrize("action", ["CLOSE", "STOP_LOSS", "TAKE_PROFIT"])
async def test_an_exit_larger_than_the_cap_passes(quiet_day, action: str) -> None:
    out = await tools.check_risk_limits("SPY", "LONG", 100, 600.0, action=action)
    assert not _order_value(out), out["violations"]


async def test_an_entry_larger_than_the_cap_is_refused(quiet_day) -> None:
    out = await tools.check_risk_limits("SPY", "LONG", 100, 600.0, action="OPEN")
    assert _order_value(out), out["violations"]


async def test_closing_options_larger_than_the_cap_passes(quiet_day) -> None:
    out = await tools.check_option_risk_limits(
        "SPY", "put", "sell", 10, 20.0, 590.0, "2026-12-18", action="CLOSE"
    )
    assert not _order_value(out), out["violations"]
