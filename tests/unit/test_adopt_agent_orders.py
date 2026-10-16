"""A sell the agents placed and never recorded is journaled, while working or at its fill.

Once the executor placed a stop and a take-profit for a new position and
recorded neither. The broker sync listed both (``pending_orders``, source
``broker_sync``) and the coverage counted them, but no trades row carried
their ids: had either filled, the fill path would have found nothing to
promote, and the position check would have closed the shares as a sync row at
its own price, outside every decision P&L figure.

Made-up ticker, prices, ids and times.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.db.journal import TradeJournal
from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

STOP_ID = "7b2c0d10-0000-4000-8000-000000000001"
TP_ID = "7b2c0d10-0000-4000-8000-000000000002"


@pytest.fixture
def journal(db) -> TradeJournal:
    return TradeJournal(db)


@pytest.fixture
def tools_on(journal, db, monkeypatch):
    from evotrader.agents import tools
    from evotrader.tools import asset_context

    tools._journal = journal
    tools._db = db
    tools._current_session_id = "sync-session"
    tools._open_positions_cache = None
    monkeypatch.setattr(asset_context, "allowed_tickers", lambda: ("XYZ",))
    yield tools
    tools._journal = None
    tools._db = None
    tools._current_session_id = None
    tools._open_positions_cache = None


def _lot(qty: float, price: float) -> TradeProposal:
    return TradeProposal(
        ticker="XYZ",
        direction=TradeDirection.LONG,
        action=TradeAction.OPEN,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price=price,
        algo_signal=0.31,
        llm_signal=0.4,
        hybrid_score=0.35,
        confidence=0.55,
        regime="trending_bull",
        algo_version="composite_v001",
    )


def _ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _stop(**extra) -> dict:
    return {
        "id": STOP_ID,
        "symbol": "XYZ",
        "side": "sell",
        "type": "market",
        "trigger": "stop",
        "state": "confirmed",
        "quantity": "15.000000",
        "price": None,
        "stop_price": "45.000000",
        "time_in_force": "gtc",
        "market_hours": "regular_hours",
        "placed_agent": "agentic",
        "created_at": _ago(60),
        **extra,
    }


def _take_profit(price: str = "58.000000", **extra) -> dict:
    return {
        "id": TP_ID,
        "symbol": "XYZ",
        "side": "sell",
        "type": "limit",
        "trigger": "immediate",
        "state": "confirmed",
        "quantity": "5.000000",
        "price": price,
        "stop_price": None,
        "time_in_force": "gtc",
        "market_hours": "regular_hours",
        "placed_agent": "agentic",
        "created_at": _ago(60),
        **extra,
    }


def _broker(monkeypatch, tools, orders: list[dict]) -> None:
    async def mcp(tool_name: str, arguments: dict) -> dict:
        if tool_name != "get_equity_orders":
            return {"data": {"orders": []}}
        if "order_id" in arguments:
            return {"data": {"orders": [o for o in orders if o["id"] == arguments["order_id"]]}}
        return {"data": {"orders": orders}}

    async def account() -> str:
        return "acct"

    monkeypatch.setattr(tools, "_call_mcp_tool", mcp)
    monkeypatch.setattr(tools, "_agentic_account_number", account)


async def _rows(journal: TradeJournal, order_id: str) -> list[dict]:
    async with journal._db.connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM trades WHERE order_id = ? ORDER BY id", (order_id,)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def test_unrecorded_agent_orders_are_adopted_against_the_lot(journal, tools_on, monkeypatch):
    [lot] = await journal.record_trade(_lot(20, 50.00))
    _broker(monkeypatch, tools_on, [_stop(), _take_profit()])

    book = await tools_on.sync_open_orders_from_broker("acct")

    assert sorted(book["adopted"]) == sorted([STOP_ID, TP_ID])
    [stop] = await _rows(journal, STOP_ID)
    [tp] = await _rows(journal, TP_ID)
    assert (stop["action"], stop["quantity"], stop["stop_price"]) == ("STOP_LOSS", 15, 45.0)
    assert (tp["action"], tp["quantity"], tp["limit_price"]) == ("TAKE_PROFIT", 5, 58.0)
    for row in (stop, tp):
        assert row["order_status"] == "PENDING"
        assert row["related_trade_id"] == lot
        assert row["time_in_force"] == "gtc"
        assert row["broker_status_reason"].startswith("ADOPTED_FROM_BROKER ")
        assert (row["algo_version"], row["regime"], row["algo_signal"]) == (
            "composite_v001",
            "trending_bull",
            0.31,
        )
        assert row["realized_pnl"] is None


async def test_it_is_recorded_in_the_cycle_that_placed_it(journal, tools_on, db, monkeypatch):
    await journal.record_trade(_lot(20, 50.00))
    placed = datetime.now(UTC) - timedelta(minutes=60)
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO agent_thought_log (timestamp, session_id, agent_name, event_type, "
            "content, meta) VALUES (?,?,?,?,?,?)",
            (
                (placed + timedelta(seconds=1)).isoformat(),
                "placing-cycle",
                "execution",
                "tool_response",
                "place_equity_order",
                json.dumps({"response": {"order": {"id": STOP_ID}}}),
            ),
        )
    _broker(monkeypatch, tools_on, [_stop(created_at=placed.isoformat())])

    await tools_on.sync_open_orders_from_broker("acct")

    [stop] = await _rows(journal, STOP_ID)
    assert stop["session_id"] == "placing-cycle"


async def test_a_limit_sell_below_the_entry_is_a_close(journal, tools_on, monkeypatch):
    await journal.record_trade(_lot(20, 50.00))
    _broker(monkeypatch, tools_on, [_take_profit(price="48.000000")])

    await tools_on.sync_open_orders_from_broker("acct")

    [row] = await _rows(journal, TP_ID)
    assert row["action"] == "CLOSE"


@pytest.mark.parametrize(
    "order",
    [
        _stop(placed_agent=None),  # a person's order in the app
        _stop(placed_agent="user"),
        _stop(created_at=_ago(2)),  # the executor may still record it
        _stop(side="buy"),
    ],
)
async def test_what_is_not_adopted(journal, tools_on, monkeypatch, order):
    await journal.record_trade(_lot(20, 50.00))
    _broker(monkeypatch, tools_on, [order])

    book = await tools_on.sync_open_orders_from_broker("acct")

    assert book["adopted"] is None
    assert await _rows(journal, STOP_ID) == []
    assert book["order_ids"] == [STOP_ID], "it is still in the order book"


async def test_a_recorded_order_and_a_second_sync_add_nothing(journal, tools_on, monkeypatch):
    await journal.record_trade(_lot(20, 50.00))
    await journal.record_trade(
        TradeProposal(
            ticker="XYZ",
            direction=TradeDirection.LONG,
            action=TradeAction.TAKE_PROFIT,
            quantity=5,
            order_type=OrderType.LIMIT,
            limit_price=58.0,
            time_in_force="gtc",
        ),
        order_status="PENDING",
        order_id=TP_ID,
    )
    _broker(monkeypatch, tools_on, [_stop(), _take_profit()])

    first = await tools_on.sync_open_orders_from_broker("acct")
    second = await tools_on.sync_open_orders_from_broker("acct")

    assert first["adopted"] == [STOP_ID]
    assert second["adopted"] is None
    assert len(await _rows(journal, STOP_ID)) == 1
    assert len(await _rows(journal, TP_ID)) == 1


async def test_an_adopted_stop_that_fills_closes_its_lots_with_pnl(journal, tools_on, monkeypatch):
    [first] = await journal.record_trade(_lot(10, 50.00))
    [second] = await journal.record_trade(_lot(5, 52.00))
    _broker(monkeypatch, tools_on, [_stop()])
    await tools_on.sync_open_orders_from_broker("acct")

    filled = _stop(
        state="filled",
        cumulative_quantity="15.000000",
        average_price="44.900000",
    )
    _broker(monkeypatch, tools_on, [filled])
    await tools_on.reconcile_pending_orders()

    rows = await _rows(journal, STOP_ID)
    assert {r["related_trade_id"]: r["quantity"] for r in rows} == {first: 10, second: 5}
    assert all(r["order_status"] == "FILLED" for r in rows)
    assert sum(r["realized_pnl"] for r in rows) == pytest.approx(
        (44.9 - 50.0) * 10 + (44.9 - 52.0) * 5
    )
    assert await journal.get_open_trades() == []


async def test_a_fill_with_no_row_is_recorded_at_the_fill(journal, tools_on, monkeypatch):
    """Placed and filled before it was old enough to adopt: recorded at the fill."""
    [lot] = await journal.record_trade(_lot(15, 50.00))
    young = _stop(created_at=_ago(3))
    _broker(monkeypatch, tools_on, [young])
    await tools_on.sync_open_orders_from_broker("acct")
    assert await _rows(journal, STOP_ID) == []

    filled = {
        **young,
        "state": "filled",
        "cumulative_quantity": "15.000000",
        "average_price": "44.950000",
    }
    _broker(monkeypatch, tools_on, [filled])
    result = await tools_on.reconcile_pending_orders()

    [row] = await _rows(journal, STOP_ID)
    assert (row["action"], row["order_status"], row["related_trade_id"]) == (
        "STOP_LOSS",
        "FILLED",
        lot,
    )
    assert row["fill_price"] == pytest.approx(44.95)
    assert row["realized_pnl"] == pytest.approx((44.95 - 50.0) * 15)
    entry = next(r for r in result["reconciled"] if r["order_id"] == STOP_ID)
    assert entry["adopted"] == [row["id"]]
    assert await journal.get_open_trades() == []


async def test_a_persons_fill_is_left_to_the_position_check(journal, tools_on, monkeypatch):
    await journal.record_trade(_lot(15, 50.00))
    manual = _stop(placed_agent=None)
    _broker(monkeypatch, tools_on, [manual])
    await tools_on.sync_open_orders_from_broker("acct")
    filled = {**manual, "state": "filled", "cumulative_quantity": "15", "average_price": "44.9"}
    _broker(monkeypatch, tools_on, [filled])

    await tools_on.reconcile_pending_orders()

    assert await _rows(journal, STOP_ID) == []
