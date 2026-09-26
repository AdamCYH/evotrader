"""Regression tests: verify DB reads return dicts (not sqlite3.Row).

The row-factory-after-execute bug caused downstream `.get()` calls to fail
on sqlite3.Row objects, which was the ROOT CAUSE of the 30-day zero-trade
streak. These tests guard against reintroduction.

See: data/evolution/reviews/20260625_210123_db_row_factory_ordering_bug_root_cause_zero_trades.md
"""

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
def sample_open_proposal() -> TradeProposal:
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
        reasoning="Test trade for row factory regression",
        timestamp=datetime.now(UTC),
    )


class TestRowFactoryRegression:
    """Guard against the row-factory-after-execute anti-pattern."""

    async def test_get_open_trades_returns_dicts(
        self, journal: TradeJournal, sample_open_proposal: TradeProposal
    ) -> None:
        await journal.record_trade(sample_open_proposal)
        rows = await journal.get_open_trades()
        assert len(rows) >= 1
        assert all(isinstance(r, dict) for r in rows)
        # dict-style .get() access must not raise (this was the production crash)
        assert rows[0].get("ticker") is not None
        assert rows[0].get("ticker") == "SPY"

    async def test_get_recent_trades_returns_dicts(
        self, journal: TradeJournal, sample_open_proposal: TradeProposal
    ) -> None:
        await journal.record_trade(sample_open_proposal)
        rows = await journal.get_recent_trades(limit=5)
        assert len(rows) >= 1
        for r in rows:
            assert isinstance(r, dict), f"Expected dict, got {type(r)}"
            assert hasattr(r, "get")  # would fail for sqlite3.Row

    async def test_get_today_trades_returns_dicts(
        self, journal: TradeJournal, sample_open_proposal: TradeProposal
    ) -> None:
        await journal.record_trade(sample_open_proposal)
        rows = await journal.get_today_trades()
        assert len(rows) >= 1
        assert all(isinstance(r, dict) for r in rows)

    async def test_dict_access_round_trip(
        self, journal: TradeJournal, sample_open_proposal: TradeProposal
    ) -> None:
        """Verify the full chain: record → retrieve → dict access works end-to-end."""
        await journal.record_trade(sample_open_proposal)
        rows = await journal.get_open_trades()

        trade = rows[0]
        # These are the exact access patterns used in reconciliation.py
        assert trade.get("ticker") == "SPY"
        assert trade.get("option_id") is None  # equity trade
        assert float(trade.get("quantity", 0.0)) == 10.0
        assert trade.get("direction") == "LONG"
