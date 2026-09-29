"""A stop's rows tell what happened to the stop, and keep telling it.

Found 2026-09-28 by the evolution agent's code review, the first day after a
data repair:

* A gtc stop the executor cancelled to resize it, days after placing it, was
  labelled "Day order expired at session close (no time_in_force — protective
  orders must be gtc)". The order had a time_in_force: gtc. That message is the
  one that made an agent sell a position on 2026-09-18 instead of re-placing
  its stop.
* The label spread to every row sharing the order's id, overwriting what the
  repair had written on them.
* A stop resized to cover a buy placed in the same cycle read OVER_COVER for
  good, because the buy was still PENDING when the stop was written.

Every number and date below is made up.
"""

from __future__ import annotations

import pytest

from evotrader.agents.tools import classify_cancelled_order
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

# Tuesday 2026-03-03, before daylight saving time: 15:00Z is 10:00 ET.


class TestWhatTheOrderWasDecides:
    def test_a_cancelled_gtc_order_was_cancelled(self) -> None:
        state, reason = classify_cancelled_order(
            {
                "state": "cancelled",
                "time_in_force": "gtc",
                "created_at": "2026-03-02T15:00:00Z",  # placed Monday
                "last_transaction_at": "2026-03-05T15:30:00Z",  # cancelled Thursday
            }
        )
        assert state == "cancelled"
        assert "time_in_force" not in reason

    def test_a_day_order_dropped_at_the_close_expired(self) -> None:
        state, reason = classify_cancelled_order(
            {
                "state": "cancelled",
                "time_in_force": "gfd",
                "created_at": "2026-03-03T15:00:00Z",  # 10:00 ET
                "last_transaction_at": "2026-03-03T21:10:00Z",  # 16:10 ET
            }
        )
        assert state == "expired"
        assert "no time_in_force" not in reason, "it had one"

    def test_a_day_order_cancelled_during_the_session_was_cancelled(self) -> None:
        state, _ = classify_cancelled_order(
            {
                "state": "cancelled",
                "time_in_force": "gfd",
                "created_at": "2026-03-03T15:00:00Z",  # 10:00 ET
                "last_transaction_at": "2026-03-04T17:00:00Z",  # 12:00 ET the next day
            }
        )
        assert state == "cancelled"

    def test_an_after_hours_order_cancelled_that_evening_was_cancelled(self) -> None:
        """Placed after the close for the next session: it had not expired yet."""
        state, _ = classify_cancelled_order(
            {
                "state": "cancelled",
                "time_in_force": "gfd",
                "created_at": "2026-03-03T22:30:00Z",  # 17:30 ET
                "last_transaction_at": "2026-03-03T23:00:00Z",  # 18:00 ET
            }
        )
        assert state == "cancelled"

    def test_a_day_order_placed_after_hours_expires_at_the_next_close(self) -> None:
        state, _ = classify_cancelled_order(
            {
                "state": "cancelled",
                "time_in_force": "gfd",
                "created_at": "2026-03-03T22:30:00Z",  # 17:30 ET Tuesday
                "last_transaction_at": "2026-03-04T21:05:00Z",  # 16:05 ET Wednesday
            }
        )
        assert state == "expired"


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


def _proposal(action: TradeAction, quantity: float, price: float) -> TradeProposal:
    return TradeProposal(
        ticker="SPY",
        direction=TradeDirection.LONG,
        action=action,
        quantity=quantity,
        order_type=OrderType.STOP if action is TradeAction.STOP_LOSS else OrderType.LIMIT,
        limit_price=None if action is TradeAction.STOP_LOSS else price,
        stop_price=price if action is TradeAction.STOP_LOSS else None,
        time_in_force="gtc" if action is TradeAction.STOP_LOSS else None,
        hybrid_score=0.5,
        confidence=0.5,
        algo_signal=0.5,
        llm_signal=0.5,
        regime="trending_bull",
        algo_version="v001_initial",
        reasoning="test",
    )


async def _row(journal: TradeJournal, trade_id: int) -> dict:
    return dict(await journal.get_trade_by_id(trade_id))


class TestRowsARepairClosedOut:
    async def _setup(self, journal: TradeJournal) -> tuple[int, int, int]:
        await journal.record_trade(_proposal(TradeAction.OPEN, 10, 100.0), order_id="buy-1")
        [live] = await journal.record_trade(
            _proposal(TradeAction.STOP_LOSS, 10, 90.0), order_status="PENDING", order_id="stop-1"
        )
        [a] = await journal.record_trade(
            _proposal(TradeAction.STOP_LOSS, 4, 90.0), order_status="PENDING", order_id="stop-1"
        )
        [b] = await journal.record_trade(
            _proposal(TradeAction.STOP_LOSS, 6, 90.0), order_status="PENDING", order_id="stop-1"
        )
        async with journal._db.transaction() as conn:  # what a data repair writes
            await conn.execute(
                "UPDATE trades SET order_status = 'CANCELLED', "
                "broker_status_reason = 'REPAIRED_20260303: a duplicate row' WHERE id = ?",
                (a,),
            )
            await conn.execute(
                "UPDATE trades SET order_status = 'FAILED', "
                "broker_status_reason = 'REPAIRED_20260303: never a trade' WHERE id = ?",
                (b,),
            )
        return live, a, b

    async def test_keep_the_repairs_label_when_the_order_is_cancelled(self, journal) -> None:
        live, a, b = await self._setup(journal)

        await journal.update_order_status(
            "stop-1", "CANCELLED", broker_status_reason="gtc order cancelled at the broker"
        )

        assert (await _row(journal, live))["order_status"] == "CANCELLED"
        assert (await _row(journal, a))["broker_status_reason"].startswith("REPAIRED_")
        assert (await _row(journal, b))["order_status"] == "FAILED"

    async def test_are_not_revived_when_the_order_fills(self, journal) -> None:
        live, a, b = await self._setup(journal)

        await journal.update_order_status("stop-1", "FILLED", fill_price=90.0)

        assert (await _row(journal, live))["order_status"] == "FILLED"
        assert (await _row(journal, a))["order_status"] == "CANCELLED"
        assert (await _row(journal, b))["order_status"] == "FAILED"


class TestAStopResizedForASameCycleBuy:
    async def test_is_not_an_over_cover(self, journal) -> None:
        await journal.record_trade(_proposal(TradeAction.OPEN, 10, 100.0), order_id="buy-1")
        await journal.record_trade(
            _proposal(TradeAction.OPEN, 3, 98.0),
            order_status="PENDING",
            order_id="buy-2",
            session_id="cycle-2",
        )

        [stop] = await journal.record_trade(
            _proposal(TradeAction.STOP_LOSS, 13, 90.0),
            order_status="PENDING",
            order_id="stop-2",
            session_id="cycle-2",
        )

        reason = (await _row(journal, stop))["broker_status_reason"] or ""
        assert "OVER_COVER" not in reason
        assert "awaiting broker confirmation" in reason

    async def test_a_buy_from_another_cycle_does_not_count(self, journal) -> None:
        await journal.record_trade(_proposal(TradeAction.OPEN, 10, 100.0), order_id="buy-1")
        await journal.record_trade(
            _proposal(TradeAction.OPEN, 3, 98.0),
            order_status="PENDING",
            order_id="buy-2",
            session_id="cycle-1",
        )

        [stop] = await journal.record_trade(
            _proposal(TradeAction.STOP_LOSS, 13, 90.0),
            order_status="PENDING",
            order_id="stop-2",
            session_id="cycle-2",
        )

        assert "OVER_COVER" in (await _row(journal, stop))["broker_status_reason"]
