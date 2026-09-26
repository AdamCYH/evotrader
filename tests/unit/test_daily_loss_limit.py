"""Regression tests: the constitution's daily-loss limit is measured against the real account.

Found 2026-09-25 while answering "why is so little invested?". Both risk
checks tested

    today_pnl < -(max_daily_loss_pct * max_order_value_usd * 10)

which is 5% of $100,000 = $5,000 — more than the whole account, so the
"HARD FLOOR" could never fire. It also had no exit exemption: on the day it did
fire, it would have refused the sells that stop the loss.

Operator decision, 2026-09-25: keep 5%, of the real account. Closed-trade
losses count. Past the limit, no new entries for the rest of the day; exits and
protective orders are always allowed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evotrader.agents import tools
from evotrader.agents.tools import _daily_loss_verdict

_ACCOUNT = 5000.0


class TestVerdict:
    def test_a_loss_past_five_percent_blocks_new_entries(self) -> None:
        violation, warning = _daily_loss_verdict(-260.0, _ACCOUNT, 0.05, is_exit=False)
        assert violation is not None and warning is None
        assert "$260.00" in violation and "$250.00" in violation

    def test_exits_are_never_blocked(self) -> None:
        violation, warning = _daily_loss_verdict(-260.0, _ACCOUNT, 0.05, is_exit=True)
        assert violation is None
        assert warning is not None and "exit" in warning.lower()

    def test_the_old_formula_let_the_whole_account_go(self) -> None:
        """THE BUG: -$4,000 in a day was under the old $5,000 threshold."""
        old_threshold = 0.05 * 10000 * 10
        assert -old_threshold < -4000.0, "the old check would have allowed this"
        violation, _ = _daily_loss_verdict(-4000.0, _ACCOUNT, 0.05, is_exit=False)
        assert violation is not None

    @pytest.mark.parametrize("pnl", [-200.0, 0.0, 150.0])
    def test_inside_the_limit_nothing_happens(self, pnl: float) -> None:
        assert _daily_loss_verdict(pnl, _ACCOUNT, 0.05, is_exit=False) == (None, None)

    def test_an_unknown_account_value_is_said_not_guessed(self) -> None:
        violation, warning = _daily_loss_verdict(-300.0, None, 0.05, is_exit=False)
        assert violation is None
        assert warning is not None and "not evaluated" in warning


def _constitution_with_a_five_percent_limit(update_data_yaml):
    """A complete constitution file carrying the operator's 5% daily-loss limit,
    with MSTR permitted, loaded the way the app loads it (percent to fraction)."""
    from evotrader.config import _load_constitution

    data_dir = update_data_yaml(
        "constitution.yaml",
        {"risk_limits": {"max_daily_loss_pct": 5}, "trading_rules": {"allowed_tickers": ["MSTR"]}},
    )
    return _load_constitution(data_dir)


def _portfolio_mcp(total_value: float | None):
    async def mcp(tool_name, arguments):
        if total_value is None:
            return None
        if tool_name == "get_accounts":
            return {"data": {"accounts": [{"account_number": "A1", "agentic_allowed": True}]}}
        if tool_name == "get_portfolio":
            return {
                "data": {
                    "cash": 3000.0,
                    "total_value": total_value,
                    "buying_power": {"buying_power": 3000.0},
                }
            }
        return None

    return mcp


@pytest.fixture
def losing_day(monkeypatch, update_data_yaml):
    """A 5% daily-loss limit, $300 of closed-trade losses today, regular session."""
    from evotrader.tools import market_hours

    journal = AsyncMock()
    journal.get_today_pnl.return_value = -300.0
    journal.get_session_consecutive_losses.return_value = 0
    journal.get_trade_count_today.return_value = 1
    journal.get_open_trades.return_value = []
    journal.get_last_loss_timestamp.return_value = None
    metrics = AsyncMock()
    metrics.get_latest_metrics.return_value = [SimpleNamespace(portfolio_value=_ACCOUNT)]
    monkeypatch.setattr(
        tools,
        "_config",
        SimpleNamespace(constitution=_constitution_with_a_five_percent_limit(update_data_yaml)),
    )
    monkeypatch.setattr(tools, "_journal", journal)
    monkeypatch.setattr(tools, "_metrics", metrics)
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda **_k: market_hours.MarketSession.REGULAR
    )
    monkeypatch.setattr(tools, "_call_mcp_tool", _portfolio_mcp(_ACCOUNT))
    return monkeypatch


def _daily(result: dict) -> list[str]:
    return [v for v in result["violations"] if "Daily loss" in v]


class TestEquityCheck:
    async def test_new_buy_is_refused_after_a_losing_day(self, losing_day) -> None:
        out = await tools.check_risk_limits("MSTR", "LONG", 2, 161.07, action="OPEN")
        assert out["verdict"] == "REJECTED"
        assert _daily(out), out["violations"]

    @pytest.mark.parametrize("action", ["CLOSE", "STOP_LOSS", "TAKE_PROFIT"])
    async def test_exits_and_protection_still_pass(self, losing_day, action: str) -> None:
        out = await tools.check_risk_limits("MSTR", "LONG", 12, 161.07, action=action)
        assert not _daily(out)

    async def test_the_limit_is_reported(self, losing_day) -> None:
        out = await tools.check_risk_limits("MSTR", "LONG", 2, 161.07, action="OPEN")
        assert out["context"]["daily_loss_limit_usd"] == pytest.approx(0.05 * _ACCOUNT)

    async def test_broker_down_falls_back_to_the_recorded_account_value(self, losing_day) -> None:
        losing_day.setattr(tools, "_call_mcp_tool", _portfolio_mcp(None))
        out = await tools.check_risk_limits("MSTR", "LONG", 2, 161.07, action="OPEN")
        assert _daily(out)


class TestOptionCheck:
    async def test_new_contract_is_refused_after_a_losing_day(self, losing_day) -> None:
        out = await tools.check_option_risk_limits(
            "MSTR", "call", "buy", 1, 5.0, 170.0, "2026-12-18", action="OPEN"
        )
        assert out["verdict"] == "REJECTED"
        assert _daily(out), out["violations"]

    async def test_closing_a_contract_still_passes(self, losing_day) -> None:
        out = await tools.check_option_risk_limits(
            "MSTR", "call", "sell", 1, 5.0, 170.0, "2026-12-18", action="CLOSE"
        )
        assert not _daily(out)
