"""Regression tests: order_status lifecycle tracking.

Verifies that all trades are journaled immediately (even PENDING orders),
that PENDING trades are excluded from get_open_trades(), and that
reconciliation updates existing trades instead of creating duplicates.

See implementation plan: Add order_status to Trades — Journal All Trades Immediately
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import (
    OrderType,
    TradeAction,
    TradeDirection,
    TradeProposal,
)

# ---------------------------------------------------------------------------
# Fixtures — real SQLite DB for integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def journal(tmp_path: Path) -> TradeJournal:
    """Create a real in-memory journal with schema applied."""
    db = Database(tmp_path / "test.db")
    await db.initialize()

    # Apply migrations
    migrations_dir = Path(__file__).parent.parent.parent / "src" / "evotrader" / "db" / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        sql = sql_file.read_text()
        async with db.transaction() as conn:
            try:
                await conn.executescript(sql)
            except Exception:
                # Some migrations may fail idempotently (e.g., duplicate column)
                pass

    return TradeJournal(db)


def _make_proposal(
    ticker: str = "QQQ",
    direction: TradeDirection = TradeDirection.LONG,
    action: TradeAction = TradeAction.OPEN,
    quantity: float = 1.0,
    option_id: str | None = None,
    regime: str = "range_bound",
    algo_signal: float = 0.5,
) -> TradeProposal:
    return TradeProposal(
        ticker=ticker,
        direction=direction,
        action=action,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=5.0,
        hybrid_score=0.3,
        confidence=0.8,
        algo_signal=algo_signal,
        llm_signal=0.2,
        regime=regime,
        algo_version="v005_mr_internal_rebalance",
        reasoning="Test trade proposal",
        option_id=option_id,
        option_type="call" if option_id else None,
        strike=759.0 if option_id else None,
        expiration="2026-08-07" if option_id else None,
    )


# ---------------------------------------------------------------------------
# Journal Layer Tests
# ---------------------------------------------------------------------------


class TestOrderStatusJournal:
    """Verify order_status column behavior in TradeJournal."""

    @pytest.mark.asyncio
    async def test_pending_trade_recorded_immediately(self, journal: TradeJournal) -> None:
        """A PENDING trade should be inserted into the trades table immediately."""
        proposal = _make_proposal(option_id="opt-123")
        trade_ids = await journal.record_trade(
            proposal=proposal,
            session_id="session-1",
            order_status="PENDING",
            order_id="broker-order-abc",
        )

        assert len(trade_ids) == 1

        # Verify it's in the DB
        trades = await journal.get_recent_trades(limit=10)
        assert len(trades) == 1
        assert trades[0]["order_status"] == "PENDING"
        assert trades[0]["order_id"] == "broker-order-abc"
        assert trades[0]["regime"] == "range_bound"  # real decision context preserved!

    @pytest.mark.asyncio
    async def test_pending_excluded_from_open_trades(self, journal: TradeJournal) -> None:
        """PENDING trades must NOT appear in get_open_trades()."""
        proposal = _make_proposal(option_id="opt-456")
        await journal.record_trade(
            proposal=proposal,
            order_status="PENDING",
            order_id="broker-order-def",
        )

        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 0, "PENDING trades should be excluded from open trades"

    @pytest.mark.asyncio
    async def test_filled_trade_in_open_trades(self, journal: TradeJournal) -> None:
        """A FILLED trade should appear in get_open_trades()."""
        proposal = _make_proposal(option_id="opt-789")
        await journal.record_trade(
            proposal=proposal,
            order_status="FILLED",
            order_id="broker-order-ghi",
        )

        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 1

    @pytest.mark.asyncio
    async def test_update_pending_to_filled(self, journal: TradeJournal) -> None:
        """Updating a PENDING trade to FILLED should make it visible in open trades."""
        proposal = _make_proposal(option_id="opt-update")
        await journal.record_trade(
            proposal=proposal,
            order_status="PENDING",
            order_id="broker-order-upd",
        )

        # Should NOT be in open trades
        assert len(await journal.get_open_trades()) == 0

        # Update to FILLED
        rows = await journal.update_order_status(
            order_id="broker-order-upd",
            new_status="FILLED",
            fill_price=5.25,
        )
        assert rows == 1

        # Now should appear in open trades
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["order_status"] == "FILLED"
        assert open_trades[0]["fill_price"] == 5.25

    @pytest.mark.asyncio
    async def test_update_to_rejected_with_reason(self, journal: TradeJournal) -> None:
        """REJECTED trade should preserve broker reason and stay excluded from open trades."""
        proposal = _make_proposal()
        await journal.record_trade(
            proposal=proposal,
            order_status="PENDING",
            order_id="broker-order-rej",
        )

        rows = await journal.update_order_status(
            order_id="broker-order-rej",
            new_status="REJECTED",
            broker_status_reason="Insufficient buying power for this order",
        )
        assert rows == 1

        # Should NOT appear in open trades
        assert len(await journal.get_open_trades()) == 0

        # But should be queryable by status
        rejected = await journal.get_trades_by_order_status("REJECTED")
        assert len(rejected) == 1
        assert rejected[0]["broker_status_reason"] == "Insufficient buying power for this order"
        assert rejected[0]["regime"] == "range_bound"  # decision context preserved

    @pytest.mark.asyncio
    async def test_get_trades_by_order_status(self, journal: TradeJournal) -> None:
        """get_trades_by_order_status should filter by status."""
        for i, status in enumerate(["PENDING", "FILLED", "REJECTED", "PENDING"]):
            proposal = _make_proposal(option_id=f"opt-{i}")
            await journal.record_trade(
                proposal=proposal,
                order_status=status,
                order_id=f"order-{i}",
            )

        pending = await journal.get_trades_by_order_status("PENDING")
        assert len(pending) == 2

        filled = await journal.get_trades_by_order_status("FILLED")
        assert len(filled) == 1

        rejected = await journal.get_trades_by_order_status("REJECTED")
        assert len(rejected) == 1

    @pytest.mark.asyncio
    async def test_default_order_status_is_filled(self, journal: TradeJournal) -> None:
        """Trades recorded without explicit order_status should default to FILLED."""
        proposal = _make_proposal()
        await journal.record_trade(proposal=proposal)

        trades = await journal.get_recent_trades(limit=1)
        assert trades[0]["order_status"] == "FILLED"

        # Should appear in open trades
        open_trades = await journal.get_open_trades()
        assert len(open_trades) == 1

    @pytest.mark.asyncio
    async def test_update_nonexistent_order_returns_zero(self, journal: TradeJournal) -> None:
        """Updating a non-existent order_id should return 0 rows."""
        rows = await journal.update_order_status(
            order_id="nonexistent",
            new_status="FILLED",
        )
        assert rows == 0


# ---------------------------------------------------------------------------
# Tools Layer Tests — record_trade behavior
# ---------------------------------------------------------------------------


class TestRecordTradeOrderStatus:
    """Verify that record_trade in tools.py correctly handles order lifecycle."""

    @pytest.mark.asyncio
    async def test_pending_trade_not_deferred(self) -> None:
        """PENDING trades should be journaled (not deferred) and return order_status."""
        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.record_trade = AsyncMock(return_value=[42])
        mock_journal.save_pending_order = AsyncMock()
        mock_journal.delete_pending_order = AsyncMock()

        trade_data = {
            "ticker": "QQQ",
            "direction": "LONG",
            "action": "OPEN",
            "quantity": 1,
            "order_type": "limit",
            "limit_price": 5.0,
            "order_id": "broker-123",
            "status": "PENDING",
            "regime": "range_bound",
            "algo_version": "v005",
            "algo_signal": 0.5,
            "confidence": 0.8,
            "reasoning": "Test trade",
        }

        with (
            patch.object(tools_module, "_journal", mock_journal),
            patch.object(tools_module, "_memory", None),
        ):
            result = await tools_module.record_trade(json.dumps(trade_data))

        # Should NOT return "deferred" anymore
        assert result["status"] == "recorded"
        assert result["order_status"] == "PENDING"
        assert result["trade_ids"] == [42]

        # Should have called record_trade on journal
        mock_journal.record_trade.assert_called_once()
        call_kwargs = mock_journal.record_trade.call_args
        assert call_kwargs.kwargs["order_status"] == "PENDING"
        assert call_kwargs.kwargs["order_id"] == "broker-123"

        # Should also save to pending_orders audit trail
        mock_journal.save_pending_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_filled_trade_recorded_normally(self) -> None:
        """FILLED trades should be recorded with order_status='FILLED'."""
        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.record_trade = AsyncMock(return_value=[43])
        mock_journal.delete_pending_order = AsyncMock()

        trade_data = {
            "ticker": "QQQ",
            "direction": "LONG",
            "action": "OPEN",
            "quantity": 1,
            "order_type": "market",
            "price": 720.0,
            "order_id": "broker-456",
            "status": "FILLED",
            "fill_price": 720.05,
            "regime": "trend",
            "algo_version": "v005",
            "algo_signal": 0.8,
            "confidence": 0.9,
            "reasoning": "Strong uptrend",
        }

        with (
            patch.object(tools_module, "_journal", mock_journal),
            patch.object(tools_module, "_memory", None),
        ):
            result = await tools_module.record_trade(json.dumps(trade_data))

        assert result["status"] == "recorded"
        assert result["order_status"] == "FILLED"

        call_kwargs = mock_journal.record_trade.call_args
        assert call_kwargs.kwargs["order_status"] == "FILLED"

    @pytest.mark.asyncio
    async def test_rejected_trade_preserves_reason(self) -> None:
        """REJECTED trades should store the broker rejection reason."""
        from evotrader.agents import tools as tools_module

        mock_journal = AsyncMock()
        mock_journal.record_trade = AsyncMock(return_value=[44])
        mock_journal.delete_pending_order = AsyncMock()

        trade_data = {
            "ticker": "QQQ",
            "direction": "LONG",
            "action": "OPEN",
            "quantity": 1,
            "order_type": "limit",
            "limit_price": 5.0,
            "order_id": "broker-789",
            "status": "REJECTED",
            "reason": "Insufficient buying power",
            "regime": "range_bound",
            "algo_version": "v005",
            "algo_signal": 0.3,
            "confidence": 0.7,
            "reasoning": "Mean reversion setup",
        }

        with (
            patch.object(tools_module, "_journal", mock_journal),
            patch.object(tools_module, "_memory", None),
        ):
            result = await tools_module.record_trade(json.dumps(trade_data))

        assert result["order_status"] == "REJECTED"

        call_kwargs = mock_journal.record_trade.call_args
        assert call_kwargs.kwargs["order_status"] == "REJECTED"
        assert call_kwargs.kwargs["broker_status_reason"] == "Insufficient buying power"
