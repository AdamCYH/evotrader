"""Regression tests: in practice mode the journal hears about every fill and cancel.

Found 2026-09-25 while simulating stop orders. The practice broker settles
resting orders at the start of most of its tool calls — a portfolio or order
lookup, the console's refresh — and an agent's cancel goes straight to it. But
the cycle's check (``check_and_journal_pending_fills``) only journaled fills it
made itself. So a stop that triggered during any other call filled in the
practice account while the journal kept showing it PENDING, and a cancelled or
expired stop stayed PENDING in the journal for good: the journal (and the
protection audit that reads it) believed in a stop that no longer existed.

The check now asks the practice broker about every order the journal is still
waiting on, the way live reconciliation asks the real broker.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from evotrader.agents import tools
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal
from evotrader.sim import SimBroker, SimBrokerProxy

_REGULAR_SESSION = "2026-09-24T10:30:00-04:00"
_EARLIER_SESSION = "2026-09-22 14:00:00"  # UTC, as the sim stores placement times


def _quote(broker: SimBroker, bid: float, ask: float) -> None:
    broker._get_live_bid_ask = AsyncMock(return_value=(bid, ask))  # type: ignore[method-assign]
    broker._get_live_price = AsyncMock(return_value=bid)  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _regular_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOTRADER_MOCK_TIME", _REGULAR_SESSION)


@pytest.fixture
async def broker(tmp_path: Path) -> AsyncIterator[SimBroker]:
    b = SimBroker(tmp_path / "sim_broker.db")
    b.slippage_model = "none"
    await b.initialize()
    await b.deposit(10_000.0)
    _quote(b, 150.00, 150.10)
    await b.place_equity_order({"symbol": "SPY", "side": "buy", "type": "market", "quantity": 10})
    yield b
    await b.close()


@pytest.fixture
async def journal(db: Any, broker: SimBroker, monkeypatch: pytest.MonkeyPatch) -> TradeJournal:
    """The journal, holding the entry the stops protect."""
    j = TradeJournal(db)
    monkeypatch.setattr(tools, "_journal", j)
    monkeypatch.setattr(tools, "_sim_proxy", SimBrokerProxy(broker))
    monkeypatch.setattr(tools, "_current_session_id", "practice")
    await j.record_trade(
        TradeProposal(
            ticker="SPY",
            direction=TradeDirection.LONG,
            action=TradeAction.OPEN,
            quantity=10,
            order_type=OrderType.MARKET,
            limit_price=150.10,
            algo_signal=0.3,
            hybrid_score=0.3,
            confidence=0.5,
            regime="trending_bull",
            algo_version="test",
        )
    )
    return j


async def _journaled_stop(
    broker: SimBroker, *, tif: str = "gtc", order_id: str | None = None
) -> str:
    """Place a sell stop in the practice account and journal it as the executor does."""
    if order_id is None:
        placed = (
            await broker.place_equity_order(
                {
                    "symbol": "SPY",
                    "side": "sell",
                    "type": "stop_market",
                    "stop_price": "142.50",
                    "quantity": 3,
                    "time_in_force": tif,
                }
            )
        )["data"]
        order_id, state = placed["id"], placed["state"]
    else:
        state = "confirmed"
    await tools.record_trade(
        json.dumps(
            {
                "ticker": "SPY",
                "action": "STOP_LOSS",
                "direction": "LONG",
                "quantity": 3,
                "order_type": "stop_market",
                "stop_price": 142.50,
                "time_in_force": tif,
                "order_id": order_id,
                "status": state,
                "cumulative_quantity": "0",
                "algo_signal": 0.3,
                "hybrid_score": 0.3,
                "confidence": 0.5,
                "regime": "trending_bull",
                "algo_version": "test",
            }
        )
    )
    return order_id


async def _status(journal: TradeJournal, order_id: str) -> str:
    rows = [r for r in await journal.get_recent_trades(limit=20) if r["order_id"] == order_id]
    assert rows, f"no journal row for {order_id}"
    return rows[0]["order_status"]


async def test_a_fill_made_during_another_call_reaches_the_journal(broker, journal) -> None:
    order_id = await _journaled_stop(broker)
    assert await _status(journal, order_id) == "PENDING"

    _quote(broker, 141.00, 141.10)
    await broker.get_portfolio(broker.account_number)  # the console refresh fills it
    assert (await broker.get_order_status(order_id))["data"]["state"] == "filled"

    result = await tools.check_and_journal_pending_fills()
    assert await _status(journal, order_id) == "FILLED"
    assert result["fills_count"] == 1


async def test_an_agents_cancel_reaches_the_journal(broker, journal) -> None:
    order_id = await _journaled_stop(broker)
    # The tool name and route the agents use.
    await SimBrokerProxy(broker).call_tool_raw(
        "cancel_equity_order", {"order_id": order_id}, original_session=None
    )

    await tools.check_and_journal_pending_fills()
    assert await _status(journal, order_id) == "CANCELLED"


async def test_an_expired_day_stop_is_expired_in_the_journal(broker, journal) -> None:
    order_id = await _journaled_stop(broker, tif="gfd")
    async with broker._transaction() as conn:
        await conn.execute(
            "UPDATE sim_orders SET timestamp = ? WHERE id = ?", (_EARLIER_SESSION, order_id)
        )

    result = await tools.check_and_journal_pending_fills()
    assert result["cancelled_count"] == 1
    assert await _status(journal, order_id) == "EXPIRED"


async def test_an_order_the_practice_broker_never_saw_is_left_alone(broker, journal) -> None:
    order_id = await _journaled_stop(broker, order_id="not-a-sim-order")
    await tools.check_and_journal_pending_fills()
    assert await _status(journal, order_id) == "PENDING"
