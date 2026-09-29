"""A lot's price comes from its own order, never from the position's average.

Found 2026-09-28 by the evolution agent's code review. The daily sync compared
every open lot with the broker's ``average_buy_price`` and rewrote any lot more
than 1% away. That average describes the POSITION: after an add at a different
price, each lot differs from it by construction. So the sync rewrote correct,
broker-confirmed fills to the blend on every run after an add, which moved each
lot's unrealized P&L and would have booked the difference as realized P&L when
the lot closed.

Now a lot is checked against the broker's record of ITS OWN order, the case the
check was written for (a fill journaled at the limit price), and the position
as a whole only raises a flag, ``sources_disagree``, which writes nothing.
Every number below is made up.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from evotrader.db.journal import TradeJournal
from evotrader.db.reconciliation import ReconciliationService
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

TICKER = "SPY"


class Broker:
    """The broker's answers: one equity position, and the orders it knows."""

    def __init__(self, quantity: float, average: float, orders: dict[str, float] | None = None):
        self.position = {
            "symbol": TICKER,
            "quantity": str(quantity),
            "average_buy_price": str(average),
            "asset_type": "EQUITY",
        }
        self.orders = orders or {}  # order id -> the price it filled at
        self.order_lookups: list[str] = []
        self._mcp_session_manager = MagicMock()
        self._mcp_session_manager.create_session = self._session

    async def _session(self) -> Broker:
        return self

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MagicMock:
        res = MagicMock()
        res.isError = False
        if name == "get_accounts":
            payload: dict[str, Any] = {"accounts": [{"account_number": "A1", "is_default": True}]}
        elif name == "get_equity_positions":
            payload = {"positions": [self.position]}
        elif name == "get_option_positions":
            payload = {"positions": []}
        elif name == "get_equity_orders":
            order_id = arguments.get("order_id", "")
            self.order_lookups.append(order_id)
            price = self.orders.get(order_id)
            order = {"id": order_id, "state": "filled", "average_price": str(price)}
            payload = {"orders": [order] if price is not None else []}
        else:
            payload = {"results": []}
        res.content = [MagicMock(text=json.dumps({"data": payload}))]
        return res


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


async def buy(
    journal: TradeJournal,
    quantity: float,
    price: float,
    order_id: str,
    status: str = "FILLED",
    fill_source: str | None = "broker",
) -> int:
    proposal = TradeProposal(
        ticker=TICKER,
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=price,
        hybrid_score=0.5,
        confidence=0.5,
        algo_signal=0.5,
        llm_signal=0.5,
        regime="trending_bull",
        algo_version="v001_initial",
        reasoning="test",
    )
    [trade_id] = await journal.record_trade(
        proposal, order_status=status, order_id=order_id, fill_source=fill_source
    )
    return trade_id


async def price_of(journal: TradeJournal, trade_id: int) -> float:
    row = await journal.get_trade_by_id(trade_id)
    return float(row["fill_price"] if row["fill_price"] is not None else row["price"])


class TestLotsOfAPositionBuiltFromSeveralBuys:
    async def test_are_left_as_their_orders_filled_them(self, journal) -> None:
        a = await buy(journal, 10, 100.00, "order-a")
        b = await buy(journal, 5, 96.00, "order-b")
        c = await buy(journal, 5, 88.00, "order-c")
        # 20 shares averaging 96.00: two lots sit well over 1% from it.
        broker = Broker(20, 96.00, {"order-a": 100.00, "order-b": 96.00, "order-c": 88.00})

        result = await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert [await price_of(journal, t) for t in (a, b, c)] == [100.00, 96.00, 88.00]
        assert result["cost_basis_check"] == {
            "sources_disagree": False,
            "journal_weighted_cost": 96.0,
            "broker_average_cost": 96.0,
        }

    async def test_never_take_the_position_average(self, journal) -> None:
        """With no order to ask, the lot keeps its price: the average is not a lot's."""
        a = await buy(journal, 10, 100.00, "order-a", fill_source="executor_claim")
        c = await buy(journal, 4, 88.00, "order-c", fill_source=None)
        broker = Broker(14, 96.57)  # the broker no longer returns these orders

        await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert await price_of(journal, a) == 100.00
        assert await price_of(journal, c) == 88.00

    async def test_a_disagreement_is_flagged_and_nothing_is_written(self, journal) -> None:
        a = await buy(journal, 10, 100.00, "order-a")
        broker = Broker(10, 95.00)

        result = await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert await price_of(journal, a) == 100.00
        assert result["cost_basis_check"]["sources_disagree"] is True


class TestALotIsCorrectedFromItsOwnOrder:
    async def test_a_fill_journaled_at_the_limit_price(self, journal) -> None:
        """The case this check exists for: the order filled below its limit."""
        a = await buy(journal, 10, 105.00, "order-a", fill_source="executor_claim")
        broker = Broker(10, 100.00, {"order-a": 100.00})

        await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        row = await journal.get_trade_by_id(a)
        assert float(row["fill_price"]) == 100.00 and float(row["price"]) == 100.00
        assert row["fill_source"] == "broker"
        assert "order-a" in row["broker_status_reason"]

    async def test_a_lot_the_old_check_rewrote_to_the_average_is_put_back(self, journal) -> None:
        """The old check marked its rewrites broker-confirmed; the order says otherwise."""
        a = await buy(journal, 10, 100.00, "order-a")
        c = await buy(journal, 4, 96.57, "order-c")  # rewritten to the blend
        broker = Broker(14, 96.57, {"order-a": 100.00, "order-c": 88.00})

        await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert await price_of(journal, a) == 100.00
        assert await price_of(journal, c) == 88.00

    async def test_practice_mode_asks_no_broker_about_orders(self, journal) -> None:
        a = await buy(journal, 10, 105.00, "order-a", fill_source="executor_claim")
        broker = Broker(10, 100.00, {"order-a": 100.00})

        await ReconciliationService(journal, broker, dry_run=True).reconcile_positions(
            ticker=TICKER
        )

        assert broker.order_lookups == []
        assert await price_of(journal, a) == 105.00


class TestAPendingBuyThatFilled:
    async def test_takes_its_orders_price_not_the_new_average(self, journal) -> None:
        await buy(journal, 10, 100.00, "order-a")
        p = await buy(journal, 4, 91.00, "order-p", status="PENDING", fill_source=None)
        # The broker now holds 14 at an average of (1000 + 4 x 90) / 14 = 97.14.
        broker = Broker(14, 97.14, {"order-a": 100.00, "order-p": 90.00})

        await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        row = await journal.get_trade_by_id(p)
        assert row["order_status"] == "FILLED"
        assert float(row["fill_price"]) == 90.00
        assert row["fill_source"] == "broker"

    async def test_keeps_its_own_price_when_its_order_cannot_be_read(self, journal) -> None:
        await buy(journal, 10, 100.00, "order-a")
        p = await buy(journal, 4, 91.00, "order-p", status="PENDING", fill_source=None)
        broker = Broker(14, 97.14, {"order-a": 100.00})

        await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        row = await journal.get_trade_by_id(p)
        assert row["order_status"] == "FILLED"
        assert await price_of(journal, p) == 91.00, "not the position's 97.14"
        assert row["fill_source"] == "broker_position"

    async def test_that_is_the_whole_position_takes_the_brokers_average(self, journal) -> None:
        p = await buy(journal, 4, 91.00, "order-p", status="PENDING", fill_source=None)
        broker = Broker(4, 90.00)

        await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert await price_of(journal, p) == 90.00
