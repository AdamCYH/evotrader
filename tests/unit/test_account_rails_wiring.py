"""The account limits reach the three places that must apply them.

``callbacks.account_rails`` decides; this file checks that the risk tool, the
positions report and the pre-order gate all read the same verdict. Made-up
account: peak $6,000 recorded by the metrics job, $4,400 at the broker now,
a 25% drawdown limit, so 26.7% down: new entries halt, exits pass.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader import paths
from evotrader.agents import tools
from evotrader.agents.factory import create_execution_agent
from evotrader.callbacks.account_rails import AccountState
from evotrader.config import AppConfig, _load_constitution

PEAK = 6000.0
NOW_VALUE = 4400.0


def _portfolio_mcp(total_value: float | None):
    async def mcp(tool_name, arguments):
        if total_value is None:
            return None
        if tool_name == "get_accounts":
            return {"data": {"accounts": [{"account_number": "A1", "agentic_allowed": True}]}}
        if tool_name == "get_portfolio":
            return {
                "data": {
                    "cash": 1000.0,
                    "total_value": total_value,
                    "buying_power": {"buying_power": 1000.0},
                }
            }
        return None

    return mcp


@pytest.fixture
def deep_drawdown(monkeypatch, update_data_yaml):
    """A 25% drawdown limit, the account 26.7% under its recorded peak."""
    from evotrader.tools import market_hours

    journal = AsyncMock()
    journal.get_today_pnl.return_value = 0.0
    journal.get_pnl.return_value = -100.0
    journal.get_session_consecutive_losses.return_value = 0
    journal.get_trade_count_today.return_value = 1
    journal.get_open_trades.return_value = []
    journal.get_pending_orders.return_value = []
    journal.get_last_loss_timestamp.return_value = None
    metrics = AsyncMock()
    metrics.get_peak_portfolio_value.return_value = PEAK
    metrics.get_latest_metrics.return_value = [SimpleNamespace(portfolio_value=NOW_VALUE)]
    data_dir = update_data_yaml(
        "constitution.yaml",
        {
            "risk_limits": {"max_drawdown_pct": 25, "max_weekly_loss_pct": 15},
            "trading_rules": {"allowed_tickers": ["MSTR"]},
        },
    )
    monkeypatch.setattr(
        tools,
        "_config",
        SimpleNamespace(constitution=_load_constitution(data_dir), data_dir=data_dir),
    )
    monkeypatch.setattr(tools, "_journal", journal)
    monkeypatch.setattr(tools, "_metrics", metrics)
    monkeypatch.setattr(tools, "_open_positions_cache", None)
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda **_k: market_hours.MarketSession.REGULAR
    )
    monkeypatch.setattr(tools, "_call_mcp_tool", _portfolio_mcp(NOW_VALUE))
    return monkeypatch


def _halts(result: dict) -> list[str]:
    return [v for v in result["violations"] if "DRAWDOWN HALT" in v]


class TestTheRiskTool:
    async def test_a_new_buy_is_refused_in_a_deep_drawdown(self, deep_drawdown) -> None:
        out = await tools.check_risk_limits("MSTR", "LONG", 2, 161.07, action="OPEN")
        assert out["verdict"] == "REJECTED"
        assert _halts(out), out["violations"]
        rails = out["context"]["account_rails"]
        assert rails["drawdown_pct"] == pytest.approx(26.67, abs=0.01)
        assert rails["drawdown_halt"] is True and rails["value_source"] == "broker"

    @pytest.mark.parametrize("action", ["CLOSE", "STOP_LOSS", "TAKE_PROFIT"])
    async def test_exits_and_protection_still_pass(self, deep_drawdown, action) -> None:
        out = await tools.check_risk_limits("MSTR", "LONG", 12, 161.07, action=action)
        assert not _halts(out)
        assert out["verdict"] == "APPROVED", out["violations"]

    async def test_a_new_contract_is_refused_too(self, deep_drawdown) -> None:
        out = await tools.check_option_risk_limits(
            "MSTR", "call", "buy", 1, 5.0, 170.0, "2026-12-18", action="OPEN"
        )
        assert _halts(out), out["violations"]

    async def test_the_old_daily_loss_shape_is_kept(self, deep_drawdown) -> None:
        out = await tools.check_risk_limits("MSTR", "LONG", 2, 161.07, action="OPEN")
        context = out["context"]
        assert context["today_pnl"] == 0.0 and context["trades_today"] == 1
        assert context["daily_loss_limit_usd"] == pytest.approx(0.02 * NOW_VALUE)

    async def test_with_the_broker_down_the_recorded_value_is_used(self, deep_drawdown) -> None:
        deep_drawdown.setattr(tools, "_call_mcp_tool", _portfolio_mcp(None))
        out = await tools.check_risk_limits("MSTR", "LONG", 2, 161.07, action="OPEN")
        assert _halts(out)
        assert out["context"]["account_rails"]["value_source"] == "recorded"


class TestThePositionsReport:
    async def test_every_cycle_sees_the_standing(self, deep_drawdown) -> None:
        out = await tools.get_open_positions()
        rails = out["account_rails"]
        assert rails["account_value"] == NOW_VALUE and rails["peak_value"] == PEAK
        assert rails["drawdown_halt"] is True and rails["entries_blocked"] is True
        assert rails["weekly_loss_limit_usd"] == pytest.approx(0.15 * NOW_VALUE)


class TestTheOrderGate:
    """The executor's pre-order callback, built as the app builds it."""

    def _agent(self, tmp_path: Path, monkeypatch):
        shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
        config = AppConfig(data_dir=tmp_path / "data")
        # The starter data keeps the console's approval gate on; an earlier test
        # may have left a console registered, whose approval never comes.
        from evotrader.web import server

        monkeypatch.setattr(server, "check_interactive_approval", AsyncMock(return_value=None))
        monkeypatch.setattr(
            tools,
            "account_rails_state",
            AsyncMock(
                return_value=AccountState(
                    account_value=NOW_VALUE, value_source="broker", peak_value=PEAK
                )
            ),
        )
        return create_execution_agent(config), config

    async def test_a_new_entry_is_refused_as_policy(self, tmp_path: Path, monkeypatch) -> None:
        agent, _ = self._agent(tmp_path, monkeypatch)
        tool = MagicMock()
        tool.name = "place_equity_order"
        verdict = await agent.before_tool_callback(
            tool=tool,
            args={"symbol": "SPY", "side": "buy", "quantity": 2, "price": 500.0},
            tool_context=MagicMock(),
        )
        assert verdict is not None and verdict["allowed"] is False
        assert verdict["action"] == "ACCOUNT_LIMIT_BLOCKED"
        assert verdict["retryable"] is False and verdict["error_class"] == "POLICY"
        assert any("10%" in v for v in verdict["violations"]), "the starter's 10% limit"

    async def test_a_sell_of_what_is_held_is_not(self, tmp_path: Path, monkeypatch) -> None:
        agent, _config = self._agent(tmp_path, monkeypatch)
        tool = MagicMock()
        tool.name = "place_equity_order"
        verdict = await agent.before_tool_callback(
            tool=tool,
            args={"symbol": "SPY", "side": "sell", "quantity": 2, "price": 500.0},
            tool_context=MagicMock(),
        )
        # The starter data runs in practice mode: the gate answers with a
        # simulated fill, which is the "allowed" path.
        assert verdict is None or verdict.get("allowed", True) is True

    async def test_an_unmeasurable_account_does_not_block(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        agent, _ = self._agent(tmp_path, monkeypatch)
        monkeypatch.setattr(tools, "account_rails_state", AsyncMock(return_value=AccountState()))
        tool = MagicMock()
        tool.name = "place_equity_order"
        verdict = await agent.before_tool_callback(
            tool=tool,
            args={"symbol": "SPY", "side": "buy", "quantity": 2, "price": 500.0},
            tool_context=MagicMock(),
        )
        assert verdict is None or verdict.get("allowed", True) is True
