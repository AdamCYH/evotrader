"""After protecting an entry, the follow-up keeps watching its stops for the window.

Found by the evolution agent's code review. A plan ended at the follow-up's
first check (``covered`` or ``placed``), so a protective order that ended after
that moment was unseen until the next cycle: a confirmed stop cancelled
outside the app between cycles left its shares with no stop until then. A
plan is now ``watching`` until the
window closes, then ``done``. When shares are under no order and a stop it
watches has ended without filling, it puts that stop back, once, at its level
and size, after the same checks as a plan's stop. A take-profit that ended is
named in the handoff, not re-placed.

Made-up ticker, prices and order ids; the broker is a stand-in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evotrader.agents import tools
from evotrader.config import AppConfig
from evotrader.db.journal import TradeJournal
from evotrader.models.config import TradingMode
from evotrader.tools import protection_followup
from evotrader.tools.trading_handoff import handoff_path

PLAN = {"stop_price": 90.0, "stop_qty": 2, "tp_limit_price": 108.0, "tp_qty": 1}


def _entry(order_id: str = "e1n7ry-0001", quantity: float = 3, plan: dict = PLAN) -> str:
    return json.dumps(
        {
            "ticker": "XYZ",
            "action": "OPEN",
            "direction": "LONG",
            "quantity": quantity,
            "order_type": "limit",
            "limit_price": 100.0,
            "time_in_force": "gfd",
            "order_id": order_id,
            "status": "PENDING",
            "algo_signal": 0.1,
            "hybrid_score": 0.1,
            "regime": "trending_bull",
            "algo_version": "v001",
            "reasoning": "made up",
            "protection_plan": plan,
        }
    )


@pytest.fixture
def broker(db, tmp_path, monkeypatch):
    """The tools on a real journal, with a stand-in broker at 105."""
    from evotrader.tools import market_hours

    config = AppConfig(data_dir=tmp_path / "data")
    config.constitution.risk_limits.max_stop_loss_pct = 0.20
    config.constitution.trading_rules.allowed_tickers = ["XYZ"]
    config.settings.protection_followup.enabled = True
    config.settings.mode = TradingMode.LIVE
    config.settings.require_trade_approval = False
    state = SimpleNamespace(calls=[], last=105.0, config=config)

    async def call_mcp(tool: str, args: dict):
        state.calls.append((tool, dict(args)))
        if tool == "place_equity_order":
            return {"data": {"order": {"id": f"0rd3r-{len(state.calls):04d}-abcd"}}}
        return None

    async def quote(ticker, now):
        return {"last": state.last}, []

    async def account():
        return "ACCT1"

    async def reconcile():
        return {}

    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_journal", TradeJournal(db))
    monkeypatch.setattr(tools, "_config", config)
    monkeypatch.setattr(tools, "_metrics", None)
    monkeypatch.setattr(tools, "_memory", None)
    monkeypatch.setattr(tools, "_sim_proxy", None)
    monkeypatch.setattr(tools, "_current_session_id", "s-entry")
    monkeypatch.setattr(tools, "_call_mcp_tool", call_mcp)
    monkeypatch.setattr(tools, "_fetch_quote", quote)
    monkeypatch.setattr(tools, "_agentic_account_number", account)
    monkeypatch.setattr(tools, "reconcile_pending_orders", reconcile)
    monkeypatch.setattr(tools, "_open_positions_cache", None)
    monkeypatch.setattr(protection_followup, "_cycle_running", lambda: False)
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda *a, **k: market_hours.MarketSession.REGULAR
    )
    return state


async def _fill(order_id: str = "e1n7ry-0001", price: float = 100.0) -> None:
    await tools._journal.update_order_status(order_id, "FILLED", fill_price=price)
    await tools._journal.resolve_pending_order(order_id, "FILLED")


async def _ended(order_id: str, status: str = "CANCELLED") -> None:
    """The broker ended a resting order, as the reconciliation records it."""
    await tools._journal.update_order_status(
        order_id, status, broker_status_reason="cancelled outside EvoTrader (made up)"
    )
    await tools._journal.resolve_pending_order(order_id, status)


async def _plans(db) -> list[dict]:
    async with db.connection() as conn:
        cursor = await conn.execute("SELECT * FROM protection_plans ORDER BY id")
        return [dict(r) for r in await cursor.fetchall()]


def _placed(state) -> list[dict]:
    return [args for tool, args in state.calls if tool == "place_equity_order"]


def _note(state) -> str:
    path = handoff_path(state.config.data_dir)
    return path.read_text() if path.exists() else ""


async def _protected(broker) -> tuple[str, str]:
    """An entry filled and its plan placed: the stop's and the take-profit's ids."""
    await tools.record_trade(_entry())
    await _fill()
    assert await protection_followup.followup_once() == 1
    (plan,) = await _plans(tools._db)
    assert plan["status"] == "watching"
    watched = json.loads(plan["watch"])["orders"]
    stop = next(o["order_id"] for o in watched if o["role"] == "stop")
    take = next(o["order_id"] for o in watched if o["role"] == "take_profit")
    return stop, take


class TestAStopThatEndsIsPutBack:
    async def test_once_at_its_level_and_size(self, broker, db) -> None:
        stop, _ = await _protected(broker)
        await _ended(stop)
        assert (await tools._protective_coverage())["XYZ"]["no_order_qty"] == 2
        assert await protection_followup.followup_once() == 1, "still watching"
        put = _placed(broker)[-1]
        assert put == {
            "symbol": "XYZ",
            "side": "sell",
            "type": "stop_market",
            "quantity": "2",
            "stop_price": "90.00",
            "time_in_force": "gtc",
            "account_number": "ACCT1",
        }
        assert (await tools._protective_coverage())["XYZ"]["no_order_qty"] == 0
        assert f"the stop 2 @ 90 ({stop[:8]}) for XYZ ended cancelled" in _note(broker)
        assert "put back stop 2 @ 90 gtc" in _note(broker)
        (plan,) = await _plans(db)
        watch = json.loads(plan["watch"])
        assert [p["after"] for p in watch["put_back"]] == [stop]
        rows = await tools._journal.get_recent_trades(limit=10)
        again = next(r for r in rows if r["order_id"] == watch["put_back"][0]["order_id"])
        assert (again["action"], again["related_trade_id"]) == ("STOP_LOSS", 1)
        assert again["reasoning"].startswith("SYSTEM: stop put back by the protection follow-up")

    async def test_never_twice(self, broker, db) -> None:
        stop, _ = await _protected(broker)
        await _ended(stop)
        await protection_followup.followup_once()
        (plan,) = await _plans(db)
        put_back = json.loads(plan["watch"])["put_back"][0]["order_id"]
        await _ended(put_back)
        placed = len(_placed(broker))
        await protection_followup.followup_once()
        assert len(_placed(broker)) == placed, "the stop it put back is not put back again"

    async def test_a_filled_stop_is_not_put_back(self, broker) -> None:
        stop, _ = await _protected(broker)
        await _ended(stop, "FILLED")
        placed = len(_placed(broker))
        await protection_followup.followup_once()
        assert len(_placed(broker)) == placed

    async def test_a_take_profit_that_ended_is_named_not_placed(self, broker, db) -> None:
        _, take = await _protected(broker)
        await _ended(take)
        placed = len(_placed(broker))
        await protection_followup.followup_once()
        assert len(_placed(broker)) == placed
        assert f"the take-profit 1 @ 108.0 ({take[:8]}) for XYZ ended cancelled" in _note(broker)
        assert "A take-profit is not put back" in _note(broker)
        (plan,) = await _plans(db)
        assert json.loads(plan["watch"])["reported"] == [take]

    async def test_a_stop_the_shares_already_had_is_watched_too(self, broker, db) -> None:
        await tools.record_trade(_entry())
        await _fill()
        await tools.record_trade(
            json.dumps(
                {
                    "ticker": "XYZ",
                    "action": "STOP_LOSS",
                    "direction": "LONG",
                    "quantity": 3,
                    "order_type": "stop_market",
                    "stop_price": 91.0,
                    "time_in_force": "gtc",
                    "order_id": "st0p-dddd-0001",
                    "status": "PENDING",
                    "related_trade_id": 1,
                    "reasoning": "made up",
                }
            )
        )
        await protection_followup.followup_once()
        (plan,) = await _plans(db)
        assert plan["status_reason"].startswith("covered:")
        await _ended("st0p-dddd-0001")
        await protection_followup.followup_once()
        put = _placed(broker)[-1]
        assert (put["quantity"], put["stop_price"]) == ("3", "91.00")


class TestWhatItWillNotDo:
    async def test_a_price_through_the_stop_is_refused_once(self, broker) -> None:
        stop, _ = await _protected(broker)
        await _ended(stop)
        broker.last = 89.0
        placed = len(_placed(broker))
        await protection_followup.followup_once()
        await protection_followup.followup_once()
        assert len(_placed(broker)) == placed
        note = _note(broker)
        assert "it is NOT put back: the price 89 is already through the stop 90" in note
        assert note.count("is NOT put back") == 1

    async def test_approval_of_every_order_is_not_given_for_it(self, broker) -> None:
        stop, _ = await _protected(broker)
        await _ended(stop)
        broker.config.settings.require_trade_approval = True
        placed = len(_placed(broker))
        await protection_followup.followup_once()
        assert len(_placed(broker)) == placed
        assert "asks for approval of every order" in _note(broker)

    async def test_nothing_outside_regular_hours(self, broker, monkeypatch) -> None:
        from evotrader.tools import market_hours

        stop, _ = await _protected(broker)
        await _ended(stop)
        monkeypatch.setattr(
            market_hours,
            "get_current_session",
            lambda *a, **k: market_hours.MarketSession.AFTER_HOURS,
        )
        placed = len(_placed(broker))
        assert await protection_followup.followup_once() == 1
        assert len(_placed(broker)) == placed

    async def test_two_plans_on_one_ticker_put_a_stop_back_once(self, broker, db) -> None:
        await tools.record_trade(_entry())
        await _fill()
        await tools.record_trade(
            _entry("e1n7ry-0002", quantity=2, plan={"stop_price": 90.0, "stop_qty": 2})
        )
        await _fill("e1n7ry-0002")
        await protection_followup.followup_once()
        assert [p["status"] for p in await _plans(db)] == ["watching", "watching"]
        stops = [a for a in _placed(broker) if a["type"] == "stop_market"]
        assert len(stops) == 2
        first_stop = json.loads((await _plans(db))[0]["watch"])["orders"][0]["order_id"]
        await _ended(first_stop)
        placed = len(_placed(broker))
        await protection_followup.followup_once()
        await protection_followup.followup_once()
        assert len(_placed(broker)) == placed + 1


class TestTheWindow:
    async def test_it_ends_done_when_the_window_closes(self, broker, db) -> None:
        await _protected(broker)
        (plan,) = await _plans(db)
        later = datetime.fromisoformat(plan["created_at"]) + timedelta(minutes=61)
        assert await protection_followup.followup_once(now=later) == 0
        (plan,) = await _plans(db)
        assert plan["status"] == "done"
        assert plan["status_reason"].startswith("placed: stop 2 @ 90.00")
        assert "watched: no protective order ended" in plan["status_reason"]
        assert "the window closed with coverage split" in plan["status_reason"]

    async def test_it_ends_done_when_the_shares_are_sold(self, broker, db) -> None:
        stop, take = await _protected(broker)
        await _ended(stop, "FILLED")
        await _ended(take, "FILLED")
        await tools.record_trade(
            json.dumps(
                {
                    "ticker": "XYZ",
                    "action": "CLOSE",
                    "direction": "LONG",
                    "quantity": 3,
                    "order_type": "market",
                    "price": 104.0,
                    "status": "FILLED",
                    "reasoning": "made up",
                }
            )
        )
        await protection_followup.followup_once(now=datetime.now(UTC))
        (plan,) = await _plans(db)
        assert plan["status"] == "done"
        assert plan["status_reason"].endswith("no XYZ shares are held")

    async def test_the_follow_up_starts_for_a_watched_plan(self, broker, monkeypatch) -> None:
        await _protected(broker)
        started = []

        async def fake_run(interval, minutes):
            started.append((interval, minutes))

        monkeypatch.setattr(protection_followup, "run_followup", fake_run)
        monkeypatch.setattr(protection_followup, "_task", None)
        assert await protection_followup.start_followup() is True
        await protection_followup._task
        assert started == [(300, 60)]
