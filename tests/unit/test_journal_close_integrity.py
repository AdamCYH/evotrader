"""Regression tests: Journal CLOSE matching and reconciliation price integrity.

Guards against the phantom-position cascade and fabricated P&L bugs
discovered on 2026-07-14.

See: data/evolution/reviews/20260715_014042_journal_close_matching_and_reconciliation_price_integrity.md
"""

from __future__ import annotations

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


def _open_option_proposal(
    *,
    direction: TradeDirection = TradeDirection.LONG,
    option_id: str = "test-opt-001",
) -> TradeProposal:
    """Create an OPEN option proposal with full provenance."""
    return TradeProposal(
        ticker="QQQ",
        direction=direction,
        action=TradeAction.OPEN,
        quantity=1.0,
        order_type=OrderType.MARKET,
        hybrid_score=0.5,
        confidence=0.8,
        algo_signal=0.4,
        llm_signal=0.6,
        regime="range_bound",
        algo_version="v_test",
        reasoning="Test option open",
        option_id=option_id,
        option_type="put",
        strike=685.0,
        expiration="2026-08-14",
    )


def _close_option_proposal(
    *,
    direction: TradeDirection = TradeDirection.LONG,
    option_id: str = "test-opt-001",
    quantity: float = 1.0,
) -> TradeProposal:
    """Create a CLOSE option proposal."""
    return TradeProposal(
        ticker="QQQ",
        direction=direction,
        action=TradeAction.CLOSE,
        quantity=quantity,
        order_type=OrderType.MARKET,
        hybrid_score=0.5,
        confidence=0.8,
        algo_signal=0.4,
        llm_signal=0.6,
        regime="range_bound",
        algo_version="v_test",
        reasoning="Test option close",
        option_id=option_id,
        option_type="put",
        strike=685.0,
        expiration="2026-08-14",
    )


def _fill_result(price: float, option_id: str = "test-opt-001") -> OrderResult:
    """Create a FILLED order result."""
    return OrderResult(
        order_id="test-order-001",
        status=OrderStatus.FILLED,
        ticker="QQQ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        order_type=OrderType.MARKET,
        requested_quantity=1.0,
        filled_quantity=1.0,
        fill_price=price,
        slippage=0.0,
        option_id=option_id,
        option_type="put",
        strike=685.0,
        expiration="2026-08-14",
    )


@pytest.mark.asyncio
class TestCloseMatchingIgnoresDirection:
    """Finding 1: CLOSE matching should use (ticker, option_id) only."""

    async def test_close_matches_despite_direction_mismatch(self, journal: TradeJournal) -> None:
        """A CLOSE with direction=SHORT must still match a LONG open lot
        on the same option_id."""
        # Open a LONG put
        open_result = _fill_result(11.05)
        open_ids = await journal.record_trade(
            _open_option_proposal(direction=TradeDirection.LONG),
            result=open_result,
        )
        assert len(open_ids) == 1

        # Close with direction=SHORT (bearish exposure semantics mismatch)
        close_result = _fill_result(8.89)
        close_result.action = TradeAction.CLOSE
        close_ids = await journal.record_trade(
            _close_option_proposal(direction=TradeDirection.SHORT),
            result=close_result,
        )
        assert len(close_ids) == 1

        # The open trade should now be fully closed
        open_trades = await journal.get_open_trades()
        matching = [t for t in open_trades if t["option_id"] == "test-opt-001"]
        assert len(matching) == 0, "Open trade should be closed despite direction mismatch"

        # P&L should be computed using the LOT direction (LONG), not proposal direction (SHORT)
        recent = await journal.get_recent_trades(limit=10)
        close_trade = next(t for t in recent if t["id"] == close_ids[0])
        assert close_trade["related_trade_id"] == open_ids[0]
        # LONG lot: P&L = (close_price - entry_price) * qty * 100
        expected_pnl = (8.89 - 11.05) * 1.0 * 100  # = -216.0
        assert close_trade["realized_pnl"] == pytest.approx(expected_pnl, abs=0.01)


@pytest.mark.asyncio
class TestPriceSentinelElimination:
    """Finding 4: fill_price=0.0 must not produce sentinel-derived P&L."""

    async def test_zero_fill_price_stores_null_pnl(self, journal: TradeJournal) -> None:
        """When fill_price=0.0, realized_pnl must be NULL, not derived from $1.00."""
        # Open a position
        open_result = _fill_result(11.05)
        await journal.record_trade(
            _open_option_proposal(),
            result=open_result,
        )

        # Close with fill_price=0.0 (data outage scenario)
        close_result = _fill_result(0.0)
        close_result.action = TradeAction.CLOSE
        close_result.fill_price = 0.0
        close_ids = await journal.record_trade(
            _close_option_proposal(),
            result=close_result,
        )

        # P&L must be NULL, NOT the old sentinel-derived value
        recent = await journal.get_recent_trades(limit=10)
        close_trade = next(t for t in recent if t["id"] == close_ids[0])
        assert close_trade["realized_pnl"] is None, (
            f"Expected NULL realized_pnl for zero fill price, got {close_trade['realized_pnl']}"
        )

    async def test_none_fill_price_stores_null_pnl(self, journal: TradeJournal) -> None:
        """When fill_price=None and no fallback prices, realized_pnl must be NULL."""
        # Open a position
        open_result = _fill_result(8.80)
        await journal.record_trade(
            _open_option_proposal(),
            result=open_result,
        )

        # Close with no fill_price and no limit/stop prices
        close_proposal = _close_option_proposal()
        close_proposal.limit_price = None
        close_proposal.stop_price = None
        close_ids = await journal.record_trade(
            close_proposal,
            result=None,  # No result at all
        )

        recent = await journal.get_recent_trades(limit=10)
        close_trade = next(t for t in recent if t["id"] == close_ids[0])
        assert close_trade["realized_pnl"] is None


@pytest.mark.asyncio
class TestFlipBranchGuard:
    """Finding 3: Unmatched option CLOSE must NOT create a flipped OPEN."""

    async def test_unmatched_option_close_creates_audit_row(self, journal: TradeJournal) -> None:
        """When no open lot matches, record an audit CLOSE row, not a new OPEN."""
        # No open trades exist — close directly
        close_result = _fill_result(8.89)
        close_result.action = TradeAction.CLOSE
        close_ids = await journal.record_trade(
            _close_option_proposal(),
            result=close_result,
        )
        assert len(close_ids) == 1

        # Verify it was recorded as CLOSE (not flipped to OPEN)
        recent = await journal.get_recent_trades(limit=10)
        audit_trade = next(t for t in recent if t["id"] == close_ids[0])
        assert audit_trade["action"] == "CLOSE", (
            f"Expected audit CLOSE row, got action={audit_trade['action']}"
        )
        assert audit_trade["order_status"] == "UNMATCHED"
        assert "UNMATCHED_CLOSE" in (audit_trade["broker_status_reason"] or "")

        # It must NOT appear as an open trade
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 0, "Unmatched CLOSE should NOT create open positions"

    async def test_over_close_option_creates_audit_not_flip(self, journal: TradeJournal) -> None:
        """Over-closing an option beyond available quantity should create
        audit row for excess, not a flipped OPEN."""
        # Open 1 contract
        open_result = _fill_result(11.05)
        await journal.record_trade(
            _open_option_proposal(),
            result=open_result,
        )

        # Try to close 2 contracts (excess of 1)
        close_result = _fill_result(8.89)
        close_result.action = TradeAction.CLOSE
        close_ids = await journal.record_trade(
            _close_option_proposal(quantity=2.0),
            result=close_result,
        )

        # Should have 2 rows: one matched CLOSE, one audit CLOSE
        assert len(close_ids) == 2

        recent = await journal.get_recent_trades(limit=10)
        # Both should be CLOSE, not OPEN
        for cid in close_ids:
            trade = next(t for t in recent if t["id"] == cid)
            assert trade["action"] == "CLOSE"


@pytest.mark.asyncio
class TestOrderLifecycleForwarding:
    """Finding 2: Order lifecycle fields must be forwarded through CLOSE paths."""

    async def test_rejected_close_preserves_status(self, journal: TradeJournal) -> None:
        """A REJECTED CLOSE should be journaled with order_status=REJECTED,
        not silently defaulted to FILLED."""
        # Open a position
        open_result = _fill_result(11.05)
        await journal.record_trade(
            _open_option_proposal(),
            result=open_result,
        )

        # Close attempt rejected by broker
        close_result = _fill_result(8.89)
        close_result.action = TradeAction.CLOSE
        close_ids = await journal.record_trade(
            _close_option_proposal(),
            result=close_result,
            order_status="REJECTED",
            order_id="reject-order-001",
            broker_status_reason="OPTION_NOT_ENOUGH_CONTRACTS_TO_CLOSE",
        )

        recent = await journal.get_recent_trades(limit=10)
        close_trade = next(t for t in recent if t["id"] == close_ids[0])
        assert close_trade["order_status"] == "REJECTED"
        assert close_trade["order_id"] == "reject-order-001"
        assert close_trade["broker_status_reason"] == "OPTION_NOT_ENOUGH_CONTRACTS_TO_CLOSE"


@pytest.mark.asyncio
class TestGetOpenTradesHardening:
    """get_open_trades must exclude REJECTED/FAILED/UNMATCHED close legs."""

    async def test_rejected_close_does_not_reduce_remaining_qty(
        self, journal: TradeJournal
    ) -> None:
        """A REJECTED close should not reduce the open position's remaining quantity."""
        # Open 1 contract
        open_result = _fill_result(11.05)
        await journal.record_trade(
            _open_option_proposal(),
            result=open_result,
        )

        # Record a REJECTED close
        close_result = _fill_result(8.89)
        close_result.action = TradeAction.CLOSE
        await journal.record_trade(
            _close_option_proposal(),
            result=close_result,
            order_status="REJECTED",
            broker_status_reason="OPTION_NOT_ENOUGH_CONTRACTS_TO_CLOSE",
        )

        # Open trade should still show full remaining_quantity
        open_trades = await journal.get_open_trades()
        matching = [t for t in open_trades if t["option_id"] == "test-opt-001"]
        assert len(matching) == 1
        assert float(matching[0]["remaining_quantity"]) == pytest.approx(1.0)

    async def test_unmatched_close_not_in_open_trades(self, journal: TradeJournal) -> None:
        """An UNMATCHED audit CLOSE row must never appear in get_open_trades."""
        # Record an unmatched close (no open lot exists)
        close_result = _fill_result(8.89)
        close_result.action = TradeAction.CLOSE
        await journal.record_trade(
            _close_option_proposal(),
            result=close_result,
        )

        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 0
