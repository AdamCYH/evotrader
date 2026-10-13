"""A failed read of the broker's orders is not their absence.

Found by the evolution agent's code review. Reconciliation marked a journal
order CANCELLED ("not found on broker") whenever no lookup returned it, and a
lookup that failed returned nothing, the same as one that found nothing. One
pre-market morning the working protective orders were written off that way;
the coverage read ``none``, and the agents cancelled the real orders to repair
a gap that did not exist.

Now an order is gone only when a broker list that answered, holds orders and
reaches back to the order's placement does not have it. A list that failed or
answered empty proves nothing: the order stays PENDING, counts as resting, and
the coverage reads ``unverified``. A sell the broker refuses for want of shares
comes back with the orders holding them, and the executor is handed its
account number instead of recalling one.

Made-up ticker, ids, prices and times; the broker is a stand-in.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from evotrader.agents import tools
from evotrader.callbacks.reserved_shares import (
    annotate_refusal,
    is_reserved_shares_refusal,
    reserving_orders,
)
from evotrader.config import AppConfig
from evotrader.db.journal import TradeJournal
from evotrader.db.protection_audit import coverage_by_ticker
from evotrader.models.config import TradingMode

#: The book: 9 shares held, two stops (4 + 2) and two take-profits (2 + 1).
BOOK = (
    ("st0p-aaaa-0001", "STOP_LOSS", 4, {"stop_price": 90.0}),
    ("st0p-aaaa-0002", "STOP_LOSS", 2, {"stop_price": 90.0}),
    ("t4ke-aaaa-0003", "TAKE_PROFIT", 2, {"limit_price": 112.0}),
    ("t4ke-aaaa-0004", "TAKE_PROFIT", 1, {"limit_price": 115.0}),
)
IDS = [order_id for order_id, *_ in BOOK]


def _ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _broker_order(order_id: str, action: str, qty: float, levels: dict, **extra) -> dict:
    stop = action == "STOP_LOSS"
    return {
        "id": order_id,
        "symbol": "XYZ",
        "side": "sell",
        "type": "market" if stop else "limit",
        "trigger": "stop" if stop else "immediate",
        "state": "confirmed",
        "quantity": f"{qty:.6f}",
        "price": None if stop else f"{levels['limit_price']:.6f}",
        "stop_price": f"{levels['stop_price']:.6f}" if stop else None,
        "time_in_force": "gtc",
        "created_at": _ago(0.01),
        **extra,
    }


#: Old orders of the account, long finished: what makes a list reach back.
HISTORY = [
    {
        "id": f"0ld0-bbbb-{i:04d}",
        "symbol": "XYZ",
        "side": "buy",
        "type": "limit",
        "trigger": "immediate",
        "state": "filled",
        "quantity": "1.000000",
        "price": "95.000000",
        "time_in_force": "gfd",
        "created_at": _ago(30 + i),
    }
    for i in range(3)
]
WORKING = [_broker_order(order_id, action, qty, levels) for order_id, action, qty, levels in BOOK]


@pytest.fixture
def broker(db, tmp_path, monkeypatch):
    """The tools on a real journal; a stand-in broker whose answers each test sets.

    ``symbol_list`` and ``account_list``: a list of orders, or None for a call
    that did not answer. ``by_id``: order id to order; any other id gets no
    answer, as a failed lookup does.
    """
    from evotrader import paths
    from evotrader.tools import asset_context

    shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
    config = AppConfig(data_dir=tmp_path / "data")
    config.settings.mode = TradingMode.LIVE
    state = SimpleNamespace(symbol_list=None, account_list=None, by_id={}, calls=[])

    async def call_mcp(tool: str, args: dict):
        state.calls.append((tool, dict(args)))
        if tool == "get_accounts":
            return {"data": {"accounts": []}}
        if "order_id" in args:
            order = state.by_id.get(args["order_id"])
            return {"data": {"orders": [order]}} if order else None
        if tool != "get_equity_orders":
            return None
        orders = state.symbol_list if "symbol" in args else state.account_list
        return None if orders is None else {"data": {"orders": orders}}

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


async def _hold_nine_under_four_orders() -> None:
    await tools.record_trade(
        json.dumps(
            {
                "ticker": "XYZ",
                "action": "OPEN",
                "direction": "LONG",
                "quantity": 9,
                "order_type": "market",
                "price": 100.0,
                "status": "FILLED",
                "algo_signal": 0.1,
                "hybrid_score": 0.1,
                "regime": "trending_bull",
                "algo_version": "v001",
                "reasoning": "made up",
            }
        )
    )
    for order_id, action, qty, levels in BOOK:
        await tools.record_trade(
            json.dumps(
                {
                    "ticker": "XYZ",
                    "action": action,
                    "direction": "LONG",
                    "quantity": qty,
                    "order_type": "stop_market" if action == "STOP_LOSS" else "limit",
                    "time_in_force": "gtc",
                    "order_id": order_id,
                    "status": "PENDING",
                    "related_trade_id": 1,
                    "algo_signal": 0.1,
                    "hybrid_score": 0.1,
                    "regime": "trending_bull",
                    "algo_version": "v001",
                    "reasoning": "made up",
                    **levels,
                }
            )
        )


async def _status(order_id: str) -> str:
    async with tools._db.connection() as conn:
        row = await (
            await conn.execute("SELECT order_status FROM trades WHERE order_id = ?", (order_id,))
        ).fetchone()
    return row["order_status"]


class TestAFailedReadIsNotAbsence:
    async def test_nothing_answered_nothing_is_cancelled(self, broker) -> None:
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        assert result["reconciled"] == []
        assert [u["order_id"] for u in result["unverified"]] == sorted(IDS)
        assert all("did not answer" in u["reason"] for u in result["unverified"])
        assert [await _status(i) for i in IDS] == ["PENDING"] * 4
        assert result["order_book"]["listing_status"]["XYZ"] == {"ok": False}
        assert "did not answer" in result["order_book"]["errors"][0]

    async def test_the_coverage_reads_unverified_not_none(self, broker) -> None:
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        block = result["protective_coverage"]["XYZ"]
        assert block["status"] == "unverified"
        assert (block["covered_qty"], block["take_profit_qty"], block["no_order_qty"]) == (6, 3, 0)
        assert block["unverified_order_ids"] == sorted(IDS)
        assert "not a gap" in block["unverified_note"]
        positions = await tools.get_open_positions()
        assert positions["protective_coverage"]["XYZ"]["status"] == "unverified"

    async def test_an_empty_answer_is_no_evidence_either(self, broker) -> None:
        broker.symbol_list, broker.account_list = [], []
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        assert [await _status(i) for i in IDS] == ["PENDING"] * 4
        assert all("answered empty" in u["reason"] for u in result["unverified"])

    async def test_a_list_that_does_not_reach_back_is_no_evidence(self, broker) -> None:
        newer = [{**HISTORY[0], "created_at": _ago(-0.01)}]  # only orders newer than ours
        broker.symbol_list, broker.account_list = newer, newer
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        assert [await _status(i) for i in IDS] == ["PENDING"] * 4
        assert all("does not reach back" in u["reason"] for u in result["unverified"])

    async def test_the_account_list_finds_what_the_symbol_list_missed(self, broker) -> None:
        broker.symbol_list = None  # the symbol list fails; the whole account's answers
        broker.account_list = [*WORKING, *HISTORY]
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        assert result["reconciled"] == [] and result["unverified"] is None
        assert sorted(result["order_book"]["order_ids"]) == sorted(IDS)
        block = result["protective_coverage"]["XYZ"]
        assert (block["status"], block["no_order_qty"]) == ("split", 0)
        assert "unverified_order_ids" not in block
        asked = [args for tool, args in broker.calls if tool == "get_equity_orders"]
        assert sum("symbol" not in a and "order_id" not in a for a in asked) == 1, "once a pass"

    async def test_the_next_good_read_clears_it(self, broker) -> None:
        await _hold_nine_under_four_orders()
        await tools.reconcile_pending_orders()
        broker.symbol_list = [*WORKING, *HISTORY]
        result = await tools.reconcile_pending_orders()
        assert result["unverified"] is None
        assert result["protective_coverage"]["XYZ"]["status"] == "split"


class TestAbsenceThatIsEvidence:
    async def test_missing_from_a_list_that_reaches_back_is_gone(self, broker) -> None:
        broker.symbol_list = [*WORKING[:3], *HISTORY]  # the last take-profit is not there
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        [gone] = result["reconciled"]
        assert (gone["order_id"], gone["state"]) == ("t4ke-aaaa-0004", "cancelled")
        assert await _status("t4ke-aaaa-0004") == "CANCELLED"
        assert [await _status(i) for i in IDS[:3]] == ["PENDING"] * 3
        async with tools._db.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT broker_status_reason FROM trades WHERE order_id = 't4ke-aaaa-0004'"
                )
            ).fetchone()
        assert row["broker_status_reason"].startswith("Not on the broker: its XYZ order list")
        assert "answered without it" in row["broker_status_reason"]

    async def test_an_answer_about_another_order_is_not_this_one(self, broker) -> None:
        other = {**WORKING[0], "id": "0th3r-cccc-0009"}
        broker.by_id = {"t4ke-aaaa-0004": other}
        await _hold_nine_under_four_orders()
        result = await tools.reconcile_pending_orders()
        assert "t4ke-aaaa-0004" in {u["order_id"] for u in result["unverified"]}
        assert await _status("t4ke-aaaa-0004") == "PENDING"


class TestTheCoverageArithmetic:
    LOTS = [{"id": 1, "ticker": "XYZ", "action": "OPEN", "quantity": 9, "remaining_quantity": 9}]

    def _book(self, *rows) -> list[dict]:
        return [
            {
                "order_id": order_id,
                "ticker": "XYZ",
                "action": action,
                "quantity": qty,
                "time_in_force": "gtc",
                "source": "broker_sync",
                **levels,
            }
            for order_id, action, qty, levels in rows
        ]

    def test_a_covered_book_becomes_unverified(self) -> None:
        block = coverage_by_ticker(self.LOTS, self._book(*BOOK), {"st0p-aaaa-0001": "no answer"})
        assert block["XYZ"]["status"] == "unverified"
        assert block["XYZ"]["unverified_order_ids"] == ["st0p-aaaa-0001"]

    def test_a_shortfall_is_never_hidden_behind_it(self) -> None:
        block = coverage_by_ticker(self.LOTS, self._book(BOOK[0]), {"st0p-aaaa-0001": "no answer"})
        assert block["XYZ"]["status"] == "partial"
        assert block["XYZ"]["no_order_qty"] == 5
        assert block["XYZ"]["unverified_order_ids"] == ["st0p-aaaa-0001"]

    def test_without_unconfirmed_orders_nothing_changes(self) -> None:
        assert coverage_by_ticker(self.LOTS, self._book(*BOOK), None)["XYZ"]["status"] == "split"
        assert "unverified_order_ids" not in coverage_by_ticker(self.LOTS, self._book(*BOOK))["XYZ"]


REFUSED = {
    "meta": {"rh_error_category": "invalid_request"},
    "content": [{"type": "text", "text": 'API error 400: {"detail":"Not enough shares to sell."}'}],
    "isError": True,
}


class TestARefusedSellNamesTheOrdersHoldingTheShares:
    def test_it_is_recognised(self) -> None:
        assert is_reserved_shares_refusal({"side": "sell"}, REFUSED)
        assert not is_reserved_shares_refusal({"side": "buy"}, REFUSED)
        other = {**REFUSED, "content": [{"type": "text", "text": "API error 400: bad price"}]}
        assert not is_reserved_shares_refusal({"side": "sell"}, other)

    def test_the_working_sells_are_named(self) -> None:
        held = reserving_orders([*WORKING, *HISTORY])
        assert [o["order_id"] for o in held] == IDS
        out = annotate_refusal(REFUSED, held)
        assert out["isError"] is True and out["content"] == REFUSED["content"]
        assert "holds 9 shares" in out["note"] and "do not cancel" in out["note"]

    def test_an_unreadable_list_is_said(self) -> None:
        assert "could not be read" in annotate_refusal(REFUSED, None)["note"]
        assert "fewer than the order asks" in annotate_refusal(REFUSED, [])["note"]


class TestTheExecutor:
    @pytest.fixture
    def executor(self, broker, monkeypatch):
        from evotrader.agents.factory import create_execution_agent

        config = tools._config
        config.settings.require_trade_approval = False
        return create_execution_agent(config)

    async def test_it_is_handed_its_account(self, executor) -> None:
        line = await executor.instruction(MagicMock())
        assert "account_number ACCT1" in line and "never write an account number" in line

    async def test_an_order_for_another_account_is_refused(self, executor) -> None:
        tool = MagicMock()
        tool.name = "place_equity_order"
        verdict = await executor.before_tool_callback(
            tool=tool,
            args={"symbol": "XYZ", "side": "sell", "quantity": 1, "account_number": "OTHER9"},
            tool_context=MagicMock(),
        )
        assert verdict["action"] == "WRONG_ACCOUNT" and verdict["retryable"] is True
        assert "ACCT1" in verdict["fix"]

    async def test_a_refused_sell_comes_back_with_the_orders(self, executor, broker) -> None:
        broker.symbol_list = [*WORKING, *HISTORY]
        tool = MagicMock()
        tool.name = "place_equity_order"
        out = await executor.after_tool_callback(
            tool=tool,
            args={"symbol": "XYZ", "side": "sell", "quantity": 2, "account_number": "ACCT1"},
            tool_context=MagicMock(),
            tool_response=REFUSED,
        )
        assert [o["order_id"] for o in out["reserved_by"]] == IDS
        assert "protection that exists" in out["note"]
