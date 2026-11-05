"""A protective stop is ``stop_market``: the gate refuses a sell ``stop_limit``.

Found by the evolution agent's code review. The executor placed a position's
only stop as a stop-limit with its limit at the trigger, and every check let it
through. A stop-limit becomes a limit order at its trigger, so when price gaps
through the trigger it rests below the market and sells nothing, where a
stop_market order sells at the open. The
instructions said so; the rule now lives in the gate, beside the one that makes
protective orders good-till-cancelled.

Made-up ticker and prices.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader import paths
from evotrader.agents.factory import create_execution_agent
from evotrader.callbacks.risk_gate import protective_stop_type_violation
from evotrader.config import AppConfig


def _stop(**extra) -> dict:
    args = {
        "symbol": "XYZ",
        "side": "sell",
        "type": "stop_limit",
        "quantity": "7",
        "stop_price": "45.10",
        "limit_price": "45.10",
        "time_in_force": "gtc",
        **extra,
    }
    return {k: v for k, v in args.items() if v is not None}


class TestTheRule:
    def test_a_sell_stop_limit_is_refused(self) -> None:
        violation = protective_stop_type_violation(_stop())
        assert violation is not None
        assert "stop_market" in violation and "gaps through" in violation

    @pytest.mark.parametrize(
        "args",
        [
            _stop(type="stop_market", limit_price=None),
            _stop(type="STOP_MARKET"),
            _stop(type="limit"),
            _stop(side="buy"),  # a buy stop-limit opens a position
            {"legs": [{"option_id": "x", "side": "sell"}], "type": "stop_limit"},
        ],
    )
    def test_everything_else_passes(self, args) -> None:
        assert protective_stop_type_violation(args) is None

    def test_the_order_type_key_too(self) -> None:
        args = _stop()
        args["order_type"] = args.pop("type")
        assert protective_stop_type_violation(args) is not None


class TestTheExecutorsGate:
    def _agent(self, tmp_path: Path, monkeypatch):
        shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
        config = AppConfig(data_dir=tmp_path / "data")
        from evotrader.web import server

        monkeypatch.setattr(server, "check_interactive_approval", AsyncMock(return_value=None))
        return create_execution_agent(config)

    async def test_it_is_refused_before_the_broker(self, tmp_path: Path, monkeypatch) -> None:
        agent = self._agent(tmp_path, monkeypatch)
        tool = MagicMock()
        tool.name = "place_equity_order"
        verdict = await agent.before_tool_callback(
            tool=tool, args=_stop(), tool_context=MagicMock()
        )
        assert verdict is not None and verdict["allowed"] is False
        assert verdict["action"] == "PROTECTIVE_STOP_NOT_MARKET"
        assert verdict["retryable"] is True and "stop_market" in verdict["fix"]

    async def test_a_stop_market_goes_on(self, tmp_path: Path, monkeypatch) -> None:
        agent = self._agent(tmp_path, monkeypatch)
        tool = MagicMock()
        tool.name = "place_equity_order"
        verdict = await agent.before_tool_callback(
            tool=tool,
            args=_stop(type="stop_market", limit_price=None),
            tool_context=MagicMock(),
        )
        assert verdict is None or verdict.get("action") != "PROTECTIVE_STOP_NOT_MARKET"
