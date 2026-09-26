"""Regression tests: an order tool the risk gate does not know is refused, not waved through.

Found 2026-09-25 while documenting the developer guide. The pre-execution risk
gate checks the order tools named in ``_GATED_TOOLS`` and lets every other tool
straight through. The broker's tool list is read from its server at start-up,
so a new order tool (a trailing stop, a replace-order) can appear without any
code change — and in live mode it would reach the broker with no constitution
check, no approval gate and no held-quantity check at all.

The gate now fails closed: a tool whose name says it places or changes an order,
but which the gate has no check for, is blocked until someone adds one.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evotrader import paths
from evotrader.agents.factory import create_execution_agent
from evotrader.callbacks.risk_gate import _GATED_TOOLS, is_unchecked_order_tool
from evotrader.config import AppConfig


@pytest.mark.parametrize(
    "name",
    [
        "place_trailing_stop_order",
        "replace_equity_order",
        "submit_order",
        "modify_option_order",
        "place_stock_order_v2",
    ],
)
def test_unknown_tools_that_place_or_change_orders_are_caught(name: str) -> None:
    assert is_unchecked_order_tool(name)


@pytest.mark.parametrize(
    "name",
    [
        *sorted(_GATED_TOOLS),  # checked by the gate itself
        "cancel_equity_order",  # cancels, reviews and reads place nothing
        "cancel_option_order",
        "review_equity_order",
        "review_option_order",
        "get_equity_orders",
        "get_option_orders",
        "record_trade",
        "get_open_positions",
    ],
)
def test_known_and_harmless_tools_are_not(name: str) -> None:
    assert not is_unchecked_order_tool(name)


async def test_the_executor_refuses_one(tmp_path: Path) -> None:
    shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
    agent = create_execution_agent(AppConfig(data_dir=tmp_path / "data"))
    tool = MagicMock()
    tool.name = "place_trailing_stop_order"
    verdict = await agent.before_tool_callback(
        tool=tool,
        args={"symbol": "SPY", "side": "sell", "quantity": 5, "trail_percent": 2},
        tool_context=MagicMock(),
    )
    assert verdict is not None and verdict["allowed"] is False
    assert verdict["action"] == "UNCHECKED_ORDER_TOOL"
