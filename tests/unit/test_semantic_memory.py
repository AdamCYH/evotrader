"""Tests for SemanticMemory trade experience persistence, outcome updating, and journal sync."""

from datetime import datetime
from pathlib import Path

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import TradeAction, TradeDirection, TradeProposal
from evotrader.tools.memory import SemanticMemory


@pytest.fixture
def memory(tmp_path: Path) -> SemanticMemory:
    mem_dir = tmp_path / "test_memory"
    return SemanticMemory(mem_dir)


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def journal(db: Database) -> TradeJournal:
    return TradeJournal(db)


def test_store_and_update_trade_experience(memory: SemanticMemory):
    """Test storing an initial open trade experience and then updating with exit outcome."""
    memory.store_trade_experience(
        trade_id=101,
        direction="LONG",
        regime="range_bound",
        algo_signal=0.15,
        llm_signal=0.20,
        hybrid_score=0.17,
        reasoning="Test entry thesis at support",
        action="OPEN",
    )

    trades = memory.get_all_trade_experiences()
    assert len(trades) == 1
    t = trades[0]
    assert t["id"] == "trade_101"
    assert t["metadata"]["outcome_pnl"] == 0.0
    assert t["metadata"]["has_outcome"] is False
    assert "Outcome:" not in t["text"]

    # Now update with positive outcome and holding period
    updated = memory.update_trade_outcome(
        trade_id=101,
        outcome_pnl=42.50,
        holding_period_s=3600,
    )
    assert updated is True

    trades_after = memory.get_all_trade_experiences()
    assert len(trades_after) == 1
    t_after = trades_after[0]
    assert t_after["metadata"]["outcome_pnl"] == pytest.approx(42.50)
    assert t_after["metadata"]["has_outcome"] is True
    assert t_after["metadata"]["holding_period_s"] == 3600
    assert "Outcome: $+42.50 (profit)" in t_after["text"]
    assert "Held for 3600s" in t_after["text"]


def test_query_similar_trades(memory: SemanticMemory):
    """Test semantic query retrieval of stored trade experiences."""
    memory.store_trade_experience(
        trade_id=201,
        direction="SHORT",
        regime="trending_bear",
        algo_signal=-0.35,
        llm_signal=-0.40,
        hybrid_score=-0.38,
        reasoning="Aggressive breakdown below moving average",
        outcome_pnl=-15.00,
        holding_period_s=1800,
        action="CLOSE",
    )

    results = memory.query_similar_trades("breakdown below moving average", n_results=1)
    assert len(results) >= 1
    top = results[0]
    assert top["metadata"]["trade_id"] == 201
    assert top["metadata"]["direction"] == "SHORT"
    assert top["metadata"]["outcome_pnl"] == pytest.approx(-15.00)


async def test_sync_experiences_from_journal(
    memory: SemanticMemory, db: Database, journal: TradeJournal
):
    """Test syncing open and closed trades from SQLite journal into ChromaDB."""
    open_proposal = TradeProposal(
        ticker="QQQ",
        action=TradeAction.OPEN,
        direction=TradeDirection.LONG,
        quantity=5.0,
        limit_price=500.0,
        regime="trending_bull",
        algo_version="v001",
        reasoning="Open bull momentum",
        timestamp=datetime(2026, 8, 1, 10, 0, 0),
    )
    open_ids = await journal.record_trade(
        open_proposal, order_status="FILLED", order_id="open-sync-1"
    )
    open_id = open_ids[0]

    close_proposal = TradeProposal(
        ticker="QQQ",
        action=TradeAction.CLOSE,
        direction=TradeDirection.LONG,
        quantity=5.0,
        limit_price=510.0,
        regime="trending_bull",
        algo_version="v001",
        reasoning="Close bull momentum at target",
        timestamp=datetime(2026, 8, 1, 11, 0, 0),
    )
    close_ids = await journal.record_trade(
        close_proposal, order_status="FILLED", order_id="close-sync-1"
    )
    close_id = close_ids[0]

    # Sync into ChromaDB
    synced = await memory.sync_experiences_from_journal(journal)
    assert synced == 2

    experiences = memory.get_all_trade_experiences()
    assert len(experiences) == 2

    # Map by trade_id
    exp_by_id = {e["metadata"]["trade_id"]: e for e in experiences}

    # Close trade should have PnL = (510 - 500) * 5 = +$50.00
    close_exp = exp_by_id[close_id]
    assert close_exp["metadata"]["outcome_pnl"] == pytest.approx(50.0)
    assert close_exp["metadata"]["has_outcome"] is True

    # Open trade should have inherited the outcome PnL from the matched close
    open_exp = exp_by_id[open_id]
    assert open_exp["metadata"]["outcome_pnl"] == pytest.approx(50.0)
    assert open_exp["metadata"]["has_outcome"] is True
