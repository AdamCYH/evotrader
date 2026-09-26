"""Regression tests: database writes take turns on the shared connection.

Found 2026-09-25 in a practice cycle. The agent framework runs an agent's tool
calls side by side (asyncio.gather). The executor recorded three trades at once;
two ``Database.transaction()`` calls sent BEGIN down the one shared SQLite
connection and the second failed with "cannot start a transaction within a
transaction", so a trade row was lost. The sibling whose transaction was open
was then cancelled mid-write, and ``transaction()`` only rolled back on
``Exception`` (a cancellation is a ``BaseException``), so the transaction stayed
open and every later write in the cycle failed the same way — including the one
that marks the cycle finished.

Simple writes that committed on their own had the same flaw from the other
side: their commit could land in the middle of another task's transaction and
commit half of it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from evotrader.db.connection import Database
from evotrader.db.journal import TradeJournal
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal


@pytest.fixture
async def scratch(db: Database) -> Database:
    async with db.transaction() as conn:
        await conn.execute("CREATE TABLE scratch (v INTEGER)")
    return db


async def _values(db: Database) -> list[int]:
    async with db.connection() as conn:
        cur = await conn.execute("SELECT v FROM scratch ORDER BY v")
        return [row["v"] for row in await cur.fetchall()]


async def test_two_writes_at_once_both_land(scratch: Database) -> None:
    async def write(v: int) -> None:
        async with scratch.transaction() as conn:
            await conn.execute("INSERT INTO scratch VALUES (?)", (v,))
            await asyncio.sleep(0.01)  # a real write awaits between statements
            await conn.execute("INSERT INTO scratch VALUES (?)", (v * 10,))

    await asyncio.gather(write(1), write(2))
    assert await _values(scratch) == [1, 2, 10, 20]


async def test_a_cancelled_write_is_undone_and_the_next_one_works(scratch: Database) -> None:
    started = asyncio.Event()

    async def interrupted() -> None:
        async with scratch.transaction() as conn:
            await conn.execute("INSERT INTO scratch VALUES (1)")
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(interrupted())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with scratch.transaction() as conn:
        await conn.execute("INSERT INTO scratch VALUES (2)")
    assert await _values(scratch) == [2], "the cancelled write left nothing behind"


async def test_a_write_inside_a_write_fails_at_once_instead_of_waiting(scratch: Database) -> None:
    """Taking turns must not turn a nesting mistake into a silent hang."""
    async with scratch.transaction():
        with pytest.raises(RuntimeError, match="already"):
            async with asyncio.timeout(1), scratch.transaction():
                pass


async def test_a_simple_write_does_not_commit_half_of_another(scratch: Database) -> None:
    journal = TradeJournal(scratch)
    inside = asyncio.Event()

    async def half_then_fail() -> None:
        async with scratch.transaction() as conn:
            await conn.execute("INSERT INTO scratch VALUES (1)")
            inside.set()
            await asyncio.sleep(0.05)
            raise ValueError("second half failed")

    task = asyncio.create_task(half_then_fail())
    await inside.wait()
    await journal.save_pending_order("ord00001", "{}")  # lands while the other is open
    with pytest.raises(ValueError):
        await task
    assert await _values(scratch) == [], "the failed transaction was rolled back whole"
    assert [p["order_id"] for p in await journal.get_pending_orders()] == ["ord00001"]


async def test_three_trades_recorded_at_once_all_land(db: Database) -> None:
    """The practice cycle's shape: the executor records entry and stops together."""
    journal = TradeJournal(db)

    def proposal(action: TradeAction, qty: float) -> TradeProposal:
        return TradeProposal(
            ticker="SPY",
            direction=TradeDirection.LONG,
            action=action,
            quantity=qty,
            order_type=OrderType.MARKET,
            hybrid_score=0.5,
            confidence=0.6,
            algo_signal=0.5,
            llm_signal=0.5,
            regime="range_bound",
            algo_version="v001_initial",
            reasoning="test",
            timestamp=datetime.now(UTC),
        )

    results = await asyncio.gather(
        journal.record_trade(proposal(TradeAction.OPEN, 16)),
        journal.record_trade(proposal(TradeAction.OPEN, 4)),
        journal.record_trade(proposal(TradeAction.OPEN, 2)),
    )
    assert all(ids and ids[0] > 0 for ids in results)
    assert len(await journal.get_recent_trades(limit=10)) == 3
