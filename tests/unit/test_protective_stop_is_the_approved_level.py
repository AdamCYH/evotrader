"""A protective stop rests at the level the risk manager approved in its cycle.

Found by the evolution agent's code review. The orchestrator's request to the
executor carried no stop, so the executor placed one at a level of its own,
much closer than the approved stop, and the gate checked its type and time in
force but not its level. The approval was in the cycle's own record. The gate
now reads it (the risk manager's last review in this session, approved or
modified, for the same symbol) and refuses a protective sell stop away from it,
within the larger of a quarter ATR and 1 percent. A stop that already rested
for the symbol (one cancelled to resize a lot) passes at its own level, and
with no approval on record nothing is checked.

Made-up ticker, prices, times and session.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader import paths
from evotrader.agents import tools
from evotrader.agents.factory import create_execution_agent
from evotrader.callbacks.risk_gate import (
    protective_stop_level_violation,
    stop_level_tolerance,
)
from evotrader.config import AppConfig
from evotrader.db.journal import TradeJournal

SESSION = "s-0001"
NOW = datetime.now(UTC).replace(microsecond=0)


def _stop(**extra) -> dict:
    args = {
        "symbol": "XYZ",
        "side": "sell",
        "type": "stop_market",
        "quantity": "6",
        "stop_price": "54.00",
        "time_in_force": "gtc",
        **extra,
    }
    return {k: v for k, v in args.items() if v is not None}


class TestTheRule:
    def test_the_tolerance(self) -> None:
        assert stop_level_tolerance(50.0, 4.0) == 1.0  # a quarter ATR
        assert stop_level_tolerance(50.0, 1.0) == 0.5  # 1 percent of the level
        assert stop_level_tolerance(50.0) == 0.5

    def test_a_made_up_level_is_refused(self) -> None:
        violation = protective_stop_level_violation(_stop(), 50.0, atr=4.0)
        assert violation is not None
        assert "54.00" in violation and "50.00" in violation and "within 1.00" in violation

    @pytest.mark.parametrize("placed", ["50.00", "50.95", "49.05"])
    def test_the_approved_level_passes(self, placed) -> None:
        assert protective_stop_level_violation(_stop(stop_price=placed), 50.0, atr=4.0) is None

    def test_a_level_that_already_rested_passes(self) -> None:
        assert protective_stop_level_violation(_stop(), 50.0, 4.0, known_levels=(54.0,)) is None

    @pytest.mark.parametrize(
        "args",
        [
            _stop(side="buy"),  # opens a position
            _stop(type="limit", stop_price=None, limit_price="58.00"),  # a take-profit
            {"legs": [{"option_id": "x", "side": "sell"}], "type": "stop_market"},
        ],
    )
    def test_only_protective_sell_stops_are_checked(self, args) -> None:
        assert protective_stop_level_violation(args, 50.0, atr=4.0) is None

    def test_no_approval_checks_nothing(self) -> None:
        assert protective_stop_level_violation(_stop(), None, atr=4.0) is None


async def _event(db, at, event_type, content, meta, session=SESSION) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO agent_thought_log (timestamp, session_id, agent_name, event_type, "
            "content, meta) VALUES (?,?,?,?,?,?)",
            (at.isoformat(), session, "orchestrator", event_type, content, json.dumps(meta)),
        )


async def _review(db, verdict="APPROVED", ticker="XYZ", stop=50.0, session=SESSION) -> None:
    t = NOW - timedelta(minutes=5)
    await _event(db, t, "tool_call", "risk_manager", {"args": {"request": "..."}}, session)
    trade = {"ticker": ticker, "action": "OPEN", "quantity": 8, "price": 60.0, "stop_price": stop}
    await _event(
        db, t + timedelta(seconds=5), "tool_call", "check_risk_limits", {"args": trade}, session
    )
    await _event(
        db,
        t + timedelta(seconds=6),
        "tool_response",
        "check_risk_limits",
        {"response": {"violations": []}},
        session,
    )
    await _event(
        db,
        t + timedelta(seconds=20),
        "tool_response",
        "risk_manager",
        {"response": {"result": f"## Verdict: {verdict}\n\nAll limits pass."}},
        session,
    )
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO market_snapshots (timestamp, session_id, ticker, close_price, "
            "indicators_json) VALUES (?,?,?,?,?)",
            (t.isoformat(), session, ticker, 60.0, json.dumps({"atr_14": 4.0})),
        )


@pytest.fixture
def cycle(db, monkeypatch):
    monkeypatch.setattr(tools, "_db", db)
    monkeypatch.setattr(tools, "_journal", TradeJournal(db))
    monkeypatch.setattr(tools, "_current_session_id", SESSION)
    return db


class TestTheApproval:
    async def test_this_cycles_approval(self, cycle) -> None:
        await _review(cycle)
        assert await tools.approved_stop_for("XYZ") == (50.0, 4.0, ())

    @pytest.mark.parametrize(
        "review",
        [
            {"verdict": "REJECTED"},
            {"ticker": "ABC"},
            {"session": "s-0000"},  # an older cycle's approval
        ],
    )
    async def test_no_approval_to_hold_to(self, cycle, review) -> None:
        await _review(cycle, **review)
        assert await tools.approved_stop_for("XYZ") == (None, None, ())

    async def test_a_modified_proposal_counts(self, cycle) -> None:
        await _review(cycle, verdict="MODIFIED", stop=48.5)
        assert (await tools.approved_stop_for("XYZ"))[0] == 48.5

    async def test_stops_that_already_rested(self, cycle) -> None:
        await _review(cycle)
        async with cycle.transaction() as conn:
            for when, status, level in (
                (NOW - timedelta(days=3), "PENDING", 47.0),  # still resting
                (NOW - timedelta(hours=2), "CANCELLED", 49.0),  # cancelled to resize
                (NOW - timedelta(days=3), "CANCELLED", 44.0),  # long gone
            ):
                await conn.execute(
                    "INSERT INTO trades (timestamp, ticker, action, direction, quantity, price, "
                    "order_type, stop_price, order_status, algo_version, regime, confidence, "
                    "reasoning, market_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        when.isoformat(),
                        "XYZ",
                        "STOP_LOSS",
                        "LONG",
                        3,
                        level,
                        "stop",
                        level,
                        status,
                        "test",
                        "range_bound",
                        0.5,
                        "test",
                        "{}",
                    ),
                )
        _, _, known = await tools.approved_stop_for("XYZ")
        assert sorted(known) == [47.0, 49.0]


class TestTheExecutorsGate:
    def _agent(self, tmp_path: Path, monkeypatch):
        shutil.copytree(paths.project_root() / "starter_data", tmp_path / "data")
        config = AppConfig(data_dir=tmp_path / "data")
        from evotrader.web import server

        monkeypatch.setattr(server, "check_interactive_approval", AsyncMock(return_value=None))
        return create_execution_agent(config)

    async def _call(self, agent, args):
        tool = MagicMock()
        tool.name = "place_equity_order"
        return await agent.before_tool_callback(tool=tool, args=args, tool_context=MagicMock())

    async def test_a_made_up_stop_is_refused(self, tmp_path, monkeypatch, cycle) -> None:
        await _review(cycle)
        verdict = await self._call(self._agent(tmp_path, monkeypatch), _stop())
        assert verdict is not None and verdict["allowed"] is False
        assert verdict["action"] == "PROTECTIVE_STOP_NOT_APPROVED"
        assert verdict["retryable"] is True and verdict["fix"].endswith("stop_price 50.")

    async def test_the_approved_stop_goes_on(self, tmp_path, monkeypatch, cycle) -> None:
        await _review(cycle)
        verdict = await self._call(self._agent(tmp_path, monkeypatch), _stop(stop_price="50.00"))
        assert verdict is None or verdict.get("action") != "PROTECTIVE_STOP_NOT_APPROVED"

    async def test_without_an_approval_it_warns(self, tmp_path, monkeypatch, cycle, caplog) -> None:
        verdict = await self._call(self._agent(tmp_path, monkeypatch), _stop())
        assert verdict is None or verdict.get("action") != "PROTECTIVE_STOP_NOT_APPROVED"
        assert "no approved stop for XYZ" in caplog.text
