"""A cycle's run record says when its strategy made no decision, and never stays open.

* A cycle whose strategy stage was reached but made no decision (its plan
  spent, its API fallback stopped or failed) read SUCCESS: reaching the stage
  was all the status asked.
* With ``quota.on_exhausted`` 'skip' or 'fail', a spent plan ran the whole
  pipeline anyway, the news agent and the orchestrator billed per call, for a
  strategy stage that could only answer "no decision".
* A process that stopped mid-cycle left the run RUNNING for good, and a
  cancelled cycle read FAILED like a crash.

Made-up sessions and times.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

import pytest

from evotrader.db.thought_log import ThoughtLogger
from evotrader.main import _DB_STATUS, _cycle_status

_REACHED = {"gather_market_data", "strategy"}


@pytest.mark.parametrize(
    ("outcome", "no_agents", "expected"),
    [
        ({"decided": True, "via": "harness"}, None, ("complete", None)),
        ({"decided": True, "via": "api_fallback"}, None, ("complete", None)),
        (None, None, ("complete", None)),
        (
            {"decided": False, "reason": "the API fallback made no decision: it failed"},
            None,
            (
                "error",
                "the strategy agent made no decision: the API fallback made no decision: it failed",
            ),
        ),
        (
            {"decided": False, "reason": "subscription quota exhausted", "on_exhausted": "skip"},
            None,
            ("skipped", "the strategy agent made no decision: subscription quota exhausted"),
        ),
        (
            {"decided": False, "reason": "subscription quota exhausted", "on_exhausted": "fail"},
            None,
            ("error", "the strategy agent made no decision: subscription quota exhausted"),
        ),
    ],
)
def test_a_strategy_stage_without_a_decision_is_not_a_success(outcome, no_agents, expected):
    assert _cycle_status(_REACHED, None, outcome, no_agents) == expected


def test_a_cycle_without_agents_reads_skipped_or_failed_by_the_setting():
    why = "strategy agent out of subscription quota (...): no agent ran this cycle"
    stages = {"gather_market_data"}
    assert _cycle_status(stages, why, None, "skip") == ("skipped", why)
    assert _cycle_status(stages, why, None, "fail") == ("error", why)
    assert _DB_STATUS.get("skipped") == "SKIPPED"
    assert _DB_STATUS.get("error", "FAILED") == "FAILED"


def test_the_cycle_error_comes_first():
    outcome = {"decided": False, "reason": "x"}
    assert _cycle_status(_REACHED, "boom", outcome, None) == ("error", "boom")


async def _run(db, session_id: str, ts: str, status: str, kind: str = "TRADING") -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) VALUES (?,?,?,?)",
            (session_id, ts, kind, status),
        )


async def _status(db, session_id: str) -> tuple[str, str | None]:
    async with db.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status, error FROM cycle_runs WHERE session_id = ?", (session_id,)
            )
        ).fetchone()
    return row["status"], row["error"]


async def test_runs_left_open_are_closed_at_start(db):
    log = ThoughtLogger(db)
    await _run(db, "open-cycle", "2026-01-06T17:30:02+00:00", "RUNNING")
    await _run(db, "open-review", "2026-01-06T22:00:00+00:00", "RUNNING", kind="EVOLUTION")
    await _run(db, "done-cycle", "2026-01-06T16:30:10+00:00", "SUCCESS")
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO agent_thought_log (timestamp, session_id, agent_name, event_type, content) "
            "VALUES (?,?,?,?,?)",
            ("2026-01-06T17:41:20+00:00", "open-cycle", "strategy", "thought", "x"),
        )

    closed = await log.close_open_runs()

    assert [r["session_id"] for r in closed] == ["open-cycle", "open-review"]
    status, error = await _status(db, "open-cycle")
    assert status == "FAILED"
    assert error == (
        "The app stopped while this run was open (started 2026-01-06 12:30 ET; its last "
        "recorded event was at 2026-01-06 12:41 ET). Closed when the app started again."
    )
    status, error = await _status(db, "open-review")
    assert status == "FAILED" and "nothing was recorded in it" in error
    assert await _status(db, "done-cycle") == ("SUCCESS", None)
    events = await log.get_session_events("open-cycle")
    assert events[-1]["event_type"] == "runtime"
    assert events[-1]["content"].startswith("[runtime] The app stopped while this run was open")
    assert await log.close_open_runs() == [], "nothing left open"


async def test_a_cycle_that_started_counts_as_a_run_though_skipped(db):
    log = ThoughtLogger(db)
    since = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)
    await log.record_skipped_run(
        "TRADING", datetime(2026, 1, 6, 14, 30, tzinfo=UTC), "the app was not running"
    )
    assert await log.has_run_since("TRADING", since) is False, "never started"

    await _run(db, "a1b2c3d4", "2026-01-06T15:30:05+00:00", "SKIPPED")
    assert await log.has_run_since("TRADING", since) is True, "it started; no agent ran"


async def test_a_cycle_without_agents_keeps_the_algorithms_call(db):
    from evotrader.agents.quota_cycle import record_cycle_without_agents

    log = ThoughtLogger(db)
    calls: list[str] = []

    async def gather(ticker: str) -> dict:
        calls.append(ticker)
        return {
            "ticker": ticker,
            "close": 101.5,
            "indicators": {"rsi_14": 41.0, "atr_14": 3.2},
            "composite_signal": -0.21,
            "regime": "trending_bear",
            "algo_version": "composite_v001",
            "sub_signals": [{"name": "momentum", "value": -0.4, "weight": 0.5}],
        }

    error = await record_cycle_without_agents(
        log,
        "s-quota",
        "XYZ",
        "quota exhausted on the five_hour window, until 10:05 ET",
        "skip",
        gather,
    )

    assert calls == ["XYZ"]
    assert error == (
        "strategy agent out of subscription quota (quota exhausted on the five_hour window, "
        "until 10:05 ET); quota.on_exhausted='skip': no agent ran this cycle"
    )
    events = await log.get_session_events("s-quota")
    assert [e["event_type"] for e in events] == ["runtime", "tool_call", "tool_response"]
    assert events[0]["content"].startswith(f"[runtime] {error}. The book was reconciled")
    assert events[1]["content"] == events[2]["content"] == "gather_market_data"
    async with db.connection() as conn:
        snap = await (
            await conn.execute(
                "SELECT ticker, composite_signal FROM market_snapshots WHERE session_id = ?",
                ("s-quota",),
            )
        ).fetchone()
    assert (snap["ticker"], snap["composite_signal"]) == ("XYZ", -0.21)


class TestTheCycleWiring:
    """The run-cycle task is one closure in main.start; its wiring is pinned here."""

    def _src(self) -> str:
        from evotrader import main

        return inspect.getsource(main)

    def test_no_agent_runs_while_the_plan_is_spent_and_the_setting_says_so(self):
        src = self._src()
        assert "strategy_host.decides_without_quota()" in src
        assert "record_cycle_without_agents(" in src
        assert "_no_events()" in src

    def test_a_cancelled_cycle_reads_cancelled(self):
        src = self._src()
        block = src[src.index("except asyncio.CancelledError:") :][:900]
        assert 'status="CANCELLED"' in block

    def test_open_runs_are_closed_before_anything_runs(self):
        src = self._src()
        assert src.index("close_open_runs()") < src.index("async def run_cycle_task")

    def test_the_status_comes_from_the_strategy_outcome(self):
        tree = ast.parse(self._src())
        called = {
            getattr(n.func, "id", getattr(n.func, "attr", ""))
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert {"_cycle_status", "outcome"} <= called
