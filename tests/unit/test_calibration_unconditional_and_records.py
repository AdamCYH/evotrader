"""Three things the evolution agent could not read from the record.

Found by the evolution agent's code review.

* A channel that only ever calls one side shows ``edge_over_baseline`` 0.0 by
  construction: the baseline is the majority direction of its own sample,
  which is its own call whenever it is right more than half the time. The
  score now carries the long/short split, the hit rate the same calls would
  score at the window's up-share, and how unlikely the actual hits are.
* A resting order did not say whether it works outside regular hours, so an
  after-the-close cycle could not count a take-profit as overnight cover.
* A cycle started by hand, and one whose start-up steps ran long, read the
  same as a scheduled cycle that ran late; and a minute the app was not
  running for left no trace.

Made-up tickers, numbers and times.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from evotrader.backtest.attribution import score_source
from evotrader.db.signal_attribution import SignalAttributionStore
from evotrader.tools.market_hours import ET
from evotrader.web.schedule_slot import CronScheduler, cycle_start_notes

DAYS = [datetime(2026, 3, 2, 15, 0, tzinfo=UTC) + timedelta(days=i) for i in range(10)]


def _brute_force_p(hits: int, rates: list[float]) -> float:
    """The same exact test, by enumerating every outcome."""
    dist = [0.0] * (len(rates) + 1)
    for outcome in product((0, 1), repeat=len(rates)):
        prob = math.prod(r if o else 1 - r for o, r in zip(outcome, rates, strict=True))
        dist[sum(outcome)] += prob
    return min(1.0, sum(q for q in dist if q <= dist[hits] * (1 + 1e-7)))


class TestTheUnconditionalExpectation:
    def test_a_one_sided_channel_reads_its_selection(self) -> None:
        """Ten long calls, eight right, while the window rose 45% of the time."""
        returns = [1.0] * 8 + [-1.0] * 2
        score = score_source("one_sided", [1.0] * 10, returns, DAYS, window_up_rate=0.45)
        assert score.edge_over_baseline == 0.0, "the old baseline is the sample's own majority"
        assert (score.n_long, score.n_short) == (10, 0)
        assert score.expected_hit_unconditional == pytest.approx(0.45)
        pmf = [math.comb(10, k) * 0.45**k * 0.55 ** (10 - k) for k in range(11)]
        exact = sum(q for q in pmf if q <= pmf[8] * (1 + 1e-7))  # 0, 1, 8, 9 and 10 hits
        assert score.binomial_p_unconditional == pytest.approx(exact, rel=1e-9)
        assert score.binomial_p_unconditional == pytest.approx(0.0507, abs=1e-4)

    def test_mixed_calls_use_each_calls_own_rate(self) -> None:
        directions = [1.0] * 6 + [-1.0] * 4
        returns = [1.0, 1.0, 1.0, -1.0, 1.0, -1.0, -1.0, -1.0, 1.0, -1.0]
        score = score_source("mixed", directions, returns, DAYS, window_up_rate=0.6)
        rates = [0.6] * 6 + [0.4] * 4
        hits = sum(1 for d, r in zip(directions, returns, strict=True) if d * r > 0)
        assert score.expected_hit_unconditional == pytest.approx(sum(rates) / 10)
        assert score.binomial_p_unconditional == pytest.approx(
            _brute_force_p(hits, rates), rel=1e-9
        )

    def test_without_a_window_up_share_nothing_is_claimed(self) -> None:
        score = score_source("plain", [1.0] * 5, [1.0] * 5, DAYS[:5])
        assert math.isnan(score.expected_hit_unconditional)
        assert math.isnan(score.binomial_p_unconditional)
        assert score.n_long == 5

    async def test_the_calibration_tool_reports_them(self, db, monkeypatch) -> None:
        import evotrader.evolution.tools as evo

        store = SignalAttributionStore(db)
        for i, up in enumerate([True, True, False, True, False, False, True, False, True, False]):
            rid = await store.record(
                "XYZ",
                algo_direction=1.0 if i < 7 else 0.0,
                price_at_decision=100.0,
                timestamp=(datetime.now(UTC) - timedelta(days=10 - i)).isoformat(),
            )
            await store.score(rid, forward_return_1d=1.0 if up else -1.0, forward_return_5d=None)
        monkeypatch.setattr(evo, "_attribution_store", store)
        out = await evo.get_signal_calibration(lookback_days=30)
        algo = next(w for w in out["witnesses"] if w["source"] == "algo")
        assert (algo["n_long"], algo["n_short"]) == (7, 0)
        assert algo["expected_hit_unconditional"] == pytest.approx(0.5)
        assert 0.0 < algo["binomial_p_unconditional"] <= 1.0
        assert "expected_hit_unconditional" in out["by_side"]["algo"]["long"]
        assert "own majority direction" in out["base_rate"]["note"]


class TestTheExtendedHoursFlag:
    def test_the_brokers_market_hours_reach_the_book(self) -> None:
        from evotrader.agents.tools import _pending_order_view, _pending_row_from_broker_order

        order = {
            "id": "0rd3r-1",
            "side": "sell",
            "type": "limit",
            "trigger": "immediate",
            "price": "120.00",
            "quantity": "2",
            "time_in_force": "gtc",
            "state": "confirmed",
        }
        for hours, flag in (("extended_hours", True), ("regular_hours", False), (None, None)):
            row = _pending_row_from_broker_order({**order, "market_hours": hours}, "XYZ")
            view = _pending_order_view({"order_id": "0rd3r-1", "trade_json": json.dumps(row)})
            assert view["extended_hours"] is flag

    def test_an_explicit_flag_counts(self) -> None:
        from evotrader.agents.tools import _pending_order_view

        row = {"ticker": "XYZ", "action": "TAKE_PROFIT", "extended_hours": True}
        view = _pending_order_view({"order_id": "j1", "trade_json": json.dumps(row)})
        assert view["extended_hours"] is True

    def test_the_coverage_block_lists_it(self) -> None:
        from evotrader.db.protection_audit import coverage_by_ticker

        lots = [
            {"id": 1, "ticker": "XYZ", "action": "OPEN", "quantity": 3, "remaining_quantity": 3}
        ]
        book = [
            {
                "order_id": "t1",
                "ticker": "XYZ",
                "action": "TAKE_PROFIT",
                "quantity": 3,
                "limit_price": 120.0,
                "time_in_force": "gtc",
                "extended_hours": True,
            }
        ]
        [order] = coverage_by_ticker(lots, book)["XYZ"]["orders"]
        assert order["extended_hours"] is True


STARTED = datetime(2026, 3, 4, 12, 30, 2, tzinfo=ET)


class TestHowACycleStarted:
    def test_a_scheduled_cycle_on_time_says_nothing(self) -> None:
        due = {"due_at": STARTED.replace(second=0), "late_reason": None}
        assert cycle_start_notes(due, "scheduled automated", STARTED, 4.0, 3.0) == []

    def test_a_late_one_says_why(self) -> None:
        due = {"due_at": STARTED.replace(minute=20, second=0), "late_reason": "waited"}
        [(line, meta)] = cycle_start_notes(due, "scheduled automated", STARTED, 4.0, 3.0)
        assert line == "[scheduler] scheduled cycle due 12:20 ET started 12:30 ET: waited"
        assert meta["late_reason"] == "waited"

    def test_one_started_by_hand_names_who(self) -> None:
        [(line, meta)] = cycle_start_notes(None, "user-triggered", STARTED, 4.0, 3.0)
        assert (
            line == "[scheduler] cycle started by user-triggered at 12:30 ET, not by the schedule"
        )
        assert meta["source"] == "user-triggered"

    def test_slow_start_up_steps_are_said(self) -> None:
        due = {"due_at": STARTED.replace(second=0), "late_reason": None}
        [(line, meta)] = cycle_start_notes(due, "scheduled automated", STARTED, 301.0, 300.0)
        assert "start-up steps took 301 s" in line and "300 s of it the order sync" in line
        assert meta["order_sync_seconds"] == 300.0

    def test_outside_the_console_only_the_start_up_time_is_said(self) -> None:
        assert cycle_start_notes(None, None, STARTED, 4.0, 3.0, console=False) == []
        notes = cycle_start_notes(None, None, STARTED, 90.0, 85.0, console=False)
        assert [line[:9] for line, _ in notes] == ["[runtime]"]


SCHEDULES = {"cycle": ["30 9-15 * * 1-5"], "evolution": [], "metrics": []}


def _et(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 3, 4, hour, minute, second, tzinfo=ET)


class TestTheMinuteBeforeAStart:
    def test_the_first_poll_names_it(self) -> None:
        scheduler = CronScheduler()
        assert scheduler.due_before_start(SCHEDULES, _et(11, 35, 10)) == [("cycle", _et(11, 30))]
        assert scheduler.due_before_start(SCHEDULES, _et(11, 55, 10)) == [], "past its grace"
        assert scheduler.due_before_start(SCHEDULES, _et(11, 30, 10)) == [], "its own minute"

    @pytest.fixture
    def console(self, db, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from evotrader.config import AppConfig
        from evotrader.web.server import create_app

        monkeypatch.setenv("DASHBOARD_PASSWORD", "test_password")
        config = AppConfig(data_dir=tmp_path / "data")
        config.settings.schedule.cycle_cron = SCHEDULES["cycle"]
        config.settings.schedule.evolution_cron = None
        config.settings.schedule.metrics_cron = None
        evolution = MagicMock()
        evolution.is_running = MagicMock(return_value=False)
        app = create_app(
            db=db,
            journal=MagicMock(),
            metrics=MagicMock(),
            mcp_toolset=MagicMock(),
            config=config,
            runner_fn=AsyncMock(),
            memory=MagicMock(),
            evolution_service=evolution,
            evolution_db=db,
        )
        app.state.cycle_cron_enabled = True
        return app

    async def _skipped(self, db) -> list[dict]:
        import asyncio

        for _ in range(20):
            await asyncio.sleep(0.01)
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT timestamp, summary FROM cycle_runs WHERE status = 'SKIPPED'"
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def test_with_no_run_on_record_it_is_a_skipped_run(self, console, db) -> None:
        await console.state.scheduler_poll(_et(11, 35, 10))
        [row] = await self._skipped(db)
        assert row["timestamp"] == _et(11, 30).astimezone(UTC).isoformat()
        assert "the app was not running at its minute" in row["summary"]

    async def test_a_run_the_previous_process_made_is_left_alone(self, console, db) -> None:
        await console.state.thought_logger.record_run_start("before-restart", "TRADING")
        async with db.connection() as conn:
            await conn.execute(
                "UPDATE cycle_runs SET timestamp = ? WHERE session_id = 'before-restart'",
                (_et(11, 30, 5).astimezone(UTC).isoformat(),),
            )
            await conn.commit()
        await console.state.scheduler_poll(_et(11, 35, 10))
        assert await self._skipped(db) == []
