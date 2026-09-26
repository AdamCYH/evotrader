"""Regression tests: phantom-share drift prevention and reconciliation robustness.

Covers:
1. Position audit after OPEN inserts (detects phantom-lot drift)
2. Reconciliation uses remaining_quantity (not original quantity)
3. Drift resolution closes per-lot remaining_quantity
4. Mislabelled trim creates phantom lots that are detectable
5. Existing defense: OPEN + related_trade_id reclassification (still works)

See: data/evolution/notes/carry_forward.md — 2026-08-01 finding.
"""

from __future__ import annotations

import logging
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


def _proposal(
    *,
    ticker: str = "PSQ",
    action: TradeAction = TradeAction.OPEN,
    quantity: float = 33.0,
    direction: TradeDirection = TradeDirection.LONG,
    reasoning: str = "Test trade",
    related_trade_id: int | None = None,
) -> TradeProposal:
    return TradeProposal(
        ticker=ticker,
        direction=direction,
        action=action,
        quantity=quantity,
        order_type=OrderType.MARKET,
        hybrid_score=0.5,
        confidence=0.8,
        algo_signal=-0.3,
        regime="trending_bear",
        algo_version="v019",
        reasoning=reasoning,
        related_trade_id=related_trade_id,
        timestamp=datetime.now(UTC),
    )


def _result(qty: float, fill: float) -> OrderResult:
    return OrderResult(
        order_id=f"test_{id(qty)}",
        status=OrderStatus.FILLED,
        ticker="PSQ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        order_type=OrderType.MARKET,
        requested_quantity=qty,
        filled_quantity=qty,
        fill_price=fill,
    )


# ════════════════════════════════════════════════════════════════
# 1. Position Audit
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestPositionAuditAfterOpen:
    """_audit_position_after_open() detects anomalous lot counts."""

    async def test_first_open_no_audit_log(
        self, journal: TradeJournal, caplog: pytest.LogCaptureFixture
    ) -> None:
        """First OPEN for a ticker should not emit POSITION_AUDIT."""
        with caplog.at_level(logging.INFO):
            await journal.record_trade(
                _proposal(quantity=33.0),
                result=_result(33.0, 27.50),
            )
        assert "POSITION_AUDIT" not in caplog.text

    async def test_second_open_emits_info_log(
        self, journal: TradeJournal, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Second OPEN for same ticker emits POSITION_AUDIT info log."""
        await journal.record_trade(
            _proposal(quantity=33.0),
            result=_result(33.0, 27.50),
        )
        with caplog.at_level(logging.INFO):
            await journal.record_trade(
                _proposal(quantity=15.0, reasoning="Add to position"),
                result=_result(15.0, 27.30),
            )
        assert "POSITION_AUDIT" in caplog.text
        assert "net_qty=48.0" in caplog.text

    async def test_four_lots_emits_critical_alert(
        self, journal: TradeJournal, caplog: pytest.LogCaptureFixture
    ) -> None:
        """4+ open lots triggers a CRITICAL alert."""
        for i in range(3):
            await journal.record_trade(
                _proposal(quantity=10.0, reasoning=f"Tranche {i + 1}"),
                result=_result(10.0, 27.00 + i * 0.1),
            )
        with caplog.at_level(logging.INFO):
            await journal.record_trade(
                _proposal(quantity=10.0, reasoning="Tranche 4"),
                result=_result(10.0, 27.30),
            )
        assert "POSITION_AUDIT ALERT" in caplog.text
        assert "4 open lots" in caplog.text

    async def test_audit_does_not_block_trade_on_error(self, journal: TradeJournal) -> None:
        """Audit failure must never block trade recording."""
        await journal.record_trade(
            _proposal(quantity=33.0),
            result=_result(33.0, 27.50),
        )
        # Patch get_open_trades to raise during audit
        original = journal.get_open_trades
        call_count = 0

        async def failing_get_open_trades():
            nonlocal call_count
            call_count += 1
            # First call is from record_trade's CLOSE path (not reached for OPEN).
            # The audit call happens after insert — force it to fail.
            raise RuntimeError("Simulated DB failure")

        journal.get_open_trades = failing_get_open_trades
        try:
            ids = await journal.record_trade(
                _proposal(quantity=15.0, reasoning="Should still insert"),
                result=_result(15.0, 27.30),
            )
            # Trade must still be recorded despite audit failure
            assert len(ids) == 1
        finally:
            journal.get_open_trades = original

    async def test_audit_ignores_different_ticker(
        self, journal: TradeJournal, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lots for a different ticker don't trigger audit for current ticker."""
        await journal.record_trade(
            _proposal(ticker="SPY", quantity=100.0, reasoning="SPY position"),
            result=_result(100.0, 450.0),
        )
        with caplog.at_level(logging.INFO):
            await journal.record_trade(
                _proposal(ticker="PSQ", quantity=33.0, reasoning="PSQ position"),
                result=_result(33.0, 27.50),
            )
        # Should not emit audit — this is the first PSQ lot
        assert "POSITION_AUDIT" not in caplog.text


# ════════════════════════════════════════════════════════════════
# 2. Phantom-Lot Drift Detection
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestPhantomLotDriftDetection:
    """Verify that mislabelled trims create detectable phantom lots."""

    async def test_mislabelled_trim_inflates_position(self, journal: TradeJournal) -> None:
        """A trim mislabelled as OPEN creates a phantom lot.

        This test documents the failure mode — the journal WILL record
        it as an OPEN (there's no broker-side signal to prevent it).
        The audit log + reconciliation are the correction mechanisms.
        """
        # Open 48 shares across 2 lots
        await journal.record_trade(
            _proposal(quantity=33.0),
            result=_result(33.0, 27.50),
        )
        await journal.record_trade(
            _proposal(quantity=15.0, reasoning="Add to position"),
            result=_result(15.0, 27.30),
        )

        # Mislabelled trim: agent says OPEN but it's actually reducing 24 shares
        # WITHOUT setting related_trade_id
        await journal.record_trade(
            _proposal(
                quantity=24.0,
                reasoning="Trimming 24 of 48 shares due to overconcentration",
            ),
            result=_result(24.0, 27.00),
        )

        # Journal now shows 72 shares (48 + 24 phantom), broker has 24
        open_trades = await journal.get_open_trades()
        psq_lots = [t for t in open_trades if t["ticker"] == "PSQ"]
        total = sum(float(t["remaining_quantity"]) for t in psq_lots)
        assert total == pytest.approx(72.0, abs=0.01), (
            "Mislabelled trim should create phantom lot (journal: 72, broker: 24)"
        )
        # But the audit log will have flagged this with POSITION_AUDIT

    async def test_existing_defense_catches_related_trade_id(self, journal: TradeJournal) -> None:
        """OPEN + related_trade_id is reclassified to CLOSE (existing defense)."""
        open_ids = await journal.record_trade(
            _proposal(quantity=48.0),
            result=_result(48.0, 27.50),
        )

        # Mislabelled trim WITH related_trade_id — caught by defense #1
        await journal.record_trade(
            _proposal(
                quantity=24.0,
                reasoning="Trimming 24 shares",
                related_trade_id=open_ids[0],
            ),
            result=_result(24.0, 27.00),
        )

        open_trades = await journal.get_open_trades()
        psq_lots = [t for t in open_trades if t["ticker"] == "PSQ"]
        total = sum(float(t["remaining_quantity"]) for t in psq_lots)
        assert total == pytest.approx(24.0, abs=0.01), (
            "Defense should reclassify OPEN+related_trade_id to CLOSE"
        )


# ════════════════════════════════════════════════════════════════
# 3. Reconciliation remaining_quantity
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestReconciliationUsesRemainingQuantity:
    """Reconciliation must use remaining_quantity, not original quantity."""

    async def test_partially_closed_lot_uses_remaining(self, journal: TradeJournal) -> None:
        """After partial close, remaining_quantity should be used for position sizing."""
        # Open 50 shares
        await journal.record_trade(
            _proposal(quantity=50.0),
            result=_result(50.0, 27.00),
        )

        # Close 20 shares
        await journal.record_trade(
            _proposal(action=TradeAction.CLOSE, quantity=20.0, reasoning="Partial close"),
            result=_result(20.0, 27.50),
        )

        # get_open_trades should show 30 remaining
        open_trades = await journal.get_open_trades()
        psq_lots = [t for t in open_trades if t["ticker"] == "PSQ"]
        assert len(psq_lots) == 1
        assert float(psq_lots[0]["remaining_quantity"]) == pytest.approx(30.0)

        # The remaining_quantity field (not quantity) is what reconciliation should use
        assert float(psq_lots[0]["quantity"]) == pytest.approx(50.0), (
            "Original quantity should be preserved"
        )
        assert float(psq_lots[0]["remaining_quantity"]) == pytest.approx(30.0), (
            "Remaining quantity should reflect partial close"
        )


# ════════════════════════════════════════════════════════════════
# 4. FIFO Close Correctness
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestFIFOCloseChunkQuantity:
    """FIFO close rows must write per-lot chunk_qty, not total order qty."""

    async def test_multi_lot_close_splits_correctly(self, journal: TradeJournal) -> None:
        """A 14-share close across 2 lots (9 remaining + 15 remaining)
        should produce close rows with qty=9 and qty=5."""
        # Lot A: 33 shares
        ids_a = await journal.record_trade(
            _proposal(quantity=33.0, reasoning="Lot A"),
            result=_result(33.0, 27.50),
        )
        # Lot B: 15 shares
        ids_b = await journal.record_trade(
            _proposal(quantity=15.0, reasoning="Lot B"),
            result=_result(15.0, 27.30),
        )

        # Partial close 24 from Lot A (FIFO: all from A)
        await journal.record_trade(
            _proposal(action=TradeAction.CLOSE, quantity=24.0, reasoning="Close 24"),
            result=_result(24.0, 26.80),
        )

        # Now Lot A has 9 remaining, Lot B has 15.
        # Close 14 shares — FIFO should take 9 from A, 5 from B.
        close_ids = await journal.record_trade(
            _proposal(action=TradeAction.CLOSE, quantity=14.0, reasoning="Close 14"),
            result=_result(14.0, 26.90),
        )

        # Should have 2 close rows
        assert len(close_ids) == 2

        # Verify chunk quantities
        recent = await journal.get_recent_trades(limit=20)
        close_rows = sorted(
            [t for t in recent if t["id"] in close_ids],
            key=lambda t: t["id"],
        )
        # First close row: 9 shares from Lot A (fully closes it)
        assert float(close_rows[0]["quantity"]) == pytest.approx(9.0, abs=0.01)
        assert close_rows[0]["related_trade_id"] == ids_a[0]
        # Second close row: 5 shares from Lot B
        assert float(close_rows[1]["quantity"]) == pytest.approx(5.0, abs=0.01)
        assert close_rows[1]["related_trade_id"] == ids_b[0]

        # Lot B should have 10 remaining
        open_trades = await journal.get_open_trades()
        psq_lots = [t for t in open_trades if t["ticker"] == "PSQ"]
        assert len(psq_lots) == 1
        assert float(psq_lots[0]["remaining_quantity"]) == pytest.approx(10.0, abs=0.01)

    async def test_exact_lot_close_single_row(self, journal: TradeJournal) -> None:
        """Closing exactly the remaining quantity of one lot produces 1 row."""
        await journal.record_trade(
            _proposal(quantity=42.0),
            result=_result(42.0, 27.00),
        )

        close_ids = await journal.record_trade(
            _proposal(action=TradeAction.CLOSE, quantity=42.0, reasoning="Full close"),
            result=_result(42.0, 27.50),
        )

        assert len(close_ids) == 1
        open_trades = await journal.get_open_trades()
        psq_lots = [t for t in open_trades if t["ticker"] == "PSQ"]
        assert len(psq_lots) == 0


# ════════════════════════════════════════════════════════════════
# 5. System Sync Trades Are Excluded from Metrics
# ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestSystemSyncExclusion:
    """System sync / reconciliation trades must not pollute performance metrics."""

    async def test_sync_trades_excluded_from_pnl(self, journal: TradeJournal) -> None:
        """Trades with regime=reconciliation, algo_version=system_sync
        must be excluded from P&L calculations."""
        # Record a real trade with P&L
        await journal.record_trade(
            _proposal(quantity=10.0),
            result=_result(10.0, 27.00),
        )
        await journal.record_trade(
            _proposal(action=TradeAction.CLOSE, quantity=10.0, reasoning="Close"),
            result=_result(10.0, 28.00),
        )

        # Record a sync trade (should be excluded)
        sync_proposal = _proposal(
            action=TradeAction.CLOSE,
            quantity=5.0,
            reasoning="System sync",
        )
        sync_proposal.regime = "reconciliation"
        sync_proposal.algo_version = "system_sync"
        await journal.record_trade(sync_proposal)

        # Consecutive losses should not count sync trades
        losses = await journal.get_consecutive_losses()
        # The real trade was a win (+$10), so streak should be 0
        assert losses == 0

    async def test_sync_trades_excluded_from_performance_summary(
        self, journal: TradeJournal
    ) -> None:
        """Performance summary should exclude reconciliation trades."""
        perf = await journal.get_performance_summary(days=30)
        assert perf["trade_count"] == 0  # No real trades
