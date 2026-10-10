"""A filled entry's approved protection is placed when the fill lands after its cycle.

Found by the evolution agent's code review. An entry accepted but not yet
filled when the executor's turn ended left its approved stop and take-profit
in prose only; it filled minutes later and the new shares sat unprotected
until the next cycle, an hour on. ``record_trade`` now keeps an entry's
``protection_plan`` as data, and the follow-up places it once the entry fills:
only for shares still under no order, only by adding orders, and only past the
same checks an agent's order meets. A fill before the open, when a stop cannot
rest, is protected as the first regular-hours cycle starts, before its agents.

Made-up tickers, prices and order ids; the broker is a stand-in.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evotrader.agents import tools
from evotrader.config import AppConfig
from evotrader.db.journal import TradeJournal
from evotrader.db.protection_plans import ProtectionPlanStore, plan_problems
from evotrader.models.config import TradingMode
from evotrader.tools import protection_followup
from evotrader.tools.trading_handoff import handoff_path

PLAN = {"stop_price": 90.0, "stop_qty": 2, "tp_limit_price": 108.0, "tp_qty": 1}


def _entry(order_id: str = "e1n7ry-0001", plan: dict | None = PLAN, **extra) -> str:
    data = {
        "ticker": "XYZ",
        "action": "OPEN",
        "direction": "LONG",
        "quantity": 3,
        "order_type": "limit",
        "limit_price": 100.0,
        "time_in_force": "gfd",
        "order_id": order_id,
        "status": "PENDING",
        "algo_signal": 0.1,
        "hybrid_score": 0.1,
        "llm_signal": 0.0,
        "confidence": 0.3,
        "regime": "trending_bull",
        "algo_version": "v001",
        "reasoning": "made up",
        **extra,
    }
    if plan is not None:
        data["protection_plan"] = plan
    return json.dumps(data)


def _protective(action: str, quantity: float, order_id: str, **levels) -> str:
    return json.dumps(
        {
            "ticker": "XYZ",
            "action": action,
            "direction": "LONG",
            "quantity": quantity,
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


@pytest.fixture
def broker(db, tmp_path, monkeypatch):
    """The tools bound to a real journal, with a stand-in broker at 105."""
    from evotrader.tools import market_hours

    config = AppConfig(data_dir=tmp_path / "data")
    config.constitution.risk_limits.max_stop_loss_pct = 0.20
    config.constitution.trading_rules.allowed_tickers = ["XYZ"]
    config.settings.protection_followup.enabled = True
    config.settings.mode = TradingMode.LIVE  # dry_run follows the mode
    config.settings.require_trade_approval = False
    state = SimpleNamespace(calls=[], last=105.0, accept=True)

    async def call_mcp(tool: str, args: dict):
        state.calls.append((tool, dict(args)))
        if tool == "place_equity_order" and state.accept:
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
    monkeypatch.setattr(
        market_hours, "get_current_session", lambda *a, **k: market_hours.MarketSession.REGULAR
    )
    state.config = config
    return state


async def _fill(order_id: str = "e1n7ry-0001", price: float = 100.0) -> None:
    await tools._journal.update_order_status(order_id, "FILLED", fill_price=price)
    await tools._journal.resolve_pending_order(order_id, "FILLED")


async def _plans(db) -> list[dict]:
    async with db.connection() as conn:
        cursor = await conn.execute("SELECT * FROM protection_plans ORDER BY id")
        return [dict(r) for r in await cursor.fetchall()]


def _placed(state) -> list[dict]:
    return [args for tool, args in state.calls if tool == "place_equity_order"]


class TestThePlanTravelsWithTheEntry:
    async def test_it_is_kept(self, broker, db) -> None:
        out = await tools.record_trade(_entry())
        assert out["plan_recorded"] is True
        (plan,) = await _plans(db)
        assert (plan["ticker"], plan["stop_price"], plan["stop_qty"]) == ("XYZ", 90.0, 2.0)
        assert (plan["tp_limit_price"], plan["tp_qty"], plan["status"]) == (108.0, 1.0, "planned")
        assert plan["entry_trade_id"] == out["trade_ids"][0]
        assert plan["session_id"] == "s-entry"

    async def test_a_bad_plan_is_reported_and_the_trade_kept(self, broker, db) -> None:
        out = await tools.record_trade(_entry(plan={**PLAN, "stop_price": 101.0}))
        assert out["trade_ids"] and out["plan_recorded"] is False
        assert "is not below the entry price 100" in out["plan_problems"][0]
        assert await _plans(db) == []

    @pytest.mark.parametrize(
        ("plan", "problem"),
        [
            ({"stop_qty": 3}, "stop_price is required"),
            ({**PLAN, "stop_qty": 3}, "more than the entry's 3 shares"),
            ({**PLAN, "tp_limit_price": None}, "tp_limit_price is required"),
            ({**PLAN, "time_in_force": "gfd"}, "must be gtc"),
            ({**PLAN, "tp_limit_price": 99.0}, "is not above the entry price"),
            ("stop at 90", "must be an object"),
        ],
    )
    def test_what_a_plan_needs(self, plan, problem) -> None:
        assert any(problem in p for p in plan_problems(plan, "LONG", 3, 100.0))

    def test_a_good_plan(self) -> None:
        assert plan_problems(PLAN, "LONG", 3, 100.0) == []
        assert plan_problems({"stop_price": 90, "stop_qty": 3}, "LONG", 3, None) == []


class TestTheFollowUp:
    async def test_nothing_before_the_fill(self, broker) -> None:
        await tools.record_trade(_entry())
        assert await protection_followup.followup_once() == 1
        assert _placed(broker) == []

    async def test_after_the_fill_the_new_shares_are_protected(self, broker, db) -> None:
        out = await tools.record_trade(_entry())
        await _fill()
        assert await protection_followup.followup_once() == 0
        stop, take = _placed(broker)
        assert stop == {
            "symbol": "XYZ",
            "side": "sell",
            "type": "stop_market",
            "quantity": "2",
            "stop_price": "90.00",
            "time_in_force": "gtc",
            "account_number": "ACCT1",
        }
        assert (take["type"], take["quantity"], take["limit_price"]) == ("limit", "1", "108.00")
        rows = await tools._journal.get_recent_trades(limit=5)
        protective = {r["action"]: r for r in rows if r["action"] in ("STOP_LOSS", "TAKE_PROFIT")}
        assert protective["STOP_LOSS"]["related_trade_id"] == out["trade_ids"][0]
        assert protective["STOP_LOSS"]["session_id"] == "s-entry"
        assert protective["TAKE_PROFIT"]["quantity"] == 1.0
        (plan,) = await _plans(db)
        assert plan["status"] == "placed"
        note = handoff_path(broker.config.data_dir).read_text()
        assert "protected the fill of e1n7ry-0" in note and "stop 2 @ 90.00" in note
        coverage = (await tools._protective_coverage())["XYZ"]
        assert coverage["no_order_qty"] == 0

    async def test_shares_already_protected_are_left_alone(self, broker, db) -> None:
        await tools.record_trade(_entry())
        await _fill()
        await tools.record_trade(_protective("STOP_LOSS", 3, "st0p-0001", stop_price=90.0))
        assert await protection_followup.followup_once() == 0
        assert _placed(broker) == []
        assert (await _plans(db))[0]["status"] == "covered"

    async def test_only_the_unprotected_shares_and_the_stop_first(self, broker, db) -> None:
        await tools.record_trade(_entry())
        await _fill()
        await tools.record_trade(_protective("STOP_LOSS", 1, "st0p-0001", stop_price=90.0))
        await protection_followup.followup_once()
        (stop,) = _placed(broker)
        assert (stop["type"], stop["quantity"]) == ("stop_market", "2")
        assert "only 2 shares were unprotected" in (await _plans(db))[0]["status_reason"]

    async def test_a_price_through_the_stop_is_refused(self, broker, db) -> None:
        broker.last = 89.5
        await tools.record_trade(_entry())
        await _fill()
        await protection_followup.followup_once()
        assert _placed(broker) == []
        (plan,) = await _plans(db)
        assert plan["status"] == "refused" and "already through the stop" in plan["status_reason"]
        assert "the next cycle decides" in handoff_path(broker.config.data_dir).read_text()

    async def test_a_stop_past_the_cap_is_refused(self, broker, db) -> None:
        await tools.record_trade(_entry(plan={**PLAN, "stop_price": 75.0}))
        await _fill()
        await protection_followup.followup_once()
        assert _placed(broker) == []
        assert "max_stop_loss_pct is 20%" in (await _plans(db))[0]["status_reason"]

    async def test_outside_regular_hours_it_waits(self, broker, monkeypatch) -> None:
        from evotrader.tools import market_hours

        monkeypatch.setattr(
            market_hours,
            "get_current_session",
            lambda *a, **k: market_hours.MarketSession.PRE_MARKET,
        )
        await tools.record_trade(_entry())
        await _fill()
        assert await protection_followup.followup_once() == 1
        assert _placed(broker) == []

    async def test_an_entry_that_never_filled_ends_its_plan(self, broker, db) -> None:
        await tools.record_trade(_entry())
        await tools._journal.update_order_status("e1n7ry-0001", "CANCELLED")
        assert await protection_followup.followup_once() == 0
        (plan,) = await _plans(db)
        assert plan["status"] == "expired" and "CANCELLED" in plan["status_reason"]

    async def test_a_plan_too_old_to_act_on_ends(self, broker, db) -> None:
        await tools.record_trade(_entry())
        later = datetime.now(UTC) + timedelta(hours=3)
        assert await protection_followup.followup_once(now=later) == 0
        assert (await _plans(db))[0]["status"] == "expired"

    async def test_when_every_order_needs_approval_it_does_not_place(self, broker, db) -> None:
        broker.config.settings.require_trade_approval = True
        await tools.record_trade(_entry())
        await _fill()
        await protection_followup.followup_once()
        assert _placed(broker) == []
        assert "approval" in (await _plans(db))[0]["status_reason"]

    async def test_a_broker_refusal_is_said(self, broker, db) -> None:
        broker.accept = False
        await tools.record_trade(_entry())
        await _fill()
        await protection_followup.followup_once()
        (plan,) = await _plans(db)
        assert plan["status"] == "failed" and "did not accept the stop" in plan["status_reason"]
        assert "did not accept the stop" in handoff_path(broker.config.data_dir).read_text()

    async def test_a_cycle_starting_meanwhile_gets_the_book(self, broker, db, monkeypatch) -> None:
        await tools.record_trade(_entry())
        await _fill()
        monkeypatch.setattr(protection_followup, "_cycle_running", lambda: True)
        assert await protection_followup.followup_once() == 1
        assert _placed(broker) == []
        assert (await _plans(db))[0]["status"] == "planned"


class TestAtTheStartOfACycle:
    async def test_a_fill_before_the_open_is_protected_as_the_first_cycle_starts(
        self, broker, db, monkeypatch
    ) -> None:
        from evotrader.tools import market_hours

        def session(name):
            return lambda *a, **k: getattr(market_hours.MarketSession, name)

        monkeypatch.setattr(market_hours, "get_current_session", session("PRE_MARKET"))
        await tools.record_trade(_entry())
        await _fill()
        assert await protection_followup.followup_once() == 1, "a stop cannot rest yet"
        monkeypatch.setattr(market_hours, "get_current_session", session("REGULAR"))
        monkeypatch.setattr(protection_followup, "_cycle_running", lambda: True)  # its own
        assert await protection_followup.protect_at_cycle_start() == 0
        stop, take = _placed(broker)
        assert (stop["type"], stop["quantity"], take["quantity"]) == ("stop_market", "2", "1")
        assert (await _plans(db))[0]["status"] == "placed"

    async def test_the_cycle_has_just_synced_the_orders(self, broker, monkeypatch) -> None:
        synced = []

        async def reconcile():
            synced.append(1)
            return {}

        monkeypatch.setattr(tools, "reconcile_pending_orders", reconcile)
        await tools.record_trade(_entry())
        await protection_followup.protect_at_cycle_start()
        assert synced == []
        await protection_followup.followup_once()
        assert synced == [1]

    async def test_off_means_off_here_too(self, broker) -> None:
        broker.config.settings.protection_followup.enabled = False
        await tools.record_trade(_entry())
        await _fill()
        assert await protection_followup.protect_at_cycle_start() == 0
        assert _placed(broker) == []

    async def test_the_follow_up_and_a_cycle_start_take_turns(
        self, broker, db, monkeypatch
    ) -> None:
        """Both see the same filled entry at once: its orders are placed once."""
        await tools.record_trade(_entry())
        await _fill()
        stand_in = tools._call_mcp_tool

        async def slow_broker(tool, args):
            await asyncio.sleep(0.01)
            return await stand_in(tool, args)

        monkeypatch.setattr(tools, "_call_mcp_tool", slow_broker)
        await asyncio.gather(
            protection_followup.followup_once(), protection_followup.protect_at_cycle_start()
        )
        assert [a["type"] for a in _placed(broker)] == ["stop_market", "limit"]
        assert [p["status"] for p in await _plans(db)] == ["placed"]


class TestWhenItRuns:
    async def test_off_by_default(self, broker) -> None:
        broker.config.settings.protection_followup.enabled = False
        await tools.record_trade(_entry())
        assert await protection_followup.start_followup() is False

    async def test_a_dry_run_places_nothing(self, broker) -> None:
        broker.config.settings.mode = TradingMode.SIM  # practice mode with no practice broker
        await tools.record_trade(_entry())
        assert await protection_followup.start_followup() is False

    async def test_nothing_waiting_nothing_started(self, broker) -> None:
        assert await protection_followup.start_followup() is False

    async def test_it_starts_for_a_waiting_plan(self, broker, monkeypatch) -> None:
        started = []

        async def fake_run(interval, minutes):
            started.append((interval, minutes))

        monkeypatch.setattr(protection_followup, "run_followup", fake_run)
        monkeypatch.setattr(protection_followup, "_task", None)
        await tools.record_trade(_entry())
        assert await protection_followup.start_followup() is True
        await protection_followup._task
        assert started == [(300, 60)]

    async def test_a_running_cycle_is_left_alone(self, broker, monkeypatch) -> None:
        checks = []

        async def fake_once(now=None):
            checks.append(now)
            return 0

        monkeypatch.setattr(protection_followup, "_cycle_running", lambda: True)
        monkeypatch.setattr(protection_followup, "followup_once", fake_once)
        await protection_followup.run_followup(interval_seconds=0, max_minutes=0.001)
        assert checks == []

    async def test_the_plan_store_keeps_one_plan_per_entry(self, broker, db) -> None:
        await tools.record_trade(_entry())
        store = ProtectionPlanStore(db)
        await store.save(
            entry_order_id="e1n7ry-0001",
            entry_trade_id=1,
            session_id="s",
            ticker="xyz",
            direction="long",
            entry_quantity=3,
            plan={**PLAN, "stop_price": 91.0},
        )
        statuses = [(p["stop_price"], p["status"]) for p in await _plans(db)]
        assert statuses == [(90.0, "expired"), (91.0, "planned")]
