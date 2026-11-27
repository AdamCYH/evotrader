"""A sale is never recorded as an entry: the broker's side outranks the label.

Found by the evolution agent's code review. The executor placed a resting
take-profit (a gtc sell limit) and recorded it with action OPEN, direction
LONG. ``record_trade`` remapped SELL/TRIM to an exit but trusted a literal
OPEN, so the sale became a lot the account never bought. Once it filled, the
journal held twice the sale's shares more than the broker, the protection
check reported shares uncovered that did not exist, and the day's position
check closed the surplus at the day's price: a loss that never happened,
while the sale's own result went unbooked.

Three places now read the order's side (``db/order_side.py``):

* ``record_trade``, when the executor records the order;
* the pending-order check, when it next reads the order from the broker, so
  a sale recorded while the broker could not be asked is relabelled before
  it fills;
* the position check, which no longer closes a surplus that a sale explains
  (it reports the lot for repair instead of booking it).

Made-up ticker, prices, quantities and order ids; the broker is a stand-in.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.agents import tools
from evotrader.db.journal import TradeJournal
from evotrader.db.order_side import action_for_side, broker_side
from evotrader.db.reconciliation import ReconciliationService
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

TICKER = "XYZ"
SALE = "5a1e0000-0000-4000-8000-000000000001"
BUY = "b0b00000-0000-4000-8000-000000000002"


# ── The rule ────────────────────────────────────────────────────────────


class TestTheRule:
    @pytest.mark.parametrize(
        "order_type, tif, implied",
        [
            ("limit", "gtc", "TAKE_PROFIT"),
            ("limit", "gfd", "CLOSE"),
            ("market", None, "CLOSE"),
            ("stop_market", "gtc", "STOP_LOSS"),
            ("stop_limit", "gtc", "STOP_LOSS"),
        ],
    )
    def test_a_sell_recorded_as_an_entry_is_an_exit(self, order_type, tif, implied) -> None:
        assert action_for_side("OPEN", "LONG", "sell", order_type, tif) == implied

    @pytest.mark.parametrize("action", ["CLOSE", "TAKE_PROFIT", "STOP_LOSS"])
    def test_a_buy_recorded_as_an_exit_is_an_entry(self, action) -> None:
        assert action_for_side(action, "LONG", "buy", "market") == "OPEN"

    @pytest.mark.parametrize(
        "action, direction, side",
        [
            ("OPEN", "LONG", "buy"),  # agrees
            ("TAKE_PROFIT", "LONG", "sell"),  # agrees
            ("OPEN", "SHORT", "sell"),  # a short entry is what it says
            ("CLOSE", "SHORT", "buy"),  # so is a short cover
            ("OPEN", None, "sell"),  # only an explicit LONG is judged
            ("OPEN", "LONG", None),  # the side is unknown
            ("CANCEL", "LONG", "sell"),
        ],
    )
    def test_everything_else_stands(self, action, direction, side) -> None:
        assert action_for_side(action, direction, side, "limit", "gtc") is None

    def test_the_side_is_read_from_the_order(self) -> None:
        assert broker_side({"side": "SELL"}) == "sell"
        assert broker_side({"side": "buy"}) == "buy"
        assert broker_side({"side": ""}) is None
        assert broker_side(None) is None


# ── record_trade ────────────────────────────────────────────────────────


@pytest.fixture
def tools_on_a_journal(db, monkeypatch):
    """The tools on a real journal with a broker connection; sides by order id."""
    sides: dict[str, str | None] = {}

    async def side_of(order_id: str) -> str | None:
        return sides.get(order_id)

    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_journal", TradeJournal(db))
    monkeypatch.setattr(tools, "_memory", None)
    monkeypatch.setattr(tools, "_sim_proxy", None)
    monkeypatch.setattr(tools, "_mcp_toolset", object())
    monkeypatch.setattr(tools, "_current_session_id", "s-1")
    monkeypatch.setattr(tools, "_open_positions_cache", None)
    monkeypatch.setattr(tools, "_protective_coverage", AsyncMock(return_value={}))
    monkeypatch.setattr(tools, "_stamp_fill_time", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_broker_order_side", side_of)
    return sides


async def _record(**fields) -> dict:
    return await tools.record_trade(
        json.dumps(
            {
                "ticker": TICKER,
                "direction": "LONG",
                "algo_signal": -0.3,
                "hybrid_score": -0.3,
                "regime": "trending_bear",
                "algo_version": "v001",
                "reasoning": "made up",
                **fields,
            }
        )
    )


async def _row(trade_id: int) -> dict:
    async with tools._db.connection() as conn:
        cursor = await conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        return dict(await cursor.fetchone())


async def _held() -> float:
    return sum(float(t["remaining_quantity"]) for t in await tools._journal.get_open_trades())


async def _a_lot(quantity: float = 10, price: float = 30.0) -> int:
    out = await _record(
        action="OPEN", quantity=quantity, order_type="market", price=price, status="FILLED"
    )
    return out["trade_ids"][0]


THE_SALE = {
    "action": "OPEN",
    "quantity": 4,
    "order_type": "limit",
    "limit_price": 34.0,
    "time_in_force": "gtc",
    "order_id": SALE,
    "status": "PENDING",
}


class TestRecordTrade:
    async def test_a_resting_sale_recorded_as_open_is_a_take_profit(
        self, tools_on_a_journal
    ) -> None:
        lot = await _a_lot()
        tools_on_a_journal[SALE] = "sell"
        out = await _record(**THE_SALE)
        row = await _row(out["trade_ids"][0])
        assert row["action"] == "TAKE_PROFIT"
        assert row["direction"] == "LONG"
        assert row["related_trade_id"] == lot
        assert row["broker_status_reason"].startswith("SIDE_CORRECTED from OPEN to TAKE_PROFIT")
        assert out["side_corrected"].startswith("SIDE_CORRECTED")
        assert "side_unverified" not in out
        assert await _held() == pytest.approx(10.0), "no lot was added"

    async def test_its_fill_closes_shares_of_the_lot(self, tools_on_a_journal) -> None:
        lot = await _a_lot()
        tools_on_a_journal[SALE] = "sell"
        await _record(**THE_SALE)
        await tools._journal.update_order_status(SALE, "FILLED", fill_price=34.5)
        assert await _held() == pytest.approx(6.0)
        async with tools._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT related_trade_id, realized_pnl FROM trades WHERE order_id = ?", (SALE,)
            )
            (sale,) = [dict(r) for r in await cursor.fetchall()]
        assert sale["related_trade_id"] == lot
        assert sale["realized_pnl"] == pytest.approx(4 * (34.5 - 30.0))

    async def test_a_second_record_of_the_same_sale_writes_nothing(
        self, tools_on_a_journal
    ) -> None:
        await _a_lot()
        tools_on_a_journal[SALE] = "sell"
        first = await _record(**THE_SALE)
        again = await _record(**THE_SALE)
        assert again["deduplicated"] is True
        assert again["trade_ids"] == first["trade_ids"]

    async def test_a_sale_carries_no_entry_plan(self, tools_on_a_journal) -> None:
        await _a_lot()
        tools_on_a_journal[SALE] = "sell"
        plan = {"stop_price": 28.0, "stop_qty": 4, "tp_limit_price": 36.0, "tp_qty": 0}
        out = await _record(**THE_SALE, protection_plan=plan)
        assert out["plan_recorded"] is False
        assert "sale" in out["plan_problems"][0]

    async def test_a_buy_recorded_as_a_close_is_an_entry(self, tools_on_a_journal) -> None:
        await _a_lot()
        tools_on_a_journal[BUY] = "buy"
        out = await _record(
            action="CLOSE",
            quantity=3,
            order_type="market",
            price=31.0,
            order_id=BUY,
            status="FILLED",
            fill_price=31.0,
        )
        row = await _row(out["trade_ids"][0])
        assert row["action"] == "OPEN"
        assert row["related_trade_id"] is None
        assert await _held() == pytest.approx(13.0)

    async def test_a_buy_recorded_as_open_is_left_alone(self, tools_on_a_journal) -> None:
        tools_on_a_journal[BUY] = "buy"
        out = await _record(
            action="OPEN", quantity=3, order_type="limit", limit_price=30.0, order_id=BUY
        )
        assert (await _row(out["trade_ids"][0]))["action"] == "OPEN"
        assert "side_corrected" not in out and "side_unverified" not in out

    async def test_when_the_broker_cannot_be_asked_it_is_recorded_as_given(
        self, tools_on_a_journal
    ) -> None:
        await _a_lot()
        out = await _record(**THE_SALE)  # no side known for SALE
        assert (await _row(out["trade_ids"][0]))["action"] == "OPEN"
        assert out["side_unverified"] is True

    async def test_an_unchecked_first_record_is_set_right_by_the_next(
        self, tools_on_a_journal
    ) -> None:
        await _a_lot()
        first = await _record(**THE_SALE)  # the broker could not be asked
        assert first["side_unverified"] is True
        tools_on_a_journal[SALE] = "sell"
        second = await _record(**THE_SALE)
        assert second["side_corrected"]
        old, new = await _row(first["trade_ids"][0]), await _row(second["trade_ids"][0])
        assert (old["action"], old["order_status"]) == ("OPEN", "DUPLICATE")
        assert old["broker_status_reason"].endswith(f"row #{new['id']} records it")
        assert (new["action"], new["order_status"]) == ("TAKE_PROFIT", "PENDING")
        await tools._journal.update_order_status(SALE, "FILLED", fill_price=34.5)
        assert await _held() == pytest.approx(6.0), "the sale closes its shares once"

    async def test_without_a_broker_nothing_is_asked(self, tools_on_a_journal, monkeypatch) -> None:
        monkeypatch.setattr(tools, "_mcp_toolset", None)
        asked = AsyncMock(return_value="sell")
        monkeypatch.setattr(tools, "_broker_order_side", asked)
        out = await _record(**THE_SALE)
        asked.assert_not_awaited()
        assert "side_unverified" not in out


class TestTheLookup:
    async def test_asks_only_for_the_equity_order(self, monkeypatch) -> None:
        monkeypatch.setattr(tools, "_agentic_account_number", AsyncMock(return_value="ACCT"))
        calls: list[str] = []

        async def call(tool: str, args: dict) -> dict:
            calls.append(tool)
            return {"data": {"orders": [{"id": args["order_id"], "side": "sell"}]}}

        monkeypatch.setattr(tools, "_call_mcp_tool", call)
        assert await tools._broker_order_side(SALE) == "sell"
        assert calls == ["get_equity_orders"]

    async def test_a_failed_read_is_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(
            tools, "_agentic_account_number", AsyncMock(side_effect=RuntimeError("down"))
        )
        assert await tools._broker_order_side(SALE) is None


# ── The pending-order check ─────────────────────────────────────────────


def _proposal(action: TradeAction, quantity: float, price: float, **extra) -> TradeProposal:
    return TradeProposal(
        ticker=TICKER,
        direction=TradeDirection.LONG,
        action=action,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=price,
        hybrid_score=0.3,
        confidence=0.5,
        algo_signal=0.3,
        llm_signal=0.3,
        regime="trending_bear",
        algo_version="v001",
        reasoning="made up",
        **extra,
    )


class TestThePendingRow:
    async def test_a_pending_sale_recorded_as_open_is_relabelled(self, db) -> None:
        journal = TradeJournal(db)
        older = await journal.record_trade(_proposal(TradeAction.OPEN, 6, 30.0))
        await journal.record_trade(_proposal(TradeAction.OPEN, 6, 32.0))
        [sale] = await journal.record_trade(
            _proposal(TradeAction.OPEN, 4, 34.0, time_in_force="gtc"),
            order_status="PENDING",
            order_id=SALE,
        )
        assert await journal.correct_pending_side(SALE, "sell") == [sale]
        row = await journal.get_trade_by_id(sale)
        assert row["action"] == "TAKE_PROFIT"
        assert row["related_trade_id"] == older[0], "the oldest open lot"
        assert "SIDE_CORRECTED from OPEN to TAKE_PROFIT" in row["broker_status_reason"]
        assert await journal.correct_pending_side(SALE, "sell") == [], "once"

    async def test_a_second_record_of_the_order_is_a_duplicate(self, db) -> None:
        journal = TradeJournal(db)
        await journal.record_trade(_proposal(TradeAction.OPEN, 6, 30.0))
        [wrong] = await journal.record_trade(
            _proposal(TradeAction.OPEN, 4, 34.0, time_in_force="gtc"),
            order_status="PENDING",
            order_id=SALE,
        )
        [right] = await journal.record_trade(
            _proposal(TradeAction.TAKE_PROFIT, 4, 34.0, time_in_force="gtc"),
            order_status="PENDING",
            order_id=SALE,
        )
        assert await journal.correct_pending_side(SALE, "sell") == [wrong]
        row = await journal.get_trade_by_id(wrong)
        assert (row["action"], row["order_status"]) == ("OPEN", "DUPLICATE")
        assert (await journal.get_trade_by_id(right))["order_status"] == "PENDING"

    async def test_filled_rows_and_agreeing_rows_are_left_alone(self, db) -> None:
        journal = TradeJournal(db)
        await journal.record_trade(_proposal(TradeAction.OPEN, 6, 30.0), order_id=SALE)
        assert await journal.correct_pending_side(SALE, "sell") == [], "FILLED: not this path"
        await journal.record_trade(
            _proposal(TradeAction.OPEN, 2, 29.0), order_status="PENDING", order_id=BUY
        )
        assert await journal.correct_pending_side(BUY, "buy") == []
        assert await journal.correct_pending_side(BUY, None) == []


class _Broker:
    """A broker stand-in for the pending-order check: orders by id."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}

    async def call(self, tool: str, args: dict) -> dict | None:
        if tool == "get_accounts":
            return {"data": {"accounts": []}}
        if tool != "get_equity_orders":
            return None
        if "order_id" in args:
            found = [o for o in self.orders.values() if o["id"] == args["order_id"]]
            return {"data": {"orders": found}} if found else None
        return {"data": {"orders": list(self.orders.values())}}


class TestThePendingCheck:
    async def test_relabels_the_sale_before_it_fills(self, db, tmp_path, monkeypatch) -> None:
        import shutil

        from evotrader import paths
        from evotrader.config import AppConfig
        from evotrader.models.config import TradingMode
        from evotrader.tools import asset_context

        shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
        config = AppConfig(data_dir=tmp_path / "data")
        config.settings.mode = TradingMode.LIVE
        broker = _Broker()
        journal = TradeJournal(db)
        monkeypatch.setattr(tools, "_db", db)
        monkeypatch.setattr(tools, "_journal", journal)
        monkeypatch.setattr(tools, "_config", config)
        monkeypatch.setattr(tools, "_metrics", None)
        monkeypatch.setattr(tools, "_memory", None)
        monkeypatch.setattr(tools, "_sim_proxy", None)
        monkeypatch.setattr(tools, "_current_session_id", "s-1")
        monkeypatch.setattr(tools, "_open_positions_cache", None)
        monkeypatch.setattr(tools, "_unconfirmed_orders", {})
        monkeypatch.setattr(tools, "_call_mcp_tool", broker.call)
        monkeypatch.setattr(tools, "_agentic_account_number", AsyncMock(return_value="ACCT1"))
        monkeypatch.setattr(asset_context, "allowed_tickers", lambda: (TICKER,))

        [lot] = await journal.record_trade(_proposal(TradeAction.OPEN, 10, 30.0))
        [sale] = await journal.record_trade(
            _proposal(TradeAction.OPEN, 4, 34.0, time_in_force="gtc"),
            order_status="PENDING",
            order_id=SALE,
        )
        await journal.save_pending_order(SALE, "{}", "s-1")
        broker.orders[SALE] = {
            "id": SALE,
            "symbol": TICKER,
            "side": "sell",
            "type": "limit",
            "state": "filled",
            "quantity": "4.000000",
            "price": "34.000000",
            "average_price": "34.500000",
            "cumulative_quantity": "4.000000",
            "time_in_force": "gtc",
            "created_at": "2026-03-04T15:00:00Z",
            "last_transaction_at": "2026-03-04T16:00:00Z",
        }

        out = await tools.reconcile_pending_orders()

        assert out["side_corrected"] == [{"order_id": SALE, "trade_ids": [sale], "side": "sell"}]
        row = await journal.get_trade_by_id(sale)
        assert (row["action"], row["order_status"], row["related_trade_id"]) == (
            "TAKE_PROFIT",
            "FILLED",
            lot,
        )
        assert row["realized_pnl"] == pytest.approx(4 * (34.5 - 30.0))
        assert sum(float(t["remaining_quantity"]) for t in await journal.get_open_trades()) == (
            pytest.approx(6.0)
        )


# ── The position check ──────────────────────────────────────────────────


class _PositionBroker:
    """One equity position and the orders the broker knows, with their sides."""

    def __init__(self, quantity: float, average: float, sides: dict[str, str]) -> None:
        self.position = {
            "symbol": TICKER,
            "quantity": str(quantity),
            "average_buy_price": str(average),
            "asset_type": "EQUITY",
        }
        self.sides = sides
        self.lookups: list[str] = []
        self._mcp_session_manager = MagicMock()
        self._mcp_session_manager.create_session = self._session

    async def _session(self) -> _PositionBroker:
        return self

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MagicMock:
        res = MagicMock()
        res.isError = False
        if name == "get_accounts":
            payload: dict[str, Any] = {"accounts": [{"account_number": "A1", "is_default": True}]}
        elif name == "get_equity_positions":
            payload = {"positions": [self.position]}
        elif name == "get_option_positions":
            payload = {"positions": []}
        elif name == "get_equity_orders":
            order_id = arguments.get("order_id", "")
            self.lookups.append(order_id)
            side = self.sides.get(order_id)
            order = {"id": order_id, "state": "filled", "side": side, "average_price": "0"}
            payload = {"orders": [order] if side else []}
        else:
            payload = {"results": []}
        res.content = [MagicMock(text=json.dumps({"data": payload}))]
        return res


async def _count(db) -> int:
    async with db.connection() as conn:
        return (await (await conn.execute("SELECT COUNT(*) AS n FROM trades")).fetchone())["n"]


class TestThePositionCheck:
    async def test_a_surplus_a_sale_explains_is_reported_not_closed(self, db) -> None:
        journal = TradeJournal(db)
        await journal.record_trade(_proposal(TradeAction.OPEN, 10, 30.0), order_id=BUY)
        [mislabelled] = await journal.record_trade(
            _proposal(TradeAction.OPEN, 4, 34.0), order_id=SALE
        )
        # The journal holds 14; the broker 6 (10 bought, 4 sold).
        broker = _PositionBroker(6, 30.0, {BUY: "buy", SALE: "sell"})
        before = await _count(db)

        result = await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert result["status"] == "SIDE_MISMATCH"
        assert result["side_mismatch"]["trade_ids"] == [mislabelled]
        assert "repair" in result["message"]
        assert await _count(db) == before, "no sync row: no loss booked that never happened"

    async def test_a_side_is_asked_once(self, db) -> None:
        journal = TradeJournal(db)
        await journal.record_trade(_proposal(TradeAction.OPEN, 10, 30.0), order_id=BUY)
        await journal.record_trade(_proposal(TradeAction.OPEN, 4, 34.0), order_id=SALE)
        broker = _PositionBroker(6, 30.0, {BUY: "buy", SALE: "sell"})
        service = ReconciliationService(journal, broker, dry_run=False)
        await service.reconcile_positions(ticker=TICKER)
        await service.reconcile_positions(ticker=TICKER)
        assert broker.lookups.count(SALE) == 1

    async def test_any_other_surplus_is_closed_as_before(self, db) -> None:
        journal = TradeJournal(db)
        await journal.record_trade(_proposal(TradeAction.OPEN, 10, 30.0), order_id=BUY)
        broker = _PositionBroker(6, 30.0, {BUY: "buy"})
        before = await _count(db)

        result = await ReconciliationService(journal, broker, dry_run=False).reconcile_positions(
            ticker=TICKER
        )

        assert result["status"] == "RECONCILED"
        assert "side_mismatch" not in result
        assert await _count(db) == before + 1
