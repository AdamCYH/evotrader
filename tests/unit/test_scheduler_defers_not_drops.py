"""A due cron trigger waits until it can start; it never silently vanishes.

Found by the evolution agent's code review. The scheduler marked a minute done
before checking whether another task was running, so a cycle due while the
evolution run was still running was logged "skipped" and never retried, and a
cycle and the evolution run due at the same minute both started, side by side;
and it matched only the current minute, so a minute the loop never saw (a
stalled loop, a host asleep across it) never fired at all. Now a due trigger
waits for the running task and starts inside a grace window, a missed minute
starts late, the cycle goes before the evolution run at a shared minute, and a
trigger that cannot start in time is recorded as a SKIPPED run.

Made-up dates. The cycles are the starter's; the evolution run is put on the
minute of the last Friday cycle, so the two meet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.cron import CronTrigger, crons_can_coincide
from evotrader.tools.market_hours import ET
from evotrader.web.schedule_slot import CronScheduler, ScheduleSlot

FRIDAY = date(2026, 3, 6)
CYCLES = ["30 9-15 * * 1-5"]
EVOLUTION = ["30 15 * * 5"]
SCHEDULES = {"cycle": CYCLES, "evolution": EVOLUTION, "metrics": []}


def _et(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(FRIDAY.year, FRIDAY.month, FRIDAY.day, hour, minute, second, tzinfo=ET)


def _triggers(exprs: list[str]) -> list[CronTrigger]:
    return [CronTrigger(e) for e in exprs]


class TestTheSlot:
    def test_a_due_minute_waits_until_it_is_taken(self) -> None:
        slot = ScheduleSlot("cycle", timedelta(minutes=20))
        slot.observe(_triggers(CYCLES), _et(15, 29), _et(15, 30))
        assert slot.due_at == _et(15, 30)
        assert slot.take(_et(15, 40)) == _et(15, 30)
        assert slot.due_at is None

    def test_a_minute_the_loop_never_saw_is_still_due(self) -> None:
        """Asleep from 11:29 to 11:33: the 11:30 minute was never polled."""
        slot = ScheduleSlot("cycle", timedelta(minutes=20))
        slot.observe(_triggers(CYCLES), _et(11, 29), _et(11, 33))
        assert slot.take(_et(11, 33)) == _et(11, 30)

    def test_after_a_long_gap_only_the_latest_minute_is_kept(self) -> None:
        """No burst: 15:30 starts; 14:30 and 13:30, past their grace, are reported."""
        slot = ScheduleSlot("cycle", timedelta(minutes=20))
        lost = slot.observe(_triggers(CYCLES), _et(13, 29), _et(15, 31))
        assert [minute for minute, _ in lost] == [_et(14, 30), _et(13, 30)]
        assert all("did not see its minute until 15:31" in why for _, why in lost)
        assert slot.take(_et(15, 31)) == _et(15, 30)
        assert slot.take(_et(15, 32)) is None

    def test_a_started_minute_is_not_started_twice(self) -> None:
        slot = ScheduleSlot("cycle", timedelta(minutes=20))
        slot.observe(_triggers(CYCLES), _et(9, 29), _et(9, 30))
        assert slot.take(_et(9, 30)) == _et(9, 30)
        slot.observe(_triggers(CYCLES), _et(9, 30), _et(9, 30))
        slot.observe(_triggers(CYCLES), None, _et(9, 30))
        assert slot.take(_et(9, 31)) is None

    def test_past_its_grace_it_expires(self) -> None:
        slot = ScheduleSlot("cycle", timedelta(minutes=20))
        slot.observe(_triggers(CYCLES), _et(12, 29), _et(12, 30))
        assert slot.expire(_et(12, 50)) is None
        assert slot.expire(_et(12, 51)) == _et(12, 30)
        assert slot.take(_et(12, 51)) is None

    def test_the_first_poll_does_not_reach_back(self) -> None:
        """Before the app started, whether a run happened is unknown."""
        slot = ScheduleSlot("cycle", timedelta(minutes=20))
        slot.observe(_triggers(CYCLES), None, _et(9, 33))
        assert slot.due_at is None


class TestTheScheduler:
    def test_at_a_shared_minute_the_cycle_starts_and_evolution_waits(self) -> None:
        scheduler = CronScheduler()
        scheduler.tick(_et(15, 29, 50), SCHEDULES, busy=False)
        tick = scheduler.tick(_et(15, 30, 5), SCHEDULES, busy=False)
        assert [(s.job, s.due_at, s.late_reason) for s in tick.start] == [
            ("cycle", _et(15, 30), None)
        ]
        assert tick.deferred == [("evolution", _et(15, 30))]
        # The cycle runs for a few minutes; the evolution run keeps waiting...
        assert scheduler.tick(_et(15, 33), SCHEDULES, busy=True).deferred == [
            ("evolution", _et(15, 30))
        ]
        # ...and starts once it is done, late, saying why.
        (start,) = scheduler.tick(_et(15, 35, 5), SCHEDULES, busy=False).start
        assert (start.job, start.due_at) == ("evolution", _et(15, 30))
        assert start.late_reason == "waited for another task to finish"

    def test_a_cycle_due_while_evolution_runs_starts_when_it_ends(self) -> None:
        scheduler = CronScheduler()
        scheduler.tick(_et(13, 29, 55), SCHEDULES, busy=True)
        assert scheduler.tick(_et(13, 30, 5), SCHEDULES, busy=True).deferred == [
            ("cycle", _et(13, 30))
        ]
        (start,) = scheduler.tick(_et(13, 38), SCHEDULES, busy=False).start
        assert (start.job, start.due_at) == ("cycle", _et(13, 30))
        assert start.late_reason == "waited for another task to finish"

    def test_a_cycle_the_loop_slept_through_starts_late(self) -> None:
        scheduler = CronScheduler()
        scheduler.tick(_et(11, 29, 50), SCHEDULES, busy=False)
        (start,) = scheduler.tick(_et(11, 33, 12), SCHEDULES, busy=False).start
        assert (start.job, start.due_at) == ("cycle", _et(11, 30))
        assert "did not see its minute" in start.late_reason

    def test_a_minute_slept_through_past_its_grace_is_reported(self) -> None:
        scheduler = CronScheduler()
        scheduler.tick(_et(10, 29, 50), SCHEDULES, busy=False)
        tick = scheduler.tick(_et(10, 51, 5), SCHEDULES, busy=False)
        assert tick.start == []
        (skip,) = tick.skipped
        assert (skip.job, skip.due_at) == ("cycle", _et(10, 30))
        assert "the app was stalled or asleep" in skip.reason

    def test_one_that_cannot_start_in_its_grace_is_reported(self) -> None:
        scheduler = CronScheduler()
        scheduler.tick(_et(10, 30), {"cycle": CYCLES}, busy=True)
        assert scheduler.tick(_et(10, 50), {"cycle": CYCLES}, busy=True).skipped == []
        (skip,) = scheduler.tick(_et(10, 51), {"cycle": CYCLES}, busy=True).skipped
        assert (skip.job, skip.due_at) == ("cycle", _et(10, 30))
        assert "another task was still running" in skip.reason

    def test_a_trigger_still_waiting_when_the_next_comes_due_is_reported(self) -> None:
        scheduler = CronScheduler(cycle_grace=timedelta(minutes=30))
        every_ten = {"cycle": ["*/10 * * * *"]}
        scheduler.tick(_et(10, 0), every_ten, busy=True)
        tick = scheduler.tick(_et(10, 10), every_ten, busy=True)
        assert [(s.job, s.due_at) for s in tick.skipped] == [("cycle", _et(10, 0))]
        assert tick.deferred == [("cycle", _et(10, 10))]

    def test_switching_a_schedule_off_forgets_its_pending_trigger(self) -> None:
        scheduler = CronScheduler()
        scheduler.tick(_et(15, 30), SCHEDULES, busy=True)
        scheduler.tick(_et(15, 32), {"cycle": [], "evolution": EVOLUTION}, busy=False)
        assert scheduler.slots["cycle"].due_at is None

    def test_the_metrics_job_does_not_wait_for_the_agents(self) -> None:
        scheduler = CronScheduler()
        schedules = {"metrics": ["0 16 * * *"]}
        scheduler.tick(_et(15, 59, 55), schedules, busy=True)
        (start,) = scheduler.tick(_et(16, 0, 5), schedules, busy=True).start
        assert (start.job, start.due_at) == ("metrics", _et(16, 0))

    def test_a_bad_expression_is_ignored_and_said(self, caplog) -> None:
        scheduler = CronScheduler()
        with caplog.at_level(logging.ERROR):
            tick = scheduler.tick(_et(9, 30), {"cycle": ["not a cron", *CYCLES]}, busy=False)
        assert "not a cron" in caplog.text
        assert [(s.job, s.due_at) for s in tick.start] == [("cycle", _et(9, 30))], (
            "the rest still fire"
        )


@pytest.fixture
def console(db, tmp_path, monkeypatch):
    """The console with a stand-in cycle and evolution run, on a real journal."""
    from evotrader.config import AppConfig
    from evotrader.web.server import create_app

    monkeypatch.setenv("DASHBOARD_PASSWORD", "test_password")
    config = AppConfig(data_dir=tmp_path / "data")
    config.settings.schedule.cycle_cron = CYCLES
    config.settings.schedule.evolution_cron = EVOLUTION[0]
    config.settings.schedule.metrics_cron = None
    evolution = MagicMock()
    evolution.is_running = MagicMock(return_value=False)
    evolution.trigger = AsyncMock()
    cycle = AsyncMock()
    app = create_app(
        db=db,
        journal=MagicMock(),
        metrics=MagicMock(),
        mcp_toolset=MagicMock(),
        config=config,
        runner_fn=cycle,
        memory=MagicMock(),
        evolution_service=evolution,
        evolution_db=db,
    )
    app.state.cycle_cron_enabled = True
    app.state.evolution_cron_enabled = True
    return app, cycle, evolution


async def _settle() -> None:
    """Let the spawned tasks finish (the journal writes in a worker thread)."""
    for _ in range(20):
        await asyncio.sleep(0.01)


class TestTheConsole:
    async def test_the_cycle_is_claimed_before_anything_else_can_start(self, console) -> None:
        app, cycle, evolution = console
        await app.state.scheduler_poll(_et(15, 29, 55))
        await app.state.scheduler_poll(_et(15, 30, 5))
        assert app.state.is_running is True, "claimed in the poll, before the task runs"
        evolution.trigger.assert_not_awaited()
        await _settle()
        cycle.assert_awaited_once()
        assert app.state.is_running is False
        await app.state.scheduler_poll(_et(15, 35, 5))
        await _settle()
        evolution.trigger.assert_awaited_once()

    async def test_a_late_cycle_tells_itself_why(self, console) -> None:
        app, cycle, _ = console
        await app.state.scheduler_poll(_et(11, 29, 50))
        seen = {}
        cycle.side_effect = lambda: seen.update(app.state.cycle_due or {})
        await app.state.scheduler_poll(_et(11, 33, 12))
        await _settle()
        assert seen["due_at"] == _et(11, 30)
        assert "did not see its minute" in seen["late_reason"]

    async def test_a_cycle_that_never_got_to_start_is_in_the_run_history(self, console, db) -> None:
        app, cycle, evolution = console
        evolution.is_running.return_value = True  # a long evolution run
        await app.state.scheduler_poll(_et(13, 29, 55))
        for minute in (30, 40, 50, 51):  # 20 minutes of grace, then reported
            await app.state.scheduler_poll(_et(13, minute, 5))
        await _settle()
        cycle.assert_not_awaited()
        async with db.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT session_id, timestamp, cycle_type, status, summary FROM cycle_runs"
                )
            ).fetchone()
        assert (row["cycle_type"], row["status"]) == ("TRADING", "SKIPPED")
        assert row["timestamp"] == _et(13, 30).astimezone(UTC).isoformat()
        assert "another task was still running" in row["summary"]
        listed = await app.state.thought_logger.get_unique_sessions(limit=5)
        entry = next(c for c in listed if c["session_id"] == row["session_id"])
        assert entry["cycle_status"] == "skipped"

    async def test_a_cycle_slept_through_is_in_the_run_history_too(self, console, db) -> None:
        app, cycle, _ = console
        await app.state.scheduler_poll(_et(12, 29, 55))
        await app.state.scheduler_poll(_et(12, 52, 5))  # asleep across 12:30 and its grace
        await _settle()
        cycle.assert_not_awaited()
        async with db.connection() as conn:
            row = await (await conn.execute("SELECT status, summary FROM cycle_runs")).fetchone()
        assert row["status"] == "SKIPPED"
        assert "did not see its minute until 12:52" in row["summary"]

    async def test_a_missed_metrics_job_is_said_but_is_not_a_run(self, console, db, caplog) -> None:
        app, _, _ = console
        app.state.config.settings.schedule.metrics_cron = "0 16 * * *"
        await app.state.scheduler_poll(_et(15, 59, 55))
        with caplog.at_level(logging.WARNING):
            await app.state.scheduler_poll(_et(22, 5, 5))  # asleep past its 6-hour grace
            await _settle()
        assert "Cron metrics due 16:00 ET skipped" in caplog.text
        async with db.connection() as conn:
            assert await (await conn.execute("SELECT * FROM cycle_runs")).fetchall() == []


class TestTheRecord:
    def test_the_table_takes_every_status_the_code_writes(self, tmp_path: Path) -> None:
        """0027 rebuilds cycle_runs; every row comes across unchanged."""
        migrations = Path(__file__).resolve().parents[2] / "src/evotrader/db/migrations"
        conn = sqlite3.connect(tmp_path / "old.db")
        conn.executescript((migrations / "0001_initial.sql").read_text())
        conn.execute(
            "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status, summary) "
            "VALUES ('s1', '2026-03-06T14:30:00+00:00', 'TRADING', 'SUCCESS', 'fine')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) "
                "VALUES ('s2', 'x', 'EVOLUTION', 'TIMED_OUT')"
            )
        conn.commit()
        for statement in (migrations / "0027_cycle_runs_statuses.sql").read_text().split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.commit()
        assert conn.execute("SELECT session_id, status, summary FROM cycle_runs").fetchall() == [
            ("s1", "SUCCESS", "fine")
        ]
        for i, status in enumerate(("SKIPPED", "CANCELLED", "TIMED_OUT")):
            conn.execute(
                "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) "
                f"VALUES ('n{i}', 'x', 'EVOLUTION', '{status}')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) "
                "VALUES ('bad', 'x', 'TRADING', 'WHATEVER')"
            )
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(cycle_runs)")}
        assert {"idx_cycle_runs_session", "idx_cycle_runs_ts", "idx_cycle_runs_type_ts"} <= indexes

    async def test_an_evolution_timeout_is_recorded_now(self, db) -> None:
        from evotrader.db.thought_log import ThoughtLogger

        logger = ThoughtLogger(db)
        await logger.record_run_start("evo-1", "EVOLUTION")
        await logger.record_run_completion("evo-1", "TIMED_OUT", error="timed out")
        async with db.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM cycle_runs WHERE session_id='evo-1'")
            ).fetchone()
        assert row["status"] == "TIMED_OUT"

    async def test_a_skipped_run_reads_with_its_cause(self, db) -> None:
        from evotrader.db.thought_log import ThoughtLogger

        logger = ThoughtLogger(db)
        session = await logger.record_skipped_run("EVOLUTION", _et(15, 30), "the app was asleep")
        assert session == "skipped-evolution-20260306T2030Z"
        assert session == await logger.record_skipped_run("EVOLUTION", _et(15, 30), "again")
        events = await logger.get_session_events(session)
        assert events[0]["event_type"] == "runtime"
        assert "did not start: the app was asleep" in events[0]["content"]
        meta = events[0]["meta"]
        meta = json.loads(meta) if isinstance(meta, str) else meta
        assert meta["status"] == "SKIPPED"


class TestTheWarning:
    def test_a_shared_minute_is_found(self) -> None:
        assert crons_can_coincide("30 9-15 * * 1-5", "30 15 * * 5")
        assert not crons_can_coincide("30 9-15 * * 1-5", "45 15 * * 5")
        assert not crons_can_coincide("30 9-15 * * 1-5", "0 17 * * 5"), "the starter's"
        assert not crons_can_coincide("30 9-15 * * 1-5", "not a cron")

    def test_the_settings_say_so(self, caplog) -> None:
        from evotrader.models.config import ScheduleConfig

        with caplog.at_level(logging.WARNING):
            ScheduleConfig(cycle_cron=CYCLES, evolution_cron="30 15 * * 5")
        assert "can fall on the same minute" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            ScheduleConfig(cycle_cron=CYCLES, evolution_cron="45 15 * * 5")
        assert "same minute" not in caplog.text
