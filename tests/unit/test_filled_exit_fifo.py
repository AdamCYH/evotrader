"""A filled stop or take-profit is matched to the lots it sold, with its P&L.

A resting protective order is one journal row against the lot it was placed
for, whatever its size (record_trade). When the broker reports it filled, the
fill path (update_order_status) used to mark that one row FILLED and stop:

  * a stop larger than its lot closed more shares of that lot than it held,
    and the next lot's shares stayed open in the journal until the day's
    position check closed them as a sync row, at a later price, with no
    decision behind it;
  * only CLOSE rows were given a realized P&L there, so a stop or take-profit
    that filled from rest closed its lot with no P&L on record.

Made-up ticker, prices and ids.
"""

from __future__ import annotations

import pytest

from evotrader.db.journal import TradeJournal
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

STOP_ID = "5e1f0c0a-0000-4000-8000-000000000001"
TP_ID = "5e1f0c0a-0000-4000-8000-000000000002"


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


def _lot(quantity: float, price: float) -> TradeProposal:
    return TradeProposal(
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=price,
        algo_signal=0.3,
        hybrid_score=0.3,
        confidence=0.5,
        regime="trending_bull",
        algo_version="composite_v001",
    )


def _exit(action: TradeAction, quantity: float, **prices: float) -> TradeProposal:
    return TradeProposal(
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=action,
        quantity=quantity,
        order_type=OrderType.STOP if action == TradeAction.STOP_LOSS else OrderType.LIMIT,
        time_in_force="gtc",
        algo_signal=0.1,
        hybrid_score=0.1,
        confidence=0.4,
        regime="trending_bull",
        algo_version="composite_v001",
        **prices,
    )


async def _rows(journal: TradeJournal, order_id: str) -> list[dict]:
    async with journal._db.connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM trades WHERE order_id = ? ORDER BY id", (order_id,)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def _two_lots_and_a_resting_stop(journal: TradeJournal) -> tuple[int, int, int]:
    [first] = await journal.record_trade(_lot(10, 50.00))
    [second] = await journal.record_trade(_lot(5, 52.00))
    [stop] = await journal.record_trade(
        _exit(TradeAction.STOP_LOSS, 15, stop_price=45.10),
        order_status="PENDING",
        order_id=STOP_ID,
    )
    return first, second, stop


async def test_a_resting_stop_is_one_row_against_the_first_lot(journal) -> None:
    first, _, stop = await _two_lots_and_a_resting_stop(journal)
    [row] = await _rows(journal, STOP_ID)
    assert (row["id"], row["related_trade_id"], row["quantity"]) == (stop, first, 15)


async def test_the_fill_spreads_over_both_lots_with_their_own_pnl(journal) -> None:
    first, second, stop = await _two_lots_and_a_resting_stop(journal)

    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.00, filled_quantity=15)

    rows = await _rows(journal, STOP_ID)
    by_lot = {r["related_trade_id"]: r for r in rows}
    assert sorted(by_lot) == [first, second]
    assert by_lot[first]["id"] == stop, "the row keeps its own lot's shares"
    assert by_lot[first]["quantity"] == 10
    assert by_lot[second]["quantity"] == 5
    assert by_lot[second]["broker_status_reason"] == f"FIFO_SPLIT_OF {stop}"
    assert all(r["order_status"] == "FILLED" and r["fill_price"] == 45.00 for r in rows)
    assert all(r["action"] == "STOP_LOSS" for r in rows)
    assert by_lot[first]["realized_pnl"] == pytest.approx((45.00 - 50.00) * 10)
    assert by_lot[second]["realized_pnl"] == pytest.approx((45.00 - 52.00) * 5)
    assert await journal.get_open_trades() == [], "nothing left for a sync row to close"


async def test_a_second_report_of_the_fill_changes_nothing(journal) -> None:
    await _two_lots_and_a_resting_stop(journal)
    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.00, filled_quantity=15)
    before = await _rows(journal, STOP_ID)

    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.00, filled_quantity=15)
    await journal.record_trade(
        _exit(TradeAction.STOP_LOSS, 15, stop_price=45.10),
        result=None,
        order_status="FILLED",
        order_id=STOP_ID,
    )

    after = await _rows(journal, STOP_ID)
    assert [(r["id"], r["quantity"], r["realized_pnl"]) for r in after] == [
        (r["id"], r["quantity"], r["realized_pnl"]) for r in before
    ]


async def test_a_stop_that_fits_its_lot_now_has_a_pnl(journal) -> None:
    [lot] = await journal.record_trade(_lot(5, 50.00))
    await journal.record_trade(
        _exit(TradeAction.STOP_LOSS, 5, stop_price=46.00),
        order_status="PENDING",
        order_id=STOP_ID,
    )

    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.90, filled_quantity=5)

    [row] = await _rows(journal, STOP_ID)
    assert row["related_trade_id"] == lot
    assert row["realized_pnl"] == pytest.approx((45.90 - 50.00) * 5)
    assert row["holding_period_s"] is not None


async def test_a_take_profit_without_a_fill_price_waits_for_one(journal) -> None:
    await journal.record_trade(_lot(5, 50.00))
    await journal.record_trade(
        _exit(TradeAction.TAKE_PROFIT, 5, limit_price=58.00),
        order_status="PENDING",
        order_id=TP_ID,
    )

    await journal.update_order_status(TP_ID, "FILLED", fill_source="broker_position")
    [row] = await _rows(journal, TP_ID)
    assert row["order_status"] == "FILLED"
    assert row["realized_pnl"] is None, "a limit is not the price it sold at"

    await journal.update_order_status(TP_ID, "FILLED", fill_price=58.02)
    [row] = await _rows(journal, TP_ID)
    assert row["realized_pnl"] == pytest.approx((58.02 - 50.00) * 5)


async def test_a_stop_whose_lot_was_already_sold_moves_to_the_next(journal) -> None:
    [first] = await journal.record_trade(_lot(10, 50.00))
    [second] = await journal.record_trade(_lot(5, 52.00))
    await journal.record_trade(
        _exit(TradeAction.STOP_LOSS, 5, stop_price=45.10),
        order_status="PENDING",
        order_id=STOP_ID,
    )
    # A sale of 10 takes the first lot (oldest first) before the stop fills.
    await journal.record_trade(
        _exit(TradeAction.CLOSE, 10, limit_price=51.00),
        order_status="FILLED",
        order_id="5e1f0c0a-0000-4000-8000-000000000003",
    )

    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.00, filled_quantity=5)

    [row] = await _rows(journal, STOP_ID)
    assert (row["related_trade_id"], row["quantity"]) == (second, 5)
    assert f"lot #{first} had no shares left" in row["broker_status_reason"]
    assert row["realized_pnl"] == pytest.approx((45.00 - 52.00) * 5)
    assert await journal.get_open_trades() == []


async def test_shares_beyond_every_lot_stay_on_the_row(journal) -> None:
    [first] = await journal.record_trade(_lot(10, 50.00))
    [second] = await journal.record_trade(_lot(3, 52.00))
    await journal.record_trade(
        _exit(TradeAction.STOP_LOSS, 15, stop_price=45.10),
        order_status="PENDING",
        order_id=STOP_ID,
    )

    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.00, filled_quantity=15)

    rows = await _rows(journal, STOP_ID)
    assert sum(r["quantity"] for r in rows) == 15, "the order's quantity is kept"
    assert {r["related_trade_id"]: r["quantity"] for r in rows} == {first: 12, second: 3}


async def test_one_lot_and_nowhere_else_leaves_the_row_alone(journal) -> None:
    [lot] = await journal.record_trade(_lot(10, 50.00))
    await journal.record_trade(
        _exit(TradeAction.STOP_LOSS, 15, stop_price=45.10),
        order_status="PENDING",
        order_id=STOP_ID,
    )

    await journal.update_order_status(STOP_ID, "FILLED", fill_price=45.00, filled_quantity=15)

    [row] = await _rows(journal, STOP_ID)
    assert (row["related_trade_id"], row["quantity"]) == (lot, 15)
