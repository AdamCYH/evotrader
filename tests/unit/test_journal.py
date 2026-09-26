"""Tests for the trade journal database operations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import (
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)


@pytest.fixture
def journal(db: Database) -> TradeJournal:
    return TradeJournal(db)


@pytest.fixture
def sample_proposal() -> TradeProposal:
    return TradeProposal(
        ticker="SPY",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=10.0,
        order_type=OrderType.MARKET,
        hybrid_score=0.65,
        confidence=0.80,
        algo_signal=0.55,
        llm_signal=0.75,
        regime="range_bound",
        algo_version="v001_initial",
        reasoning="RSI oversold at 28 with bullish news divergence",
        timestamp=datetime.now(UTC),
    )


class TestTradeJournal:
    async def test_record_and_retrieve_trade(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        trade_ids = await journal.record_trade(sample_proposal)
        assert trade_ids[0] > 0

        recent = await journal.get_recent_trades(limit=1)
        assert len(recent) == 1
        assert recent[0]["ticker"] == "SPY"
        assert recent[0]["direction"] == "LONG"

    async def test_update_outcome(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        trade_ids = await journal.record_trade(sample_proposal)
        await journal.update_outcome(trade_ids[0], realized_pnl=150.0, holding_period_seconds=3600)

        recent = await journal.get_recent_trades(limit=1)
        assert recent[0]["realized_pnl"] == 150.0
        assert recent[0]["holding_period_s"] == 3600

    async def test_today_pnl(self, journal: TradeJournal, sample_proposal: TradeProposal) -> None:
        # Record a trade with today's timestamp
        proposal = sample_proposal.model_copy(update={"timestamp": datetime.now(UTC)})
        trade_ids = await journal.record_trade(proposal)
        await journal.update_outcome(trade_ids[0], realized_pnl=100.0, holding_period_seconds=600)

        pnl = await journal.get_today_pnl()
        assert pnl == 100.0

    async def test_consecutive_losses(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        # Record 3 losing trades
        for _i in range(3):
            tids = await journal.record_trade(sample_proposal)
            await journal.update_outcome(tids[0], realized_pnl=-50.0, holding_period_seconds=300)

        streak = await journal.get_consecutive_losses()
        assert streak == 3

    async def test_trade_count_today(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        proposal = sample_proposal.model_copy(update={"timestamp": datetime.now(UTC)})
        await journal.record_trade(proposal)
        await journal.record_trade(proposal)

        count = await journal.get_trade_count_today()
        assert count == 2

    async def test_performance_summary_empty(self, journal: TradeJournal) -> None:
        summary = await journal.get_performance_summary()
        assert summary["trade_count"] == 0
        assert summary["win_rate"] == 0.0

    async def test_row_factory_isolation(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        # 1. Record a trade
        trade_ids = await journal.record_trade(sample_proposal)
        await journal.update_outcome(trade_ids[0], realized_pnl=50.0, holding_period_seconds=100)

        # 2. Get recent trades (uses dict row factory)
        recent = await journal.get_recent_trades(limit=1)
        assert len(recent) == 1
        assert isinstance(recent[0], dict)

        # 3. Get performance summary (should NOT fail with KeyError: 0 due to row factory pollution)
        summary = await journal.get_performance_summary()
        assert summary["trade_count"] == 1
        assert summary["total_pnl"] == 50.0

    async def test_record_partial_close_trade_inherits_context(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """Position properties inherit across an exit; ORDER properties do not.

        `order_type` was removed from the inherited set on 2026-09-14 (review
        20260914_225229 finding 4): a live trade was a `limit` entry, so its
        stop-loss child inherited 'limit' and a broker-accepted stop_market was
        permanently recorded as a limit order. algo_version and regime describe
        the POSITION and inherit sensibly; order_type describes THIS order and
        changes meaning entirely when the action changes.
        """
        # A LIMIT entry, so inheritance would be visible if it still happened.
        limit_entry = sample_proposal.model_copy(
            update={"order_type": OrderType.LIMIT, "limit_price": 420.0}
        )
        open_ids = await journal.record_trade(limit_entry)
        assert open_ids[0] > 0

        # 2. Construct a partial close proposal with NO order_type
        close_proposal = TradeProposal(
            ticker="SPY",
            action=TradeAction.CLOSE,
            related_trade_id=open_ids[0],
        )

        # 3. Record the close trade
        close_ids = await journal.record_trade(close_proposal)
        assert close_ids[0] > 0

        # 4. Verify the database entry has inherited context
        recent = await journal.get_recent_trades(limit=10)
        close_trade = next(t for t in recent if t["id"] == close_ids[0])
        assert close_trade["direction"] == "LONG"
        assert close_trade["quantity"] == 10.0
        assert close_trade["order_type"] != "limit", (
            "order_type must NOT be inherited from the entry across an exit"
        )
        assert close_trade["algo_version"] == "v001_initial"
        assert close_trade["regime"] == "range_bound"
        assert close_trade["algo_signal"] == 0.55
        assert close_trade["hybrid_score"] == 0.65
        assert close_trade["confidence"] == 0.80
        assert "Exit trade linked to ID" in close_trade["reasoning"]

    async def test_performance_summary_excludes_reconciliation(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """Regression: reconciliation/system_sync closes must NOT pollute
        performance summary.

        See: data/evolution/reviews/20260702_210154_reconciliation_pollutes_performance_summary_and_circuit_breaker.md
        """
        now = datetime.now(UTC)

        # 1. Record a genuine winning trade
        genuine = sample_proposal.model_copy(update={"timestamp": now})
        tids = await journal.record_trade(genuine)
        await journal.update_outcome(tids[0], realized_pnl=200.0, holding_period_seconds=600)

        # 2. Record a reconciliation forced close (should be excluded)
        recon = sample_proposal.model_copy(
            update={
                "timestamp": now,
                "regime": "reconciliation",
                "algo_version": "system_sync",
                "reasoning": "System Sync: manual reconciliation",
            }
        )
        recon_ids = await journal.record_trade(recon)
        await journal.update_outcome(recon_ids[0], realized_pnl=-500.0, holding_period_seconds=0)

        summary = await journal.get_performance_summary(days=30)

        # Only the genuine trade should be counted
        assert summary["trade_count"] == 1
        assert summary["total_pnl"] == 200.0
        assert summary["win_rate"] == 1.0

    async def test_consecutive_losses_excludes_reconciliation(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """Regression: reconciliation forced closes must NOT inflate the
        consecutive loss streak used by the circuit breaker.

        See: data/evolution/reviews/20260702_210154_reconciliation_pollutes_performance_summary_and_circuit_breaker.md
        """
        # 1. Record a genuine winning trade (breaks any streak)
        tids = await journal.record_trade(sample_proposal)
        await journal.update_outcome(tids[0], realized_pnl=100.0, holding_period_seconds=300)

        # 2. Record 5 reconciliation losses — these should NOT count
        for _ in range(5):
            recon = sample_proposal.model_copy(
                update={
                    "regime": "reconciliation",
                    "algo_version": "system_sync",
                }
            )
            rids = await journal.record_trade(recon)
            await journal.update_outcome(rids[0], realized_pnl=-50.0, holding_period_seconds=0)

        streak = await journal.get_consecutive_losses()
        # Streak should be 0 — the most recent genuine trade was a win
        assert streak == 0

    async def test_last_loss_timestamp_returns_most_recent(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """get_last_loss_timestamp should return the timestamp of the most
        recent losing trade — used by the circuit breaker to enforce the
        pause_duration_minutes cooldown.

        Bug fix: prior to this change the circuit breaker checked consecutive
        losses but NEVER checked whether the pause had expired, causing a
        permanent lockout until a winning trade.
        """
        from datetime import timedelta

        now = datetime.now(UTC)

        # Record a loss 90 minutes ago and a loss 30 minutes ago
        old_loss = sample_proposal.model_copy(update={"timestamp": now - timedelta(minutes=90)})
        old_ids = await journal.record_trade(old_loss)
        await journal.update_outcome(old_ids[0], realized_pnl=-50.0, holding_period_seconds=300)

        recent_loss = sample_proposal.model_copy(update={"timestamp": now - timedelta(minutes=30)})
        recent_ids = await journal.record_trade(recent_loss)
        await journal.update_outcome(recent_ids[0], realized_pnl=-25.0, holding_period_seconds=300)

        last_ts = await journal.get_last_loss_timestamp()
        assert last_ts is not None
        # Should be the 30-minute-ago loss, not the 90-minute-ago one
        expected = now - timedelta(minutes=30)
        assert abs((last_ts - expected).total_seconds()) < 2

    async def test_last_loss_timestamp_none_when_no_losses(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """Should return None when there are no losing trades."""
        # Record only a winning trade
        tids = await journal.record_trade(sample_proposal)
        await journal.update_outcome(tids[0], realized_pnl=100.0, holding_period_seconds=300)

        last_ts = await journal.get_last_loss_timestamp()
        assert last_ts is None

    async def test_last_loss_timestamp_excludes_reconciliation(
        self, journal: TradeJournal, sample_proposal: TradeProposal
    ) -> None:
        """Reconciliation losses should not count as 'last loss' for
        circuit breaker purposes."""
        from datetime import timedelta

        now = datetime.now(UTC)

        # Record a genuine loss 2 hours ago
        genuine_loss = sample_proposal.model_copy(update={"timestamp": now - timedelta(hours=2)})
        tids = await journal.record_trade(genuine_loss)
        await journal.update_outcome(tids[0], realized_pnl=-50.0, holding_period_seconds=300)

        # Record a reconciliation loss 5 minutes ago — should be ignored
        recon = sample_proposal.model_copy(
            update={
                "timestamp": now - timedelta(minutes=5),
                "regime": "reconciliation",
                "algo_version": "system_sync",
            }
        )
        rids = await journal.record_trade(recon)
        await journal.update_outcome(rids[0], realized_pnl=-200.0, holding_period_seconds=0)

        last_ts = await journal.get_last_loss_timestamp()
        assert last_ts is not None
        # Should be the 2-hour-ago genuine loss, not the 5-minute recon
        expected = now - timedelta(hours=2)
        assert abs((last_ts - expected).total_seconds()) < 2


class TestRecommendations:
    """Tests for PerformanceAnalyser recommendation generation."""

    def test_recommendations_suppressed_with_low_trade_count(self) -> None:
        """Regression: win-rate 'tighten thresholds' advice should NOT fire
        when trade_count < 10 — insufficient sample size.

        See: data/evolution/reviews/20260702_210154_reconciliation_pollutes_performance_summary_and_circuit_breaker.md
        """
        from evotrader.evolution.analyser import PerformanceAnalyser

        analyser = PerformanceAnalyser(journal=None, metrics=None)

        # Low trade count with bad win rate — should NOT recommend tightening
        analysis_low = {
            "overall": {"trade_count": 3, "win_rate": 0.0},
            "regime_breakdown": {},
            "signal_attribution": {"algo_dominant": {}, "llm_dominant": {}},
        }
        recs = analyser._generate_recommendations(analysis_low)
        assert not any("45%" in r for r in recs), "Should not recommend tightening with < 10 trades"

        # High trade count with bad win rate — SHOULD recommend tightening
        analysis_high = {
            "overall": {"trade_count": 15, "win_rate": 0.30},
            "regime_breakdown": {},
            "signal_attribution": {"algo_dominant": {}, "llm_dominant": {}},
        }
        recs = analyser._generate_recommendations(analysis_high)
        assert any("45%" in r for r in recs), (
            "Should recommend tightening with >= 10 trades and low win rate"
        )


# ═══════════════════════════════════════════════════════════════
# Pending Orders Persistence Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestPendingOrders:
    """Test the pending_orders table lifecycle."""

    async def test_save_and_get_pending_order(self, journal: TradeJournal):
        """Saving a pending order should make it retrievable."""
        trade_json = '{"ticker": "QQQ", "action": "OPEN", "direction": "LONG"}'
        row_id = await journal.save_pending_order("order-001", trade_json, "session-abc")

        assert row_id is not None

        pending = await journal.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["order_id"] == "order-001"
        assert pending[0]["session_id"] == "session-abc"
        assert pending[0]["trade_json"] == trade_json
        assert pending[0]["status"] == "PENDING"

    async def test_upsert_on_conflict(self, journal: TradeJournal):
        """Re-saving with same order_id should update, not duplicate."""
        await journal.save_pending_order("order-001", '{"v": 1}', "s1")
        await journal.save_pending_order("order-001", '{"v": 2}', "s2")

        pending = await journal.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["trade_json"] == '{"v": 2}'
        assert pending[0]["session_id"] == "s2"

    async def test_resolve_pending_order(self, journal: TradeJournal):
        """Resolving a pending order should exclude it from get_pending_orders."""
        await journal.save_pending_order("order-001", "{}", None)
        await journal.save_pending_order("order-002", "{}", None)

        await journal.resolve_pending_order("order-001", "FILLED")

        pending = await journal.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["order_id"] == "order-002"

    async def test_delete_pending_order(self, journal: TradeJournal):
        """Deleting a pending order should remove it entirely."""
        await journal.save_pending_order("order-001", "{}", None)
        await journal.delete_pending_order("order-001")

        pending = await journal.get_pending_orders()
        assert len(pending) == 0

    async def test_delete_nonexistent_is_noop(self, journal: TradeJournal):
        """Deleting a non-existent order should not raise."""
        await journal.delete_pending_order("does-not-exist")  # Should not raise

    async def test_multiple_pending_orders(self, journal: TradeJournal):
        """Multiple pending orders should all be returned in creation order."""
        for i in range(5):
            await journal.save_pending_order(f"order-{i:03d}", f'{{"idx": {i}}}', None)

        pending = await journal.get_pending_orders()
        assert len(pending) == 5
        assert [p["order_id"] for p in pending] == [f"order-{i:03d}" for i in range(5)]

    async def test_resolved_orders_not_returned(self, journal: TradeJournal):
        """Only PENDING orders should be returned, not resolved ones."""
        await journal.save_pending_order("order-001", "{}", None)
        await journal.save_pending_order("order-002", "{}", None)
        await journal.save_pending_order("order-003", "{}", None)

        await journal.resolve_pending_order("order-001", "FILLED")
        await journal.resolve_pending_order("order-003", "REJECTED")

        pending = await journal.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["order_id"] == "order-002"


class TestPriceReconciliation:
    """Regression tests for PENDING → FILLED price update.

    Reproduces the bug where limit_price ($15.20) was stored as the
    canonical `price` and never updated when the broker fill came in
    at a different price ($10.86), causing incorrect P&L.
    """

    async def test_update_order_status_corrects_price_column(
        self,
        journal: TradeJournal,
    ) -> None:
        """When fill_price arrives via reconciliation, both `fill_price`
        AND `price` columns should be updated."""
        # 1. Record a PENDING limit order at $15.20
        proposal = TradeProposal(
            ticker="SPY",
            direction=TradeDirection.LONG,
            action=TradeAction.OPEN,
            quantity=100.0,
            order_type=OrderType.LIMIT,
            limit_price=15.20,
            algo_signal=0.5,
            regime="range_bound",
            algo_version="v001_initial",
            reasoning="Test limit order",
            timestamp=datetime(2026, 7, 16, 14, 0, 0),
        )
        trade_ids = await journal.record_trade(
            proposal,
            order_status="PENDING",
            order_id="order-abc",
        )
        assert len(trade_ids) == 1

        # Verify initial state: price = limit_price
        trades = await journal.get_recent_trades(limit=1)
        assert trades[0]["price"] == 15.20
        assert trades[0]["order_status"] == "PENDING"

        # 2. Reconciliation promotes to FILLED at actual fill $10.86
        rows = await journal.update_order_status(
            order_id="order-abc",
            new_status="FILLED",
            fill_price=10.86,
        )
        assert rows == 1

        # 3. Verify both columns updated
        trades = await journal.get_recent_trades(limit=1)
        assert trades[0]["fill_price"] == pytest.approx(10.86)
        assert trades[0]["price"] == pytest.approx(10.86), (
            "price column must be updated to fill_price on reconciliation"
        )
        assert trades[0]["limit_price"] == pytest.approx(15.20), (
            "limit_price column must preserve the agent's original requested price"
        )
        assert trades[0]["order_status"] == "FILLED"

    async def test_pnl_uses_fill_price_not_limit(
        self,
        journal: TradeJournal,
    ) -> None:
        """P&L should be computed against the actual fill price, not
        the agent's requested limit price."""
        # 1. Open at fill $10.86 (simulating reconciled trade)
        from evotrader.models.trade import OrderResult, OrderStatus

        open_proposal = TradeProposal(
            ticker="SOXL",
            direction=TradeDirection.LONG,
            action=TradeAction.OPEN,
            quantity=1.0,
            order_type=OrderType.LIMIT,
            limit_price=15.20,
            algo_signal=0.3,
            regime="range_bound",
            algo_version="v001_initial",
            reasoning="Test entry",
            timestamp=datetime(2026, 7, 16, 14, 0, 0),
        )
        result = OrderResult(
            order_id="fill-test",
            status=OrderStatus.FILLED,
            ticker="SOXL",
            direction=TradeDirection.LONG,
            action=TradeAction.OPEN,
            order_type=OrderType.LIMIT,
            requested_quantity=1.0,
            filled_quantity=1.0,
            fill_price=10.86,
        )
        await journal.record_trade(open_proposal, result=result)

        # 2. Close at $12.00
        close_proposal = TradeProposal(
            ticker="SOXL",
            direction=TradeDirection.LONG,
            action=TradeAction.CLOSE,
            quantity=1.0,
            order_type=OrderType.MARKET,
            limit_price=12.00,
            algo_signal=-0.1,
            regime="range_bound",
            algo_version="v001_initial",
            reasoning="Test exit",
            timestamp=datetime(2026, 7, 16, 15, 0, 0),
        )
        close_result = OrderResult(
            order_id="fill-close",
            status=OrderStatus.FILLED,
            ticker="SOXL",
            direction=TradeDirection.LONG,
            action=TradeAction.CLOSE,
            order_type=OrderType.MARKET,
            requested_quantity=1.0,
            filled_quantity=1.0,
            fill_price=12.00,
        )
        close_ids = await journal.record_trade(close_proposal, result=close_result)

        # 3. P&L should be $12.00 - $10.86 = $1.14 (not $12.00 - $15.20 = -$3.20)
        trades = await journal.get_recent_trades(limit=10)
        close_trade = next(t for t in trades if t["id"] == close_ids[0])
        assert close_trade["realized_pnl"] == pytest.approx(1.14, abs=0.01), (
            f"P&L should use fill_price $10.86, not limit_price $15.20. "
            f"Got {close_trade['realized_pnl']}"
        )

    async def test_pending_order_price_columns_after_reconciliation(
        self,
        journal: TradeJournal,
    ) -> None:
        """After reconciliation, get_open_trades() should reflect the
        actual fill price for position valuation."""
        proposal = TradeProposal(
            ticker="SOXL",
            direction=TradeDirection.LONG,
            action=TradeAction.OPEN,
            quantity=50.0,
            order_type=OrderType.LIMIT,
            limit_price=15.20,
            algo_signal=0.5,
            regime="trending",
            algo_version="v001_initial",
            reasoning="Test",
            timestamp=datetime(2026, 7, 16, 14, 0, 0),
        )
        await journal.record_trade(
            proposal,
            order_status="PENDING",
            order_id="recon-test",
        )

        # Shouldn't appear in open trades while PENDING
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 0

        # Reconcile to FILLED at $10.86
        await journal.update_order_status(
            order_id="recon-test",
            new_status="FILLED",
            fill_price=10.86,
        )

        # Now should appear with the corrected price
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["price"] == pytest.approx(10.86)
        assert open_trades[0]["fill_price"] == pytest.approx(10.86)

    async def test_get_trade_by_id_and_lifecycle_pnl_filtering(self, db, journal):
        """Test get_trade_by_id and ensure cancelled/failed trades do not count toward win/loss."""
        open_proposal = TradeProposal(
            ticker="QQQ",
            action=TradeAction.OPEN,
            direction=TradeDirection.LONG,
            quantity=10.0,
            limit_price=100.0,
            regime="range_bound",
            algo_version="v001",
            reasoning="Open test",
            timestamp=datetime(2026, 7, 16, 10, 0, 0),
        )
        open_ids = await journal.record_trade(
            open_proposal, order_status="FILLED", order_id="open-1"
        )
        open_id = open_ids[0]

        # Test get_trade_by_id
        t_row = await journal.get_trade_by_id(open_id)
        assert t_row is not None
        assert t_row["ticker"] == "QQQ"
        assert t_row["price"] == 100.0

        # Non-existent trade ID returns None
        assert await journal.get_trade_by_id(99999) is None

        # Record a failed CLOSE attempt
        failed_close = TradeProposal(
            ticker="QQQ",
            action=TradeAction.CLOSE,
            direction=TradeDirection.LONG,
            quantity=5.0,
            limit_price=110.0,
            regime="range_bound",
            algo_version="v001",
            reasoning="Failed close attempt",
            timestamp=datetime(2026, 7, 16, 11, 0, 0),
        )
        failed_ids = await journal.record_trade(
            failed_close, order_status="FAILED", order_id="failed-close-1"
        )
        failed_id = failed_ids[0]

        # Failed close should have None realized_pnl
        failed_row = await journal.get_trade_by_id(failed_id)
        assert failed_row["realized_pnl"] is None

        # Win/Loss counts should be 0W / 0L
        wins, losses = await journal.get_period_win_loss_count("all")
        assert wins == 0
        assert losses == 0
        assert await journal.get_pnl("all") == 0.0

        # Record a PENDING CLOSE attempt, then promote to FILLED
        pending_close = TradeProposal(
            ticker="QQQ",
            action=TradeAction.CLOSE,
            direction=TradeDirection.LONG,
            quantity=5.0,
            limit_price=115.0,
            regime="range_bound",
            algo_version="v001",
            reasoning="Pending close attempt",
            timestamp=datetime(2026, 7, 16, 12, 0, 0),
        )
        pending_ids = await journal.record_trade(
            pending_close, order_status="PENDING", order_id="pending-close-1"
        )
        pending_id = pending_ids[0]

        pending_row = await journal.get_trade_by_id(pending_id)
        assert pending_row["realized_pnl"] is None

        # Promote pending close to FILLED at $115.00
        await journal.update_order_status("pending-close-1", new_status="FILLED", fill_price=115.0)

        # Promoted trade should have realized PnL = (115 - 100) * 5 = +$75.00
        filled_row = await journal.get_trade_by_id(pending_id)
        assert filled_row["order_status"] == "FILLED"
        assert filled_row["realized_pnl"] == pytest.approx(75.0)

        # Win/loss counts should reflect 1W / 0L and +$75.00 PnL
        wins, losses = await journal.get_period_win_loss_count("all")
        assert wins == 1
        assert losses == 0
        assert await journal.get_pnl("all") == pytest.approx(75.0)
