"""One broker order is one journal fact, however many times it is recorded.

See: data/evolution/reviews/
20261007_201905_journal_record_trade_idempotent_by_order_id_and_take_profit_coverage.md
(findings 1 and 2)

The executor records an order when it places it, from the placement response
(usually PENDING), and again after it re-checks the order by id (FILLED). Each
call wrote rows. A sell of part of a lot became two exits against that lot; a
later status update, which addresses every row sharing the order id, promoted
the first copy to FILLED too, so both carried the sale's full realized P&L. The
reports counted one losing trim as two losses, the lot read below zero, the
journal's position came out short of the broker's, and reconciliation wrote
shares at the broker's average cost to make up the difference.

Now a second record of an order moves the existing rows forward, every P&L
reader leaves out a second copy that an older journal may still hold, and the
start-up check names it. Numbers and the ticker are made up.
"""

from __future__ import annotations

import json
import logging

import pytest

from evotrader.db.journal import (
    DUPLICATE_STATUS,
    TradeJournal,
    is_broker_order_id,
    superseded_duplicate_ids,
)
from evotrader.models.trade import (
    OrderResult,
    OrderStatus,
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)

ORDER = "6f00aa11-2b3c-4d5e-8f90-1a2b3c4d5e6f"


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


def _proposal(action: TradeAction, qty: float, price: float | None = None) -> TradeProposal:
    return TradeProposal(
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=action,
        quantity=qty,
        order_type=OrderType.MARKET if price is None else OrderType.LIMIT,
        limit_price=price,
        algo_signal=0.2,
        hybrid_score=0.2,
        confidence=0.5,
        regime="trending_bull",
        algo_version="test",
    )


def _filled(price: float, qty: float, action: TradeAction = TradeAction.CLOSE) -> OrderResult:
    return OrderResult(
        order_id=ORDER,
        status=OrderStatus.FILLED,
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=action,
        order_type=OrderType.MARKET,
        requested_quantity=qty,
        filled_quantity=qty,
        fill_price=price,
    )


async def _two_lots(journal: TradeJournal) -> tuple[int, int]:
    [first] = await journal.record_trade(_proposal(TradeAction.OPEN, 10.0, 100.0))
    [second] = await journal.record_trade(_proposal(TradeAction.OPEN, 4.0, 101.0))
    return first, second


async def _exit_rows(journal: TradeJournal) -> list[dict]:
    rows = await journal.get_recent_trades(limit=50)
    return [r for r in rows if r["action"] == "CLOSE"]


async def _remaining(journal: TradeJournal) -> dict[int, float]:
    return {int(t["id"]): float(t["remaining_quantity"]) for t in await journal.get_open_trades()}


# ── The re-check sequence ─────────────────────────────────────────────────


class TestPendingThenFilled:
    """THE REGRESSION CASE: placement response, then the re-check by id."""

    async def _sequence(self, journal: TradeJournal) -> tuple[list[int], list[int]]:
        await _two_lots(journal)
        first = await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="PENDING", order_id=ORDER
        )
        second = await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0),
            result=_filled(95.0, 6.0),
            order_status="FILLED",
            order_id=ORDER,
            fill_source="broker",
        )
        # The post-cycle reconciliation: the broker confirms the order, and the
        # update reaches every row with its id. This is what turned the first
        # copy into a second FILLED exit.
        await journal.update_order_status(ORDER, "FILLED", fill_price=95.0, filled_quantity=6.0)
        return first, second

    async def test_one_row(self, journal) -> None:
        first, second = await self._sequence(journal)
        assert second == first, "the re-check returns the row it promoted"
        rows = await _exit_rows(journal)
        assert len(rows) == 1
        assert rows[0]["order_status"] == "FILLED"

    async def test_one_realized_pnl_at_the_fill_price(self, journal) -> None:
        await self._sequence(journal)
        [row] = await _exit_rows(journal)
        assert row["fill_price"] == pytest.approx(95.0)
        assert row["realized_pnl"] == pytest.approx((95.0 - 100.0) * 6.0)

    async def test_the_lots_are_right(self, journal) -> None:
        first_lot, second_lot = await _two_lots(journal)
        await self._sequence_on(journal)
        assert await _remaining(journal) == {first_lot: 4.0, second_lot: 4.0}

    async def _sequence_on(self, journal: TradeJournal) -> None:
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="PENDING", order_id=ORDER
        )
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0),
            result=_filled(95.0, 6.0),
            order_status="FILLED",
            order_id=ORDER,
        )
        await journal.update_order_status(ORDER, "FILLED", fill_price=95.0, filled_quantity=6.0)

    async def test_the_reports_count_one_loss(self, journal) -> None:
        await self._sequence(journal)
        summary = await journal.get_performance_summary(days=30)
        assert summary["trade_count"] == 1
        assert summary["total_pnl"] == pytest.approx(-30.0)
        assert summary["duplicate_order_rows"] == 0


class TestOtherRepeats:
    async def test_filled_twice_is_one_row(self, journal) -> None:
        await _two_lots(journal)
        for _ in range(2):
            await journal.record_trade(
                _proposal(TradeAction.CLOSE, 6.0),
                result=_filled(95.0, 6.0),
                order_status="FILLED",
                order_id=ORDER,
            )
        assert len(await _exit_rows(journal)) == 1

    async def test_an_entry_recorded_twice_is_one_lot(self, journal) -> None:
        """An OPEN recorded twice would be a lot that does not exist."""
        await journal.record_trade(
            _proposal(TradeAction.OPEN, 5.0, 100.0), order_status="PENDING", order_id=ORDER
        )
        await journal.record_trade(
            _proposal(TradeAction.OPEN, 5.0, 100.0),
            result=_filled(99.5, 5.0, TradeAction.OPEN),
            order_status="FILLED",
            order_id=ORDER,
        )
        await journal.update_order_status(ORDER, "FILLED", fill_price=99.5, filled_quantity=5.0)
        lots = await journal.get_open_trades()
        assert len(lots) == 1
        assert lots[0]["remaining_quantity"] == pytest.approx(5.0)
        assert lots[0]["fill_price"] == pytest.approx(99.5)

    async def test_a_filled_claim_without_a_price_waits_for_reconciliation(self, journal) -> None:
        """Promoting it here would compute P&L from a placeholder price."""
        await _two_lots(journal)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="PENDING", order_id=ORDER
        )
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="FILLED", order_id=ORDER
        )
        [row] = await _exit_rows(journal)
        assert row["order_status"] == "PENDING"
        assert row["realized_pnl"] is None

    async def test_a_cancellation_ends_the_waiting_row(self, journal) -> None:
        await _two_lots(journal)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="PENDING", order_id=ORDER
        )
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="CANCELLED", order_id=ORDER
        )
        [row] = await _exit_rows(journal)
        assert row["order_status"] == "CANCELLED"

    async def test_a_late_pending_record_does_not_undo_a_fill(self, journal) -> None:
        await _two_lots(journal)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0),
            result=_filled(95.0, 6.0),
            order_status="FILLED",
            order_id=ORDER,
        )
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="PENDING", order_id=ORDER
        )
        [row] = await _exit_rows(journal)
        assert row["order_status"] == "FILLED"
        assert row["realized_pnl"] == pytest.approx(-30.0)

    async def test_an_exit_across_two_lots_is_still_two_rows(self, journal) -> None:
        """One order, one row per lot: that is FIFO, not a duplicate."""
        await _two_lots(journal)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 12.0),
            result=_filled(105.0, 12.0),
            order_status="FILLED",
            order_id=ORDER,
        )
        rows = await _exit_rows(journal)
        assert sorted(r["quantity"] for r in rows) == [2.0, 10.0]
        summary = await journal.get_performance_summary(days=30)
        assert summary["trade_count"] == 2
        assert summary["closing_orders"] == 1
        assert summary["duplicate_order_rows"] == 0

    async def test_placeholder_ids_are_not_one_order(self, journal) -> None:
        """Two failures both recorded with the word an agent typed for an id."""
        await _two_lots(journal)
        for _ in range(2):
            await journal.record_trade(
                _proposal(TradeAction.CLOSE, 6.0), order_status="FAILED", order_id="PENDING"
            )
        assert len(await _exit_rows(journal)) == 2


class TestTheToolSaysSo:
    async def test_the_response_marks_a_re_record(self, journal) -> None:
        from evotrader.agents import tools

        await _two_lots(journal)
        base = {
            "ticker": "XYZ",
            "action": "CLOSE",
            "direction": "LONG",
            "quantity": 6,
            "order_type": "market",
            "order_id": ORDER,
            "algo_signal": 0.2,
            "hybrid_score": 0.2,
            "confidence": 0.5,
            "regime": "trending_bull",
            "algo_version": "test",
            "reasoning": "Trim.",
        }
        tools._journal = journal
        tools._current_session_id = "s1"
        tools._open_positions_cache = None
        try:
            first = await tools.record_trade(
                json.dumps({**base, "status": "PENDING", "state": "unconfirmed"})
            )
            second = await tools.record_trade(
                json.dumps(
                    {
                        **base,
                        "status": "FILLED",
                        "state": "filled",
                        "average_price": 95.0,
                        "cumulative_quantity": "6.000000",
                    }
                )
            )
        finally:
            tools._journal = None
            tools._current_session_id = None
            tools._open_positions_cache = None

        assert "deduplicated" not in first
        assert second["deduplicated"] is True
        assert second["trade_ids"] == first["trade_ids"]
        assert second["journal_order_status"] == "FILLED"
        assert len(await _exit_rows(journal)) == 1
        assert await journal.get_pending_orders() == [], "the filled order left the watch list"


# ── The status update ─────────────────────────────────────────────────────


class TestUpdateOrderStatus:
    async def test_an_order_across_lots_keeps_each_lots_quantity(self, journal) -> None:
        """The broker's quantity is the order's, not each row's."""
        await _two_lots(journal)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 12.0), order_status="PENDING", order_id=ORDER
        )
        await journal.update_order_status(ORDER, "FILLED", fill_price=105.0, filled_quantity=12.0)
        rows = await _exit_rows(journal)
        assert sorted(r["quantity"] for r in rows) == [2.0, 10.0]
        assert sum(r["realized_pnl"] for r in rows) == pytest.approx(
            (105.0 - 100.0) * 10.0 + (105.0 - 101.0) * 2.0
        )

    async def test_a_single_row_still_takes_the_brokers_quantity(self, journal) -> None:
        await _two_lots(journal)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0), order_status="PENDING", order_id=ORDER
        )
        await journal.update_order_status(ORDER, "FILLED", fill_price=95.0, filled_quantity=5.0)
        [row] = await _exit_rows(journal)
        assert row["quantity"] == pytest.approx(5.0)

    async def test_a_repaired_duplicate_is_left_alone(self, journal, db) -> None:
        first_lot, _ = await _two_lots(journal)
        keep, copy = await _seed_duplicate(journal, first_lot)
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE trades SET order_status = ?, realized_pnl = NULL WHERE id = ?",
                (DUPLICATE_STATUS, copy),
            )
        await journal.update_order_status(ORDER, "FILLED", fill_price=94.0)
        row = await journal.get_trade_by_id(copy)
        assert row["order_status"] == DUPLICATE_STATUS
        assert row["realized_pnl"] is None
        assert (await journal.get_trade_by_id(keep))["fill_price"] == pytest.approx(94.0)


# ── A journal that already holds a second copy ────────────────────────────


async def _seed_duplicate(journal: TradeJournal, lot: int) -> tuple[int, int]:
    """Two FILLED copies of one exit, as record_trade wrote them before this fix."""
    ids = []
    for _ in range(2):
        ids.append(
            await journal._insert_single_trade(
                _proposal(TradeAction.CLOSE, 6.0),
                95.0,
                6.0,
                95.0,
                None,
                "{}",
                lot,
                -30.0,
                60,
                order_status="FILLED",
                order_id=ORDER,
                fill_source="broker",
            )
        )
    return ids[1], ids[0]


class TestAJournalWithACopy:
    async def test_every_pnl_reader_counts_it_once(self, journal) -> None:
        first_lot, _ = await _two_lots(journal)
        await _seed_duplicate(journal, first_lot)

        summary = await journal.get_performance_summary(days=30)
        assert summary["trade_count"] == 1
        assert summary["total_pnl"] == pytest.approx(-30.0)
        assert summary["duplicate_order_rows"] == 1
        assert await journal.get_today_pnl() == pytest.approx(-30.0)
        assert await journal.get_pnl("week") == pytest.approx(-30.0)
        assert await journal.get_consecutive_losses() == 1
        assert await journal.get_period_win_loss_count("today") == (0, 1)
        metrics = await journal.get_period_metrics("today")
        assert metrics["pnl"] == pytest.approx(-30.0) and metrics["losses"] == 1
        history = await journal.get_realized_pnl_history()
        assert [h["realized_pnl"] for h in history] == [pytest.approx(-30.0)]

    async def test_the_start_up_check_names_it(self, journal, caplog) -> None:
        first_lot, _ = await _two_lots(journal)
        keep, copy = await _seed_duplicate(journal, first_lot)
        with caplog.at_level(logging.WARNING):
            groups = await journal.warn_on_duplicate_orders()
        assert [g["ids"] for g in groups] == [[copy, keep]]
        assert "JOURNAL DUPLICATE" in caplog.text

    async def test_the_analysis_counts_it_once_and_says_so(self, journal) -> None:
        from evotrader.evolution.analyser import PerformanceAnalyser

        first_lot, _ = await _two_lots(journal)
        _, copy = await _seed_duplicate(journal, first_lot)
        report = await PerformanceAnalyser(journal, None).full_analysis(lookback_days=30)
        overall = report["overall"]
        assert overall["trade_count"] == 1
        assert overall["total_pnl"] == pytest.approx(-30.0)
        assert overall["duplicate_order_rows"] == 1
        assert overall["closing_orders"] == 1
        assert report["order_lifecycle"]["duplicate_order_row_ids"] == [copy]

    async def test_a_new_record_of_the_order_writes_nothing(self, journal) -> None:
        first_lot, _ = await _two_lots(journal)
        await _seed_duplicate(journal, first_lot)
        await journal.record_trade(
            _proposal(TradeAction.CLOSE, 6.0),
            result=_filled(95.0, 6.0),
            order_status="FILLED",
            order_id=ORDER,
        )
        assert len(await _exit_rows(journal)) == 2, "the two old copies, nothing new"


class TestTheRules:
    @pytest.mark.parametrize(
        ("order_id", "real"),
        [
            (ORDER, True),
            ("sim_1a2b3c4d", True),
            ("PENDING", False),
            ("pending", False),
            ("N/A", False),
            ("FAILED_MAX_SHARES_EXCEEDED", False),
            ("", False),
            (None, False),
        ],
    )
    def test_a_broker_id_is_one_with_a_digit(self, order_id, real) -> None:
        assert is_broker_order_id(order_id) is real

    def test_the_in_memory_rule_matches_the_query(self) -> None:
        rows = [
            {"id": 1, "order_id": ORDER, "action": "CLOSE", "related_trade_id": 7},
            {"id": 2, "order_id": ORDER, "action": "CLOSE", "related_trade_id": 7},
            {"id": 3, "order_id": ORDER, "action": "CLOSE", "related_trade_id": 8},
            {"id": 4, "order_id": "PENDING", "action": "CLOSE", "related_trade_id": 7},
            {"id": 5, "order_id": "PENDING", "action": "CLOSE", "related_trade_id": 7},
            {
                "id": 6,
                "order_id": ORDER,
                "action": "CLOSE",
                "related_trade_id": 7,
                "order_status": "CANCELLED",
            },
        ]
        assert superseded_duplicate_ids(rows) == {1}
