"""A pending exit that filled closes its lot once, through its own order.

Found by the evolution agent's code review. The executor placed a sale of a
whole lot, polled it once, recorded it PENDING and ended its turn; the order
filled minutes later. The next position check found the broker holding none of
the shares the journal still held. It counted the pending sale by its lot's
direction, as shares coming IN, so the drift read as unexplained, and it wrote
a sync row closing the lot. When the sale's own fill was then recorded, the lot
was closed twice and both rows booked a loss.

Now the check asks the broker about every pending exit before it measures (a
filled one is promoted through its own order), a pending exit counts as
shares going out, a sync row is not written while an exit in the drift's
direction cannot be looked up (for a day at most), and a filled exit that
finds a sync row already closed its lot marks that row DUPLICATE.

Made-up ticker, prices and order ids.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from evotrader.db.journal import DUPLICATE_STATUS, TradeJournal
from evotrader.db.reconciliation import ReconciliationService, signed_pending_quantity
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

TICKER = "XYZ"
ENTRY = "6f100000-0000-4000-8000-000000000001"
SALE = "6f100000-0000-4000-8000-000000000002"
STOP = "6f100000-0000-4000-8000-000000000003"
TP = "6f100000-0000-4000-8000-000000000004"


class Broker:
    """One equity position and the orders the broker knows, by state."""

    def __init__(self, quantity: float, average: float, orders: dict[str, dict] | None = None):
        self.quantity = quantity
        self.average = average
        self.orders = orders or {}
        self.lookups: list[str] = []
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
            position = {
                "symbol": TICKER,
                "quantity": str(self.quantity),
                "average_buy_price": str(self.average),
                "asset_type": "EQUITY",
            }
            payload = {"positions": [position] if self.quantity else []}
        elif name == "get_equity_orders":
            order_id = arguments.get("order_id", "")
            self.lookups.append(order_id)
            order = self.orders.get(order_id)
            payload = {"orders": [{"id": order_id, **order}] if order else []}
        else:
            payload = {"results": []}
        res.content = [MagicMock(text=json.dumps({"data": payload}))]
        return res


class Service(ReconciliationService):
    async def get_current_stock_price(self, ticker: str) -> float | None:
        return 51.0


def _proposal(action: TradeAction, quantity: float, price: float, related: int | None = None):
    return TradeProposal(
        ticker=TICKER,
        direction=TradeDirection.LONG,
        action=action,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=price,
        stop_price=price if action == TradeAction.STOP_LOSS else None,
        hybrid_score=0.5,
        confidence=0.5,
        algo_signal=0.5,
        llm_signal=0.5,
        regime="range_bound",
        algo_version="v001_initial",
        reasoning="made up",
        related_trade_id=related,
    )


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


async def _lot(journal: TradeJournal, quantity: float = 10.0) -> int:
    [lot] = await journal.record_trade(
        _proposal(TradeAction.OPEN, quantity, 55.0),
        order_status="FILLED",
        order_id=ENTRY,
        fill_source="broker",
    )
    return lot


async def _pending(journal, action, quantity, price, lot, order_id) -> int:
    [row] = await journal.record_trade(
        _proposal(action, quantity, price, related=lot), order_status="PENDING", order_id=order_id
    )
    return row


async def _rows(db) -> list[dict]:
    async with db.connection() as conn:
        cursor = await conn.execute("SELECT * FROM trades ORDER BY id")
        return [dict(r) for r in await cursor.fetchall()]


def _sync_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("fill_source") == "sync"]


class TestTheSign:
    @pytest.mark.parametrize(
        "action, direction, expected",
        [
            ("OPEN", "LONG", 3.0),
            ("OPEN", "SHORT", -3.0),
            ("CLOSE", "LONG", -3.0),
            ("STOP_LOSS", "LONG", -3.0),
            ("TAKE_PROFIT", "LONG", -3.0),
            ("CLOSE", "SHORT", 3.0),
        ],
    )
    def test_an_exit_takes_shares_away(self, action, direction, expected) -> None:
        row = {"action": action, "direction": direction, "quantity": 3}
        assert signed_pending_quantity(row) == expected


class TestTheSaleThatFilledAfterTheTurn:
    async def test_it_closes_the_lot_and_no_sync_row_is_written(self, db, journal) -> None:
        lot = await _lot(journal)
        sale = await _pending(journal, TradeAction.CLOSE, 10, 50.40, lot, SALE)
        broker = Broker(0, 0.0, {SALE: {"state": "filled", "average_price": "50.40"}})

        result = await Service(journal, broker, dry_run=False).reconcile_positions(ticker=TICKER)

        assert result["status"] == "IN_SYNC"
        rows = await _rows(db)
        assert _sync_rows(rows) == []
        row = next(r for r in rows if r["id"] == sale)
        assert row["order_status"] == "FILLED" and float(row["fill_price"]) == 50.40
        assert row["realized_pnl"] == pytest.approx((50.40 - 55.0) * 10)
        assert await journal.get_open_trades() == []

    async def test_a_filled_stop_beside_a_resting_take_profit(self, db, journal) -> None:
        """Together the two pending legs never matched the drift of one."""
        lot = await _lot(journal)
        stop = await _pending(journal, TradeAction.STOP_LOSS, 6, 49.0, lot, STOP)
        tp = await _pending(journal, TradeAction.TAKE_PROFIT, 4, 60.0, lot, TP)
        broker = Broker(
            4,
            55.0,
            {
                STOP: {"state": "filled", "average_price": "48.95"},
                TP: {"state": "confirmed"},
            },
        )

        result = await Service(journal, broker, dry_run=False).reconcile_positions(ticker=TICKER)

        assert result["status"] == "IN_SYNC"
        rows = {r["id"]: r for r in await _rows(db)}
        assert _sync_rows(list(rows.values())) == []
        assert rows[stop]["order_status"] == "FILLED"
        assert rows[tp]["order_status"] == "PENDING"
        [open_lot] = await journal.get_open_trades()
        assert open_lot["remaining_quantity"] == pytest.approx(4.0)

    async def test_an_exit_it_cannot_look_up_holds_off_a_sync_row(self, db, journal) -> None:
        lot = await _lot(journal)
        await _pending(journal, TradeAction.STOP_LOSS, 6, 49.0, lot, STOP)
        await _pending(journal, TradeAction.TAKE_PROFIT, 4, 60.0, lot, TP)
        broker = Broker(4, 55.0, {TP: {"state": "confirmed"}})  # the stop cannot be read

        result = await Service(journal, broker, dry_run=False).reconcile_positions(ticker=TICKER)

        assert result["status"] == "DRIFT_DEFERRED"
        assert _sync_rows(await _rows(db)) == []

    async def test_for_a_day_at_most(self, db, journal) -> None:
        lot = await _lot(journal)
        stop = await _pending(journal, TradeAction.STOP_LOSS, 6, 49.0, lot, STOP)
        await _pending(journal, TradeAction.TAKE_PROFIT, 4, 60.0, lot, TP)
        old = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
        async with db.transaction() as conn:
            await conn.execute("UPDATE trades SET timestamp = ? WHERE id = ?", (old, stop))
        broker = Broker(4, 55.0, {TP: {"state": "confirmed"}})

        result = await Service(journal, broker, dry_run=False).reconcile_positions(ticker=TICKER)

        assert result["status"] == "RECONCILED"
        [sync] = _sync_rows(await _rows(db))
        assert sync["quantity"] == pytest.approx(6.0)

    async def test_a_sale_that_is_the_whole_gap_is_taken_as_filled(self, db, journal) -> None:
        """Unreadable, but the broker holds exactly what it leaves: as for a pending buy."""
        lot = await _lot(journal)
        sale = await _pending(journal, TradeAction.CLOSE, 10, 50.40, lot, SALE)
        broker = Broker(0, 0.0, {})

        result = await Service(journal, broker, dry_run=False).reconcile_positions(ticker=TICKER)

        assert result["status"] == "IN_SYNC"
        rows = {r["id"]: r for r in await _rows(db)}
        assert _sync_rows(list(rows.values())) == []
        assert rows[sale]["order_status"] == "FILLED"
        assert rows[sale]["fill_source"] == "broker_position", "its price is not confirmed"

    async def test_drift_no_exit_explains_is_synced_as_before(self, db, journal) -> None:
        lot = await _lot(journal)
        await _pending(journal, TradeAction.TAKE_PROFIT, 4, 60.0, lot, TP)
        broker = Broker(7, 55.0, {TP: {"state": "confirmed"}})  # 3 sold outside the app

        result = await Service(journal, broker, dry_run=False).reconcile_positions(ticker=TICKER)

        assert result["status"] == "RECONCILED"
        [sync] = _sync_rows(await _rows(db))
        assert sync["action"] == "CLOSE" and sync["quantity"] == pytest.approx(3.0)

    async def test_practice_mode_asks_the_broker_nothing(self, db, journal) -> None:
        lot = await _lot(journal)
        await _pending(journal, TradeAction.CLOSE, 10, 50.40, lot, SALE)
        broker = Broker(0, 0.0, {SALE: {"state": "filled", "average_price": "50.40"}})

        await Service(journal, broker, dry_run=True).reconcile_positions(ticker=TICKER)

        assert SALE not in broker.lookups


class TestASyncRowAlreadyClosedTheLot:
    async def _the_double_close(self, db, journal, other_lot: bool = False) -> tuple[int, int, int]:
        lot = await _lot(journal)
        if other_lot:
            await journal.record_trade(
                _proposal(TradeAction.OPEN, 4, 54.0),
                order_status="FILLED",
                order_id="6f100000-0000-4000-8000-0000000000aa",
                fill_source="broker",
            )
        sale = await _pending(journal, TradeAction.CLOSE, 10, 50.40, lot, SALE)
        # What the old check wrote: a sync close of the whole lot.
        svc = Service(journal, None, dry_run=False)
        lot_row = next(t for t in await journal.get_open_trades() if t["id"] == lot)
        await svc._record_sync_trade(
            TICKER, "LONG", "CLOSE", 10, 51.0, lot, {"broker_cost": 0.0}, lot_row
        )
        [sync] = [r["id"] for r in _sync_rows(await _rows(db))]
        return lot, sale, sync

    async def test_the_sync_row_is_the_duplicate(self, db, journal) -> None:
        lot, sale, sync = await self._the_double_close(db, journal)

        await journal.update_order_status(SALE, "FILLED", fill_price=50.40)

        rows = {r["id"]: r for r in await _rows(db)}
        assert rows[sync]["order_status"] == DUPLICATE_STATUS
        assert rows[sync]["realized_pnl"] is None
        assert rows[sync]["broker_status_reason"].startswith(f"DUPLICATE_OF_{sale}")
        assert rows[sale]["related_trade_id"] == lot
        assert rows[sale]["realized_pnl"] == pytest.approx((50.40 - 55.0) * 10)
        total = sum(r["realized_pnl"] or 0.0 for r in rows.values())
        assert total == pytest.approx((50.40 - 55.0) * 10), "one loss, not two"

    async def test_the_sale_stays_on_its_own_lot(self, db, journal) -> None:
        """Spread first, the sale would have moved to the other lot, emptied by the sync row."""
        lot, sale, sync = await self._the_double_close(db, journal, other_lot=True)

        await journal.update_order_status(SALE, "FILLED", fill_price=50.40)

        rows = {r["id"]: r for r in await _rows(db)}
        assert rows[sale]["related_trade_id"] == lot
        assert rows[sync]["order_status"] == DUPLICATE_STATUS
        [still_open] = await journal.get_open_trades()
        assert still_open["remaining_quantity"] == pytest.approx(4.0)

    async def test_a_lot_closed_once_is_left_alone(self, db, journal) -> None:
        lot = await _lot(journal)
        await _pending(journal, TradeAction.CLOSE, 4, 50.40, lot, SALE)
        svc = Service(journal, None, dry_run=False)
        lot_row = (await journal.get_open_trades())[0]
        await svc._record_sync_trade(
            TICKER, "LONG", "CLOSE", 6, 51.0, lot, {"broker_cost": 0.0}, lot_row
        )

        await journal.update_order_status(SALE, "FILLED", fill_price=50.40)

        [sync] = _sync_rows(await _rows(db))
        assert sync["order_status"] == "FILLED", "4 + 6 is the lot's 10: nothing doubled"
