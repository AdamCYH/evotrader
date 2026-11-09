"""A fill the executor records itself keeps the broker's fill time.

Found by the evolution agent's code review. The reconciliation stamps
``trades.filled_at`` on the orders it resolves, but a fill the executor records
as FILLED is never pending, so it had none, and an exit's holding period ran
from when its row was written. The executor's payload carries no broker time:
``await_order_state`` now returns ``filled_at``, ``record_trade`` takes it, and
without it the broker is asked for the order by id.

Made-up ticker, prices, times and order ids.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from evotrader.agents import tools
from evotrader.db.journal import TradeJournal

ORDER = "6f000000-0000-4000-8000-000000000001"
ENTRY_FILL = "2026-03-02T15:00:00+00:00"
EXIT_FILL = "2026-03-04T15:30:00+00:00"


async def _row(db, **values) -> int:
    row = {
        "timestamp": "2026-03-04T19:00:00+00:00",
        "ticker": "XYZ",
        "direction": "LONG",
        "quantity": 4,
        "price": 50.0,
        "order_type": "limit",
        "algo_version": "test",
        "regime": "range_bound",
        "confidence": 0.5,
        "reasoning": "test",
        "market_snapshot": "{}",
        "order_status": "FILLED",
        **values,
    }
    async with db.transaction() as conn:
        cursor = await conn.execute(
            f"INSERT INTO trades ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        return cursor.lastrowid


async def _get(db, trade_id: int) -> dict:
    async with db.connection() as conn:
        return dict(
            await (await conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))).fetchone()
        )


class TestTheJournal:
    async def test_an_exit_is_stamped_and_its_holding_period_follows(self, db) -> None:
        journal = TradeJournal(db)
        entry = await _row(db, action="OPEN", filled_at=ENTRY_FILL)
        exit_id = await _row(
            db, action="CLOSE", order_id=ORDER, related_trade_id=entry, holding_period_s=12
        )
        assert await journal.stamp_fill_time(ORDER, EXIT_FILL) == 1
        row = await _get(db, exit_id)
        assert row["filled_at"] == EXIT_FILL
        assert row["holding_period_s"] == 2 * 86400 + 1800

    async def test_a_time_already_there_stays(self, db) -> None:
        journal = TradeJournal(db)
        trade_id = await _row(db, action="OPEN", order_id=ORDER, filled_at=ENTRY_FILL)
        assert await journal.stamp_fill_time(ORDER, EXIT_FILL) == 0
        assert (await _get(db, trade_id))["filled_at"] == ENTRY_FILL

    async def test_only_filled_rows(self, db) -> None:
        journal = TradeJournal(db)
        await _row(db, action="OPEN", order_id=ORDER, order_status="PENDING")
        assert await journal.stamp_fill_time(ORDER, EXIT_FILL) == 0


@pytest.fixture
def journal(db, monkeypatch):
    monkeypatch.setattr(tools, "_journal", TradeJournal(db))
    return db


class TestTheTimeIsFound:
    async def test_from_the_payload(self, journal) -> None:
        trade_id = await _row(journal, action="OPEN", order_id=ORDER)
        assert (
            await tools._stamp_fill_time(ORDER, {"filled_at": "2026-03-04T15:30:00Z"}) == EXIT_FILL
        )
        assert (await _get(journal, trade_id))["filled_at"] == EXIT_FILL

    async def test_from_the_broker(self, journal, monkeypatch) -> None:
        trade_id = await _row(journal, action="OPEN", order_id=ORDER)
        monkeypatch.setattr(tools, "_agentic_account_number", AsyncMock(return_value="ACCT"))
        lookup = AsyncMock(
            return_value={
                "id": ORDER,
                "state": "filled",
                "executions": [{"timestamp": "2026-03-04T15:29:58Z"}, {"timestamp": EXIT_FILL}],
            }
        )
        monkeypatch.setattr(tools, "_lookup_broker_order", lookup)
        assert await tools._stamp_fill_time(ORDER, {}) == EXIT_FILL
        lookup.assert_awaited_once_with("ACCT", ORDER)
        assert (await _get(journal, trade_id))["filled_at"] == EXIT_FILL

    async def test_an_order_not_filled_at_the_broker_is_not_stamped(
        self, journal, monkeypatch
    ) -> None:
        trade_id = await _row(journal, action="OPEN", order_id=ORDER)
        monkeypatch.setattr(tools, "_agentic_account_number", AsyncMock(return_value="ACCT"))
        monkeypatch.setattr(
            tools, "_lookup_broker_order", AsyncMock(return_value={"id": ORDER, "state": "queued"})
        )
        assert await tools._stamp_fill_time(ORDER, {}) is None
        assert (await _get(journal, trade_id))["filled_at"] is None

    async def test_not_a_broker_order(self, journal) -> None:
        assert await tools._stamp_fill_time("PENDING", {"filled_at": EXIT_FILL}) is None


class TestAwaitOrderState:
    async def test_it_returns_the_fill_time(self, monkeypatch) -> None:
        order = {
            "id": ORDER,
            "state": "filled",
            "average_price": "50.10",
            "cumulative_quantity": "4",
            "quantity": "4",
            "last_transaction_at": EXIT_FILL,
        }
        monkeypatch.setattr(
            tools, "_call_mcp_tool", AsyncMock(return_value={"data": {"orders": [order]}})
        )
        result = await tools.await_order_state(ORDER, account_number="ACCT")
        assert result["filled"] is True and result["filled_at"] == EXIT_FILL

    async def test_not_filled_no_time(self, monkeypatch) -> None:
        order = {"id": ORDER, "state": "cancelled", "last_transaction_at": EXIT_FILL}
        monkeypatch.setattr(
            tools, "_call_mcp_tool", AsyncMock(return_value={"data": {"orders": [order]}})
        )
        result = await tools.await_order_state(ORDER, account_number="ACCT")
        assert result["filled_at"] is None


def test_record_trade_says_it_takes_the_time() -> None:
    assert "filled_at" in (tools.record_trade.__doc__ or "")


@pytest.fixture
def executor(db, tmp_path, monkeypatch):
    """The tools on a real journal; the broker answers a lookup by id from ``orders``."""
    import shutil

    from evotrader import paths
    from evotrader.config import AppConfig
    from evotrader.models.config import TradingMode
    from evotrader.tools import asset_context

    shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
    config = AppConfig(data_dir=tmp_path / "data")
    config.settings.mode = TradingMode.LIVE
    orders: dict[str, dict] = {}

    async def call_mcp(tool: str, args: dict):
        if tool == "get_equity_orders" and args.get("order_id") in orders:
            return {"data": {"orders": [orders[args["order_id"]]]}}
        return None

    for name, value in {
        "_db": db,
        "_journal": TradeJournal(db),
        "_config": config,
        "_metrics": None,
        "_memory": None,
        "_sim_proxy": None,
        "_current_session_id": "s-1",
        "_open_positions_cache": None,
        "_call_mcp_tool": call_mcp,
        "_agentic_account_number": AsyncMock(return_value="ACCT1"),
    }.items():
        monkeypatch.setattr(tools, name, value)
    monkeypatch.setattr(asset_context, "allowed_tickers", lambda: ("XYZ",))
    return orders


async def _record(**fields) -> list[int]:
    import json

    out = await tools.record_trade(
        json.dumps(
            {
                "ticker": "XYZ",
                "direction": "LONG",
                "algo_signal": 0.1,
                "hybrid_score": 0.1,
                "regime": "range_bound",
                "algo_version": "v001",
                "reasoning": "made up",
                **fields,
            }
        )
    )
    return out["trade_ids"]


class TestThroughRecordTrade:
    async def test_an_entry_and_its_exit_keep_the_brokers_times(self, executor, db) -> None:
        entry_id = "6f000000-0000-4000-8000-0000000000e1"
        exit_id = "6f000000-0000-4000-8000-0000000000e2"
        (entry,) = await _record(
            action="OPEN",
            quantity=4,
            order_type="limit",
            limit_price=50.0,
            order_id=entry_id,
            status="FILLED",
            fill_price=50.0,
            filled_quantity=4,
            filled_at=ENTRY_FILL,
        )
        executor[exit_id] = {"id": exit_id, "state": "filled", "last_transaction_at": EXIT_FILL}
        (close,) = await _record(
            action="CLOSE",
            quantity=4,
            order_type="limit",
            limit_price=52.0,
            order_id=exit_id,
            status="FILLED",
            fill_price=52.0,
            filled_quantity=4,
            related_trade_id=entry,
        )
        assert (await _get(db, entry))["filled_at"] == ENTRY_FILL
        row = await _get(db, close)
        assert row["filled_at"] == EXIT_FILL
        assert row["holding_period_s"] == 2 * 86400 + 1800
