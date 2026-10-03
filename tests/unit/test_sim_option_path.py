"""Regression tests from an audit of the practice broker's option path (2026-10-03).

Two of the gaps it found were small enough to close at once:

- The sim priced an option position per share, while the broker prices one per
  contract (per share times the multiplier, which it names). The position sync
  reads both through ``McpPosition``, which divides by that multiplier, so a
  practice option's cost reached it at a hundredth of itself.
- The sim keys a position by ticker as well as contract id, and took the ticker
  from the agents' cache alone. A close placed after the cache had lost it (a
  restart, a change of instrument) opened a second position under "OPTION"
  instead of closing the held one.

The contract and its prices are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from evotrader.agents.tools import OPTION_ID_TO_TICKER
from evotrader.models.mcp import McpPosition
from evotrader.sim import SimBroker

_CONTRACT = "test-contract-0001"
_PREMIUM = 1.50  # a share


def _order(side: str, effect: str, qty: int = 2) -> dict[str, Any]:
    return {
        "legs": [{"option_id": _CONTRACT, "side": side, "position_effect": effect}],
        "type": "market",
        "quantity": qty,
    }


@pytest.fixture
async def broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimBroker]:
    """A practice account long two contracts on SPY, bought while the cache knew them."""
    b = SimBroker(tmp_path / "sim_broker.db")
    b.slippage_model = "none"
    await b.initialize()
    await b.deposit(10_000.0)
    b._get_live_option_bid_ask = AsyncMock(return_value=(_PREMIUM, _PREMIUM))  # type: ignore[method-assign]
    b._get_live_option_price = AsyncMock(return_value=_PREMIUM)  # type: ignore[method-assign]
    monkeypatch.setitem(OPTION_ID_TO_TICKER, _CONTRACT, "SPY")
    opened = await b.place_option_order(_order("buy", "open"))
    assert opened["data"]["status"] == "filled"
    yield b
    await b.close()


async def _positions(broker: SimBroker) -> list[dict[str, Any]]:
    return (await broker.get_option_positions(broker.account_number))["data"]["positions"]


async def test_an_option_position_is_priced_per_contract_as_the_broker_prices_one(
    broker: SimBroker,
) -> None:
    [position] = await _positions(broker)
    assert position["average_price"] == f"{_PREMIUM * 100:.4f}"
    assert position["trade_value_multiplier"] == "100.0000"
    # Read as the position sync reads it: back to the price per share.
    assert McpPosition.from_dict(position).average_price == pytest.approx(_PREMIUM)
    assert McpPosition.from_dict(position).asset_type == "OPTION"


async def test_a_close_finds_the_held_contract_without_the_agents_cache(
    broker: SimBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(OPTION_ID_TO_TICKER, _CONTRACT)

    closed = await broker.place_option_order(_order("sell", "close"))
    assert closed["data"]["status"] == "filled"
    assert closed["data"]["symbol"] == "SPY"
    assert await _positions(broker) == []

    conn = await broker._get_conn()
    async with conn.execute("SELECT DISTINCT ticker FROM sim_orders") as cursor:
        assert [row["ticker"] for row in await cursor.fetchall()] == ["SPY"]
