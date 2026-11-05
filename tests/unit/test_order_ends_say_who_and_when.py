"""A cancelled order says who cancelled it; a filled one says when it filled.

Found by the evolution agent's code review. Two gaps in what the journal kept
about an order that ended:

* Every cancelled gtc order read "cancelled at the broker", whether the
  executor had cancelled it to resize or nothing in the app had touched it, so
  neither agent could tell a decision from a broken order. An agent's cancel
  is a logged tool call naming the order; the reason now says which, and the
  coverage carries the newest one as ``last_protective_cancel``.
* A stop or take-profit is recorded when it is placed, and its holding period
  was worked out from that: one placed with its entry and filled days later
  read as held for seconds. The fill's own time is now kept (``filled_at``)
  and the holding period runs to it.

Made-up ticker, ids, prices and times; the broker is a stand-in.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evotrader.agents import tools
from evotrader.config import AppConfig
from evotrader.db.journal import TradeJournal, holding_seconds
from evotrader.db.thought_log import ThoughtLogger
from evotrader.models.config import TradingMode


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat().replace("+00:00", "Z")


def _broker_order(order_id: str, state: str, **extra) -> dict:
    return {
        "id": order_id,
        "symbol": "XYZ",
        "side": "sell",
        "type": "market",
        "trigger": "stop",
        "state": state,
        "quantity": "2.000000",
        "stop_price": "90.000000",
        "price": None,
        "time_in_force": "gtc",
        "created_at": _ago(hours=2),
        "last_transaction_at": _ago(minutes=30),
        **extra,
    }


@pytest.fixture
def broker(db, tmp_path, monkeypatch):
    """The tools on a real journal; the broker lists ``state.orders`` for the symbol."""
    from evotrader import paths
    from evotrader.tools import asset_context

    shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
    config = AppConfig(data_dir=tmp_path / "data")
    config.settings.mode = TradingMode.LIVE
    state = SimpleNamespace(orders=[])

    async def call_mcp(tool: str, args: dict):
        if tool == "get_accounts":
            return {"data": {"accounts": []}}
        if tool != "get_equity_orders":
            return None
        if "order_id" in args:
            found = [o for o in state.orders if o["id"] == args["order_id"]]
            return {"data": {"orders": found}} if found else None
        return {"data": {"orders": state.orders}}

    async def account():
        return "ACCT1"

    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_journal", TradeJournal(db))
    monkeypatch.setattr(tools, "_config", config)
    monkeypatch.setattr(tools, "_metrics", None)
    monkeypatch.setattr(tools, "_memory", None)
    monkeypatch.setattr(tools, "_sim_proxy", None)
    monkeypatch.setattr(tools, "_current_session_id", "s-1")
    monkeypatch.setattr(tools, "_open_positions_cache", None)
    monkeypatch.setattr(tools, "_unconfirmed_orders", {})
    monkeypatch.setattr(tools, "_call_mcp_tool", call_mcp)
    monkeypatch.setattr(tools, "_agentic_account_number", account)
    monkeypatch.setattr(asset_context, "allowed_tickers", lambda: ("XYZ",))
    return state


async def _record(**fields) -> list[int]:
    out = await tools.record_trade(
        json.dumps(
            {
                "ticker": "XYZ",
                "direction": "LONG",
                "algo_signal": 0.1,
                "hybrid_score": 0.1,
                "regime": "trending_bull",
                "algo_version": "v001",
                "reasoning": "made up",
                **fields,
            }
        )
    )
    return out["trade_ids"]


async def _hold_with_a_stop(stop_id: str = "st0p-cccc-0001") -> int:
    (entry,) = await _record(
        action="OPEN", quantity=5, order_type="market", price=100.0, status="FILLED"
    )
    await _record(
        action="STOP_LOSS",
        quantity=2,
        order_type="stop_market",
        stop_price=90.0,
        time_in_force="gtc",
        order_id=stop_id,
        status="PENDING",
        related_trade_id=entry,
    )
    return entry


async def _row(order_id: str) -> dict:
    async with tools._db.connection() as conn:
        cursor = await conn.execute("SELECT * FROM trades WHERE order_id = ?", (order_id,))
        return dict(await cursor.fetchone())


class TestWhoCancelled:
    async def test_an_agents_cancel_names_the_agent_and_its_session(self, broker, db) -> None:
        await _hold_with_a_stop()
        await ThoughtLogger(db).record_event(
            "5e55i0n1-aaaa",
            "execution",
            "tool_call",
            "cancel_equity_order",
            {"args": {"order_id": "st0p-cccc-0001", "account_number": "ACCT1"}},
        )
        broker.orders = [_broker_order("st0p-cccc-0001", "cancelled")]
        await tools.reconcile_pending_orders()
        row = await _row("st0p-cccc-0001")
        assert row["order_status"] == "CANCELLED"
        assert row["broker_status_reason"].startswith(
            "cancelled by the execution agent in session 5e55i0n1 at "
        )
        assert "ET" in row["broker_status_reason"]

    async def test_no_agent_cancel_means_outside_the_app(self, broker) -> None:
        await _hold_with_a_stop()
        broker.orders = [_broker_order("st0p-cccc-0001", "cancelled")]
        await tools.reconcile_pending_orders()
        reason = (await _row("st0p-cccc-0001"))["broker_status_reason"]
        assert reason.startswith("cancelled outside EvoTrader at ")
        assert "no agent cancelled it (the broker did, or someone in the broker's app)" in reason

    async def test_a_cancel_of_another_order_does_not_count(self, broker, db) -> None:
        await _hold_with_a_stop()
        await ThoughtLogger(db).record_event(
            "5e55i0n1-aaaa",
            "execution",
            "tool_call",
            "cancel_equity_order",
            {"args": {"order_id": "0ther-0rder-9999"}},
        )
        broker.orders = [_broker_order("st0p-cccc-0001", "cancelled")]
        await tools.reconcile_pending_orders()
        assert (await _row("st0p-cccc-0001"))["broker_status_reason"].startswith(
            "cancelled outside EvoTrader"
        )

    async def test_the_brokers_own_reason_follows(self, broker) -> None:
        await _hold_with_a_stop()
        broker.orders = [
            _broker_order("st0p-cccc-0001", "cancelled", cancel_reason="made-up broker reason")
        ]
        await tools.reconcile_pending_orders()
        reason = (await _row("st0p-cccc-0001"))["broker_status_reason"]
        assert reason.startswith("cancelled outside EvoTrader")
        assert reason.endswith("; the broker says: made-up broker reason")

    async def test_a_day_order_that_expired_is_still_expired(self, broker) -> None:
        await _hold_with_a_stop()
        broker.orders = [
            _broker_order(
                "st0p-cccc-0001",
                "cancelled",
                time_in_force="gfd",
                created_at="2026-03-02T15:00:00Z",  # a Monday, 10:00 ET
                last_transaction_at="2026-03-02T21:30:00Z",  # 16:30 ET, after the close
            )
        ]
        await tools.reconcile_pending_orders()
        row = await _row("st0p-cccc-0001")
        assert row["order_status"] == "EXPIRED"
        assert row["broker_status_reason"].startswith("Day order expired")


class TestTheCoverageSaysWhoCancelled:
    async def test_the_newest_cancel_is_on_the_tickers_block(self, broker) -> None:
        await _hold_with_a_stop()
        broker.orders = [_broker_order("st0p-cccc-0001", "cancelled")]
        await tools.reconcile_pending_orders()
        block = (await tools._protective_coverage())["XYZ"]
        cancel = block["last_protective_cancel"]
        assert (cancel["order_id"], cancel["action"], cancel["quantity"]) == (
            "st0p-cccc-0001",
            "STOP_LOSS",
            2.0,
        )
        assert (cancel["level"], cancel["by"]) == (90.0, "outside_evotrader")
        assert block["no_order_qty"] == 5.0, "the stop is gone: all five shares are bare"
        positions = await tools.get_open_positions()
        assert positions["protective_coverage"]["XYZ"]["last_protective_cancel"]["by"] == (
            "outside_evotrader"
        )

    async def test_none_when_nothing_was_cancelled(self, broker) -> None:
        await _hold_with_a_stop()
        broker.orders = [_broker_order("st0p-cccc-0001", "confirmed")]
        await tools.reconcile_pending_orders()
        assert "last_protective_cancel" not in (await tools._protective_coverage())["XYZ"]

    @pytest.mark.parametrize(
        ("reason", "by"),
        [
            (
                "cancelled by the execution agent in session 5e55i0n1 at 2026-03-05 11:37 ET",
                "agent",
            ),
            (
                "cancelled outside EvoTrader at 2026-03-05 15:46 ET: no agent cancelled it",
                ("outside_evotrader"),
            ),
            ("gtc order cancelled at the broker (a gtc order does not expire)", "not_recorded"),
        ],
    )
    def test_by(self, reason, by) -> None:
        assert tools._cancelled_by(reason) == by


class TestWhenItFilled:
    def test_the_last_execution(self) -> None:
        order = {
            "executions": [
                {"timestamp": "2026-03-05T15:00:01Z"},
                {"timestamp": "2026-03-05T15:00:03.5Z"},
            ],
            "last_transaction_at": "2026-03-05T15:00:04Z",
        }
        assert tools.broker_fill_time(order) == "2026-03-05T15:00:03.500000+00:00"

    def test_else_the_last_transaction(self) -> None:
        assert tools.broker_fill_time({"last_transaction_at": "2026-03-05T15:00:04Z"}) == (
            "2026-03-05T15:00:04+00:00"
        )
        assert tools.broker_fill_time({}) is None

    def test_holding_runs_from_the_entrys_fill_to_the_exits(self) -> None:
        entry = {"timestamp": "2026-03-02T15:00:00+00:00"}
        exit_row = {"timestamp": "2026-03-02T15:00:12+00:00", "filled_at": "2026-03-05T15:09:00Z"}
        assert holding_seconds(entry, exit_row) == 3 * 86400 + 540
        assert holding_seconds(entry, {"timestamp": exit_row["timestamp"]}) is None
        later_fill = {**entry, "filled_at": "2026-03-02T16:00:00Z"}
        assert holding_seconds(later_fill, exit_row) == 3 * 86400 - 3600 + 540

    async def test_a_take_profit_that_filled_days_later_is_held_days(self, broker) -> None:
        (entry,) = await _record(
            action="OPEN", quantity=4, order_type="market", price=100.0, status="FILLED"
        )
        await _record(
            action="TAKE_PROFIT",
            quantity=2,
            order_type="limit",
            limit_price=112.0,
            time_in_force="gtc",
            order_id="t4ke-cccc-0002",
            status="PENDING",
            related_trade_id=entry,
        )
        fill = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)
        broker.orders = [
            {
                **_broker_order("t4ke-cccc-0002", "filled"),
                "type": "limit",
                "trigger": "immediate",
                "price": "112.000000",
                "stop_price": None,
                "average_price": "112.000000",
                "cumulative_quantity": "2.000000",
                "executions": [{"timestamp": fill.isoformat().replace("+00:00", "Z")}],
            }
        ]
        async with tools._db.transaction() as conn:  # the entry, three days before
            await conn.execute(
                "UPDATE trades SET timestamp = ? WHERE id = ?",
                ((fill - timedelta(days=3)).isoformat(), entry),
            )
        await tools.reconcile_pending_orders()
        row = await _row("t4ke-cccc-0002")
        assert row["order_status"] == "FILLED"
        assert row["filled_at"] == fill.isoformat()
        assert row["holding_period_s"] == 3 * 86400
        assert row["realized_pnl"] == pytest.approx(24.0)
