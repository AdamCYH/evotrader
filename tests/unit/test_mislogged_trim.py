"""Regression tests: journal mis-logged trim creates phantom lots.

A partial sell (trim) mislabelled as action=OPEN bypasses FIFO matching
and creates a duplicate lot, inflating the apparent position by double
the trimmed quantity.  These tests guard against recurrence.

See: data/evolution/reviews/20260801_005123_journal_position_drift_and_broken_concentration_check.md
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import (
    OrderResult,
    OrderStatus,
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)


@pytest.fixture
def journal(db: Database) -> TradeJournal:
    return TradeJournal(db)


def _make_proposal(**overrides) -> TradeProposal:
    defaults = dict(
        ticker="PSQ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=84.0,
        order_type=OrderType.MARKET,
        hybrid_score=0.5,
        confidence=0.8,
        algo_signal=-0.3,
        regime="trending_bear",
        algo_version="v019",
        reasoning="Open PSQ position",
        timestamp=datetime.now(UTC),
    )
    defaults.update(overrides)
    return TradeProposal(**defaults)


def _make_result(qty: float, fill: float, action: TradeAction = TradeAction.OPEN) -> OrderResult:
    return OrderResult(
        order_id=f"test_{id(qty)}",
        status=OrderStatus.FILLED,
        ticker="PSQ",
        direction=TradeDirection.LONG,
        action=action,
        order_type=OrderType.MARKET,
        requested_quantity=qty,
        filled_quantity=qty,
        fill_price=fill,
    )


class TestMisloggedTrimRegression:
    """Guard against the phantom-lot defect from mislabelled trims."""

    async def test_open_with_related_trade_id_reclassified_to_close(
        self, journal: TradeJournal
    ) -> None:
        """An OPEN with related_trade_id is contradictory — it should be
        reclassified to CLOSE and enter FIFO matching."""
        # Open 84 shares
        open_ids = await journal.record_trade(
            _make_proposal(quantity=84.0),
            result=_make_result(84.0, 26.90),
        )
        assert len(open_ids) == 1

        # "Trim" 24 shares but with action=OPEN and related_trade_id set
        # (the mislabelled case that was creating phantom lots)
        trim_ids = await journal.record_trade(
            _make_proposal(
                action=TradeAction.OPEN,
                quantity=24.0,
                related_trade_id=open_ids[0],
                reasoning="Trim 24 shares",
            ),
            result=_make_result(24.0, 27.10, action=TradeAction.CLOSE),
        )
        assert len(trim_ids) >= 1

        # The open lot should now show 60 remaining, NOT 84+24=108
        open_trades = await journal.get_open_trades()
        psq_open = [t for t in open_trades if t["ticker"] == "PSQ"]

        total_remaining = sum(float(t["remaining_quantity"]) for t in psq_open)
        assert total_remaining == pytest.approx(60.0, abs=0.01), (
            f"Expected 60 remaining after trimming 24 from 84, "
            f"got {total_remaining} across {len(psq_open)} lots"
        )

    async def test_normal_close_still_works(self, journal: TradeJournal) -> None:
        """A properly labelled CLOSE should still work as before."""
        await journal.record_trade(
            _make_proposal(quantity=50.0),
            result=_make_result(50.0, 26.50),
        )

        close_ids = await journal.record_trade(
            _make_proposal(
                action=TradeAction.CLOSE,
                quantity=20.0,
                reasoning="Partial close",
            ),
            result=_make_result(20.0, 27.00, action=TradeAction.CLOSE),
        )
        assert len(close_ids) >= 1

        open_trades = await journal.get_open_trades()
        psq_open = [t for t in open_trades if t["ticker"] == "PSQ"]
        total_remaining = sum(float(t["remaining_quantity"]) for t in psq_open)
        assert total_remaining == pytest.approx(30.0, abs=0.01)

    async def test_genuine_add_not_reclassified(self, journal: TradeJournal) -> None:
        """A genuine OPEN without related_trade_id should NOT be
        reclassified — it's a real position add."""
        # Open 50 shares
        await journal.record_trade(
            _make_proposal(quantity=50.0),
            result=_make_result(50.0, 26.50),
        )

        # Add 30 more shares (genuine OPEN, no related_trade_id)
        await journal.record_trade(
            _make_proposal(
                quantity=30.0,
                reasoning="Add to position",
            ),
            result=_make_result(30.0, 26.80),
        )

        open_trades = await journal.get_open_trades()
        psq_open = [t for t in open_trades if t["ticker"] == "PSQ"]
        total_remaining = sum(float(t["remaining_quantity"]) for t in psq_open)
        assert total_remaining == pytest.approx(80.0, abs=0.01), (
            f"Expected 80 total (50+30) for genuine ADD, got {total_remaining}"
        )

    async def test_full_close_leaves_no_open_lots(self, journal: TradeJournal) -> None:
        """Closing the entire position should leave zero open lots."""
        await journal.record_trade(
            _make_proposal(quantity=42.0),
            result=_make_result(42.0, 26.90),
        )

        await journal.record_trade(
            _make_proposal(
                action=TradeAction.CLOSE,
                quantity=42.0,
                reasoning="Close entire position",
            ),
            result=_make_result(42.0, 27.50, action=TradeAction.CLOSE),
        )

        open_trades = await journal.get_open_trades()
        psq_open = [t for t in open_trades if t["ticker"] == "PSQ"]
        assert len(psq_open) == 0, f"Expected 0 open lots after full close, got {len(psq_open)}"
