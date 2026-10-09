"""A refused proposal reaches the next cycle.

Found by the evolution agent's code review. The risk manager's verdict lived
only in its report, and the next cycle starts from a new session: a cycle read
the missing order as one that had "left no trace at the broker" and proposed
the same over-wide stop again, which was refused for the same reason. The
verdict is now read back from the thought log: a SYSTEM line in the trading
handoff after a cycle whose last review was REJECTED, and ``last_risk_verdict``
in ``get_open_positions``.

Made-up reports, tickers and prices.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from evotrader.tools.risk_verdict import (
    latest_risk_review,
    note_rejection,
    reasons_of,
    rejection_line,
    verdict_of,
)
from evotrader.tools.trading_handoff import handoff_path, write_handoff

REJECTED_REPORT = """# Risk Evaluation Report

**Verdict:** **REJECTED**

### 1. Violation Details
1. **Stop-Loss Cap Breach (`max_stop_loss_pct: 20%`):** the stop at $9.00 is 28% below $12.50.

### 3. Required Modifications for Resubmission
1. **Stop Price:** Set the stop at or above **$10.00**.
2. **Full Coverage:** Place the stop on all 20 shares.
"""

APPROVED_REPORT = "## Risk Manager Verdict: APPROVED\n\nAll hard limits pass."

_T = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)


class TestReadingTheReport:
    @pytest.mark.parametrize(
        ("text", "verdict"),
        [
            ("**Verdict:** **REJECTED**", "REJECTED"),
            ("## Risk Manager Verdict: **MODIFIED (reduce size)**", "MODIFIED"),
            ("## VALIDATION RESULT: **REJECTED**", "REJECTED"),
            ("✅ VERDICT: APPROVED", "APPROVED"),
            ("## **DECISION: REJECTED**", "REJECTED"),
            ("The tool returned APPROVED.\n\n**Verdict:** REJECTED", "REJECTED"),
        ],
    )
    def test_a_labelled_verdict(self, text: str, verdict: str) -> None:
        assert verdict_of(text) == verdict

    @pytest.mark.parametrize(
        "text",
        [
            "The trade proposal has been **APPROVED**.",
            "Checks: check_risk_limits returns REJECTED for the add.",
            "",
            None,
        ],
    )
    def test_an_unlabelled_verdict_is_not_guessed(self, text) -> None:
        assert verdict_of(text) is None

    def test_the_reasons_are_the_required_modifications(self) -> None:
        reasons = reasons_of(REJECTED_REPORT)
        assert reasons.startswith("1. Stop Price: Set the stop at or above $10.00.")
        assert "Full Coverage" in reasons
        assert "*" not in reasons and "#" not in reasons

    def test_without_that_section_the_text_after_the_verdict(self) -> None:
        text = "## VERDICT: REJECTED\n\n**Circuit breaker:** `consecutive_losses: 5 (limit: 5)`"
        assert reasons_of(text) == "Circuit breaker: consecutive_losses: 5 (limit: 5)"

    def test_the_reasons_are_short(self) -> None:
        assert len(reasons_of("Verdict: REJECTED " + "x " * 400)) <= 300


async def _event(db, session, at, agent, event_type, content, meta) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO agent_thought_log (timestamp, session_id, agent_name, event_type, "
            "content, meta) VALUES (?,?,?,?,?,?)",
            (at.isoformat(), session, agent, event_type, content, json.dumps(meta)),
        )


async def _review(
    db, session: str, at: datetime, report: str, *, args: dict | None = None, check=None
) -> None:
    """One review as a cycle records it: the orchestrator's call, the risk
    manager's check, its answer, and the report."""
    await _event(db, session, at, "orchestrator", "tool_call", "risk_manager", {"args": {}})
    if args is not None:
        await _event(
            db,
            session,
            at + timedelta(seconds=3),
            "risk_manager",
            "tool_call",
            "check_risk_limits",
            {"args": args},
        )
        await _event(
            db,
            session,
            at + timedelta(seconds=4),
            "risk_manager",
            "tool_response",
            "check_risk_limits",
            {"response": check or {"verdict": "APPROVED", "violations": []}},
        )
    await _event(
        db,
        session,
        at + timedelta(seconds=60),
        "orchestrator",
        "tool_response",
        "risk_manager",
        {"response": {"result": report}},
    )


ARGS = {
    "ticker": "XYZ",
    "direction": "LONG",
    "quantity": 20,
    "price": 12.5,
    "action": "OPEN",
    "stop_price": 9.0,
}


class TestTheLatestReview:
    async def test_a_rejection_with_what_was_checked(self, db) -> None:
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS)
        review = await latest_risk_review(db, session_id="s1")
        assert review["verdict"] == "REJECTED"
        assert review["trade"] == ARGS
        assert review["violations"] == []
        assert "at or above $10.00" in review["reasons"]

    async def test_the_cycles_last_review_counts(self, db) -> None:
        """Refused, then resubmitted and approved in the same cycle: it went through."""
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS)
        await _review(
            db, "s1", _T + timedelta(minutes=4), APPROVED_REPORT, args={**ARGS, "stop_price": 10.1}
        )
        review = await latest_risk_review(db, session_id="s1")
        assert review["verdict"] == "APPROVED"
        assert review["trade"]["stop_price"] == 10.1

    async def test_a_later_review_without_a_check_does_not_borrow_the_earlier_one(self, db) -> None:
        await _review(db, "s1", _T, APPROVED_REPORT, args=ARGS)
        await _review(db, "s1", _T + timedelta(minutes=4), REJECTED_REPORT)
        review = await latest_risk_review(db, session_id="s1")
        assert (review["verdict"], review["trade"]) == ("REJECTED", None)

    async def test_the_checks_own_violations(self, db) -> None:
        check = {"verdict": "REJECTED", "violations": ["Stop 9 is 28.0% from the entry price"]}
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS, check=check)
        review = await latest_risk_review(db, session_id="s1")
        assert review["violations"] == ["Stop 9 is 28.0% from the entry price"]

    async def test_since_and_sessions(self, db) -> None:
        await _review(db, "old", _T - timedelta(days=2), REJECTED_REPORT, args=ARGS)
        await _review(db, "new", _T, APPROVED_REPORT, args=ARGS)
        assert (await latest_risk_review(db))["session_id"] == "new"
        assert await latest_risk_review(db, since=(_T + timedelta(hours=1)).isoformat()) is None
        assert (await latest_risk_review(db, session_id="old"))["verdict"] == "REJECTED"
        assert await latest_risk_review(db, session_id="none") is None


class TestTheNoteForTheNextCycle:
    async def test_a_rejection_lands_in_the_handoff_once(self, db, tmp_path) -> None:
        write_handoff(tmp_path, "Flat. Watching for a bounce.", session_id="s1")
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS)
        assert await note_rejection(db, "s1", tmp_path) is True
        assert await note_rejection(db, "s1", tmp_path) is False, "an identical line once"
        text = handoff_path(tmp_path).read_text()
        assert text.count("SYSTEM: risk_manager REJECTED") == 1
        line = next(row for row in text.splitlines() if row.startswith("SYSTEM: risk_manager"))
        assert "OPEN LONG 20 XYZ @ 12.5, stop 9.0 at 09:36 ET (session s1)" in line
        assert "nothing was sent to the broker" in line
        assert "at or above $10.00" in line

    async def test_an_approved_cycle_writes_nothing(self, db, tmp_path) -> None:
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS)
        await _review(db, "s1", _T + timedelta(minutes=4), APPROVED_REPORT, args=ARGS)
        assert await note_rejection(db, "s1", tmp_path) is False
        assert not handoff_path(tmp_path).exists()

    def test_the_checks_violations_come_first(self) -> None:
        line = rejection_line(
            {
                "session_id": "s1",
                "at": _T.isoformat(),
                "trade": None,
                "violations": ["Stop 9 is 28.0% from the entry price 12.5"],
                "reasons": "the report's words",
            }
        )
        assert line.endswith("Reasons: Stop 9 is 28.0% from the entry price 12.5")
        assert "REJECTED the proposal at 09:35 ET" in line


class TestTheStrategyReadsItAsData:
    async def test_get_open_positions_returns_the_last_verdict(self, db, monkeypatch) -> None:
        from evotrader.agents import tools
        from evotrader.tools import market_hours

        journal = AsyncMock()
        journal.get_open_trades.return_value = []
        journal.get_pending_orders.return_value = []
        monkeypatch.setattr(tools, "_journal", journal)
        monkeypatch.setattr(tools, "_db", db)
        monkeypatch.setattr(tools, "_config", None)
        monkeypatch.setattr(tools, "_open_positions_cache", None)
        monkeypatch.setattr(market_hours, "_resolve_now", lambda now=None: _T + timedelta(hours=1))
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS)

        out = await tools.get_open_positions()
        assert out["last_risk_verdict"]["verdict"] == "REJECTED"
        assert out["last_risk_verdict"]["trade"]["stop_price"] == 9.0

    async def test_an_old_verdict_is_not_shown(self, db, monkeypatch) -> None:
        from evotrader.agents import tools
        from evotrader.tools import market_hours

        journal = AsyncMock()
        journal.get_open_trades.return_value = []
        journal.get_pending_orders.return_value = []
        monkeypatch.setattr(tools, "_journal", journal)
        monkeypatch.setattr(tools, "_db", db)
        monkeypatch.setattr(tools, "_config", None)
        monkeypatch.setattr(tools, "_open_positions_cache", None)
        monkeypatch.setattr(market_hours, "_resolve_now", lambda now=None: _T + timedelta(days=2))
        await _review(db, "s1", _T, REJECTED_REPORT, args=ARGS)

        assert (await tools.get_open_positions())["last_risk_verdict"] is None
