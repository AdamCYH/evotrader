"""One losing decision is one loss, however many lots it closed.

Found by the evolution agent's code review. The loss streak walked journal
rows, and an exit of a position held as several lots is one broker order
written as one row per lot (each closes its own lot). One sale of four lots
read as four consecutive losses; with one more loss the streak reached the
constitution's pause threshold after two decisions, and the performance
analysis reported the same inflated run. The streak now counts closing
decisions: a real broker order (all its rows), else the row itself, lost when
its rows sum below zero, the same grouping the closing-orders count uses. The
rails report also says when a pause at the limit ends.

Made-up trades.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evotrader.callbacks.account_rails import AccountState, evaluate_account_rails
from evotrader.config import AppConfig
from evotrader.db.journal import TradeJournal
from evotrader.evolution.analyser import PerformanceAnalyser
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


async def _closed(journal, db, pnl: float, order_id: str | None, at: datetime, lot: int) -> int:
    """A closing row: its own lot (related_trade_id), the order that closed it."""
    proposal = TradeProposal(
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=3.0,
        order_type=OrderType.LIMIT,
        limit_price=10.0,
        hybrid_score=0.1,
        confidence=0.5,
        algo_signal=0.1,
        llm_signal=0.1,
        regime="trending_bull",
        algo_version="v001",
        reasoning="made up",
        timestamp=at,
    )
    row_id = (await journal.record_trade(proposal))[0]
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE trades SET realized_pnl = ?, order_id = ?, order_status = 'FILLED', "
            "timestamp = ?, related_trade_id = ? WHERE id = ?",
            (pnl, order_id, at.isoformat(), lot, row_id),
        )
    return row_id


def _now(seconds_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds_ago)


class TestTheBreakerCountsDecisions:
    async def test_a_four_lot_exit_is_one_loss(self, journal, db) -> None:
        """Four lots sold by one order, then one more losing sale: two losses, not five."""
        for lot in range(4):
            await _closed(journal, db, -30.0, "a1b2c3d4-0001", _now(60), lot=100 + lot)
        await _closed(journal, db, -10.0, "a1b2c3d4-0002", _now(30), lot=200)
        assert await journal.get_consecutive_losses() == 2
        assert await journal.get_session_consecutive_losses() == 2

    async def test_a_win_ends_the_run(self, journal, db) -> None:
        for lot in range(4):
            await _closed(journal, db, -30.0, "a1b2c3d4-0001", _now(60), lot=100 + lot)
        await _closed(journal, db, 15.0, "a1b2c3d4-0002", _now(30), lot=200)
        assert await journal.get_consecutive_losses() == 0

    async def test_a_decision_is_lost_when_its_lots_sum_below_zero(self, journal, db) -> None:
        await _closed(journal, db, -40.0, "a1b2c3d4-0001", _now(90), lot=100)
        await _closed(journal, db, 10.0, "a1b2c3d4-0001", _now(90), lot=101)  # net -30: a loss
        await _closed(journal, db, -5.0, "a1b2c3d4-0002", _now(60), lot=102)
        await _closed(journal, db, 12.0, "a1b2c3d4-0002", _now(60), lot=103)  # net +7: a win
        await _closed(journal, db, -8.0, "a1b2c3d4-0003", _now(30), lot=104)
        assert await journal.get_consecutive_losses() == 1

    async def test_placeholder_order_ids_are_separate_decisions(self, journal, db) -> None:
        """The words an agent typed with no id are not one order."""
        await _closed(journal, db, -10.0, "PENDING", _now(60), lot=100)
        await _closed(journal, db, -10.0, "PENDING", _now(30), lot=101)
        await _closed(journal, db, -10.0, None, _now(10), lot=102)
        assert await journal.get_consecutive_losses() == 3


class TestTheReportAgrees:
    def test_the_analysis_groups_the_same_way(self) -> None:
        t0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
        rows = [
            {"id": 1, "order_id": "z9-001", "realized_pnl": 20.0, "timestamp": t0.isoformat()},
            *(
                {
                    "id": 2 + i,
                    "order_id": "z9-002",
                    "realized_pnl": -30.0,
                    "timestamp": (t0 + timedelta(hours=1)).isoformat(),
                }
                for i in range(4)
            ),
            {
                "id": 9,
                "order_id": "z9-003",
                "realized_pnl": -10.0,
                "timestamp": (t0 + timedelta(hours=2)).isoformat(),
            },
        ]
        streaks = PerformanceAnalyser(None, None)._analyse_streaks(rows)
        assert streaks["max_loss_streak"] == 2
        assert streaks["current_streak_type"] == "loss"
        assert streaks["current_streak_length"] == 2
        assert streaks["max_win_streak"] == 1


class TestTheRailsSayWhenThePauseEnds:
    def _report(self, losses: int, last_loss_at: datetime | None, now: datetime) -> dict:
        constitution = AppConfig().constitution
        state = AccountState(
            account_value=5000.0,
            value_source="broker",
            peak_value=5000.0,
            consecutive_losses=losses,
            last_loss_at=last_loss_at,
        )
        return evaluate_account_rails(state, constitution, is_exit=False, now=now).report

    def test_at_the_limit_the_expiry_is_reported(self) -> None:
        breakers = AppConfig().constitution.circuit_breakers
        last = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
        report = self._report(breakers.consecutive_losses_pause, last, last + timedelta(minutes=10))
        assert report["loss_streak_limit"] == breakers.consecutive_losses_pause
        assert report["loss_streak_pause_active"] is True
        assert report["last_loss_at"] == last.isoformat()
        expires = last + timedelta(minutes=breakers.pause_duration_minutes)
        assert report["pause_expires_at"] == expires.isoformat()

    def test_lapsed_it_still_says_when(self) -> None:
        breakers = AppConfig().constitution.circuit_breakers
        last = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
        later = last + timedelta(minutes=breakers.pause_duration_minutes + 1)
        report = self._report(breakers.consecutive_losses_pause, last, later)
        assert report["loss_streak_pause_active"] is False
        assert report["pause_expires_at"] is not None

    def test_below_the_limit_no_expiry(self) -> None:
        last = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
        report = self._report(1, last, last)
        assert report["pause_expires_at"] is None
        assert report["last_loss_at"] == last.isoformat()
