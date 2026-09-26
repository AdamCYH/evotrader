"""Regression tests: the simulated broker's writes take turns on its connection.

The practice broker keeps its own SQLite file and its own shared connection, and
its ``_transaction()`` had the flaw fixed in the main database on 2026-09-25
(see test_db_writes_take_turns.py): no turn-taking, so two tool calls running
side by side sent BEGIN down the one connection and the second failed with
"cannot start a transaction within a transaction"; and a rollback only on
``Exception``, so a cancelled write (a ``BaseException``) left its transaction
open and every later write failed.

A write that committed on its own had the same flaw from the other side: its
commit could land in the middle of another task's transaction and commit half
of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from evotrader.sim import SimBroker


@pytest.fixture
async def broker(tmp_path: Path) -> AsyncIterator[SimBroker]:
    b = SimBroker(tmp_path / "sim_broker.db")
    b.slippage_model = "none"
    b._get_live_bid_ask = AsyncMock(return_value=(150.0, 150.0))  # type: ignore[method-assign]
    b._get_live_price = AsyncMock(return_value=150.0)  # type: ignore[method-assign]
    await b.initialize()
    async with b._transaction() as conn:
        await conn.execute("CREATE TABLE scratch (v INTEGER)")
    yield b
    await b.close()


async def _values(broker: SimBroker) -> list[int]:
    conn = await broker._get_conn()
    async with conn.execute("SELECT v FROM scratch ORDER BY v") as cur:
        return [row["v"] for row in await cur.fetchall()]


async def test_two_writes_at_once_both_land(broker: SimBroker) -> None:
    async def write(v: int) -> None:
        async with broker._transaction() as conn:
            await conn.execute("INSERT INTO scratch VALUES (?)", (v,))
            await asyncio.sleep(0.01)  # a real write awaits between statements
            await conn.execute("INSERT INTO scratch VALUES (?)", (v * 10,))

    await asyncio.gather(write(1), write(2))
    assert await _values(broker) == [1, 2, 10, 20]


async def test_a_cancelled_write_is_undone_and_the_next_one_works(broker: SimBroker) -> None:
    started = asyncio.Event()

    async def interrupted() -> None:
        async with broker._transaction() as conn:
            await conn.execute("INSERT INTO scratch VALUES (1)")
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(interrupted())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with broker._transaction() as conn:
        await conn.execute("INSERT INTO scratch VALUES (2)")
    assert await _values(broker) == [2], "the cancelled write left nothing behind"


async def test_a_write_inside_a_write_fails_at_once_instead_of_waiting(broker: SimBroker) -> None:
    """Taking turns must not turn a nesting mistake into a silent hang."""
    async with broker._transaction():
        with pytest.raises(RuntimeError, match="already"):
            async with asyncio.timeout(1), broker._transaction():
                pass


async def test_the_stale_order_sweep_does_not_commit_half_of_another_write(
    broker: SimBroker,
) -> None:
    """The sweep used to commit on its own, mid-way through someone else's write."""
    async with broker._transaction() as conn:
        await conn.execute(
            """
            INSERT INTO sim_orders (id, account_number, asset_type, ticker, side, order_type,
                                    quantity, limit_price, status, timestamp)
            VALUES ('old00001', ?, 'EQUITY', 'MSTR', 'buy', 'limit', 1, 140.0, 'pending',
                    '2026-09-22 14:00:00')
            """,
            (broker.account_number,),
        )

    inside = asyncio.Event()

    async def half_then_fail() -> None:
        async with broker._transaction() as conn:
            await conn.execute("INSERT INTO scratch VALUES (1)")
            inside.set()
            await asyncio.sleep(0.05)
            raise ValueError("second half failed")

    task = asyncio.create_task(half_then_fail())
    await inside.wait()
    swept = await broker.cancel_stale_pending_orders()  # lands while the other is open
    with pytest.raises(ValueError):
        await task

    assert await _values(broker) == [], "the failed write was rolled back whole"
    assert [o["id"] for o in swept] == ["old00001"]
    status = (await broker.get_order_status("old00001"))["data"]
    assert status["state"] == "cancelled"


async def test_orders_placed_at_once_all_land(broker: SimBroker) -> None:
    """The executor's shape: several orders sent in one turn, run side by side."""
    await broker.deposit(10_000.0)
    results = await asyncio.gather(
        *(
            broker.place_equity_order(
                {
                    "symbol": "MSTR",
                    "side": "buy",
                    "type": "limit",
                    "limit_price": 140.0 - i,
                    "time_in_force": "gtc",
                    "quantity": 1,
                }
            )
            for i in range(3)
        )
    )
    assert [r["data"]["status"] for r in results] == ["pending"] * 3
    orders = (await broker.get_orders({"symbol": "MSTR"}))["data"]["results"]
    assert len(orders) == 3
