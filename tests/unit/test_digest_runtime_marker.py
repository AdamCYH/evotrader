"""The cycle digest must show the agent's reasoning, not a runtime marker.

See: data/evolution/reviews/20260918_210213_20260918_digest_final_thought_is_runtime_marker.md

``_make_event_sink`` logged harness quota checks as ``event_type='thought'``
with the content ``[runtime] quota ok``. The CLI backend emits that check AFTER
the agent's final report, so the LAST 'thought' row for an agent was the marker,
and ``get_final_thoughts`` ranks by id DESC. Result: ``get_cycle_digest`` — the
evolution agent's primary review tool — showed ``strategy: "[runtime] quota ok"``
on 4 of 5 regular-hours cycles on 2026-09-18 (sessions 60f4e526, d2f925ce,
41ba2669, da2704b0). The reasoning was in the table the whole time (event 18403);
the digest just hid it. Evolution reviewed its own game film with the picture
blacked out.

Both halves are needed. New rows get their own ``event_type='runtime'`` so no
consumer has to string-match. History cannot be rewritten, so the query also
excludes ``[runtime]`` content — 47 such rows already exist in the live journal.
"""

from __future__ import annotations

import pytest

from evotrader.db.thought_log import ThoughtLogger

_SID = "cc-20260918T183300Z"
_REASONING = (
    "Composite -0.034, authored by intraday_vwap_zscore counter-trend. "
    "FLAT: the long is vetoed and the short has no placeable vehicle."
)


@pytest.fixture
def logger(db) -> ThoughtLogger:
    return ThoughtLogger(db)


async def _cycle_with_trailing_quota_check(logger: ThoughtLogger) -> None:
    """The exact live shape: reasoning first, harness quota marker last."""
    await logger.record_event(_SID, "strategy", "thought", _REASONING)
    await logger.record_event(_SID, "strategy", "tool_call", "log_signal_attribution")
    await logger.record_event(
        _SID, "strategy", "runtime", "[runtime] quota ok", meta={"utilization": 0.11}
    )


class TestTheDigestShowsReasoning:
    async def test_final_thought_is_the_reasoning_not_the_marker(
        self, logger: ThoughtLogger
    ) -> None:
        """THE REGRESSION CASE."""
        await _cycle_with_trailing_quota_check(logger)
        finals = await logger.get_final_thoughts(_SID)
        by_agent = {f["agent_name"]: f["content"] for f in finals}
        assert by_agent["strategy"] == _REASONING
        assert "[runtime]" not in by_agent["strategy"]

    async def test_historical_rows_logged_as_thoughts_are_still_excluded(
        self, logger: ThoughtLogger
    ) -> None:
        """47 rows already in the live journal carry event_type='thought' with
        '[runtime]' content. Those cannot be rewritten, so the query must
        exclude them too — the new event_type alone does not fix history."""
        await logger.record_event(_SID, "strategy", "thought", _REASONING)
        await logger.record_event(_SID, "strategy", "thought", "[runtime] quota ok")
        finals = await logger.get_final_thoughts(_SID)
        assert {f["content"] for f in finals} == {_REASONING}

    async def test_an_agent_with_only_markers_contributes_nothing(
        self, logger: ThoughtLogger
    ) -> None:
        """Better to say nothing than to report a marker as reasoning."""
        await logger.record_event(_SID, "strategy", "thought", _REASONING)
        await logger.record_event(_SID, "news_sentiment", "runtime", "[runtime] quota ok")
        finals = await logger.get_final_thoughts(_SID)
        assert [f["agent_name"] for f in finals] == ["strategy"]

    async def test_other_agents_are_unaffected(self, logger: ThoughtLogger) -> None:
        await _cycle_with_trailing_quota_check(logger)
        await logger.record_event(_SID, "orchestrator", "thought", "Cycle complete, no trade.")
        finals = await logger.get_final_thoughts(_SID)
        by_agent = {f["agent_name"]: f["content"] for f in finals}
        assert by_agent == {"strategy": _REASONING, "orchestrator": "Cycle complete, no trade."}

    async def test_get_cycle_digest_end_to_end(self, logger: ThoughtLogger, monkeypatch) -> None:
        import evotrader.evolution.tools as evo

        await _cycle_with_trailing_quota_check(logger)
        monkeypatch.setattr(evo, "_thought_logger", logger)
        digest = await evo.get_cycle_digest(limit=5)
        cycle = next(c for c in digest["cycles"] if c["session_id"] == _SID)
        assert cycle["final_thoughts"]["strategy"] == _REASONING


class TestRuntimeEventsAreStillVisibleWhenAskedFor:
    """Filtering them out of the digest must not delete them — a quota wall is
    exactly the kind of thing evolution needs to be able to find."""

    async def test_they_are_retrievable_by_event_type(self, logger: ThoughtLogger) -> None:
        await _cycle_with_trailing_quota_check(logger)
        rows = await logger.get_recent_thoughts(limit=50, session_id=_SID, event_type="runtime")
        assert [r["content"] for r in rows] == ["[runtime] quota ok"]

    async def test_they_still_appear_in_the_unfiltered_timeline(
        self, logger: ThoughtLogger
    ) -> None:
        await _cycle_with_trailing_quota_check(logger)
        rows = await logger.get_recent_thoughts(limit=50, session_id=_SID)
        assert any("[runtime]" in r["content"] for r in rows)

    def test_the_tool_docs_name_the_new_event_type(self) -> None:
        import evotrader.evolution.tools as evo

        assert "runtime" in (evo.query_cycle_thoughts.__doc__ or ""), (
            "an event_type the agent is never told about cannot be queried"
        )


class TestTheSinkStopsMislabellingQuotaChecks:
    async def test_a_quota_event_is_logged_as_runtime_not_thought(self) -> None:
        """Source of the bad rows: cli_agent._make_event_sink."""
        from evotrader.agents.cli.types import AgentEvent

        recorded: list[dict] = []

        class _Logger:
            async def record_event(self, **kw):
                recorded.append(kw)

        from evotrader.agents.cli_agent import CliBackedAgent

        agent = CliBackedAgent.__new__(CliBackedAgent)
        object.__setattr__(agent, "thought_logger", _Logger())
        object.__setattr__(agent, "name", "strategy")

        sink = agent._make_event_sink("sess-1")
        await sink(AgentEvent(kind="thought", content="real reasoning"))
        await sink(AgentEvent(kind="quota", content="quota ok", payload={"utilization": 0.1}))

        assert [r["event_type"] for r in recorded] == ["thought", "runtime"]
        assert recorded[1]["content"] == "[runtime] quota ok"


class TestQuotaChecksAreNotCountedAsLlmCalls:
    async def test_evolution_run_call_count_excludes_runtime_rows(
        self, logger: ThoughtLogger, db
    ) -> None:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO cycle_runs (session_id, timestamp, cycle_type, status) "
                "VALUES (?, ?, 'EVOLUTION', 'SUCCESS')",
                (_SID, "2026-09-18T21:00:00+00:00"),
            )
        await logger.record_event(_SID, "evolution", "thought", "analysing")
        await logger.record_event(_SID, "evolution", "runtime", "[runtime] quota ok")

        runs = await logger.get_evolution_runs()
        row = next(r for r in runs if r["session_id"] == _SID)
        assert row["llm_call_count"] == 1, "a quota check is not an LLM call"


class TestTheConsoleRendersRuntimeNotes:
    def test_the_timeline_handles_the_runtime_type(self) -> None:
        from pathlib import Path

        js = Path("src/evotrader/web/static/js/drawer_controller.js").read_text()
        assert '"runtime"' in js or "'runtime'" in js, (
            "an event_type the console does not know about renders as nothing"
        )


class TestRiskBudgetShowsWhenTheFloorIsSettingIt:
    """Finding 2. On MSTR (realised vol ~88%) a 15-20% target implies ~0.2x, so
    the 0.30 floor binds on every cycle and the tool emits a constant. The agent
    cannot tell that from a measured reading unless the tool says so."""

    def _snapshot(self, daily_pct_moves: list[float]):
        from datetime import UTC, datetime, timedelta

        from evotrader.models.market import (
            OHLCV,
            MarketRegime,
            MarketSnapshot,
            Quote,
            RegimeClassification,
            TechnicalIndicators,
        )

        now = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
        price = 100.0
        candles = []
        for i, move in enumerate(daily_pct_moves):
            price *= 1 + move / 100.0
            candles.append(
                OHLCV(
                    timestamp=now - timedelta(days=len(daily_pct_moves) - i),
                    open=price,
                    high=price * 1.01,
                    low=price * 0.99,
                    close=price,
                    volume=1e6,
                )
            )
        return MarketSnapshot(
            ticker="MSTR",
            timestamp=now,
            quote=Quote(
                ticker="MSTR",
                bid=price - 0.01,
                ask=price + 0.01,
                last=price,
                volume=1e6,
                timestamp=now,
            ),
            indicators=TechnicalIndicators(atr_14=price * 0.065),
            regime=RegimeClassification(
                regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
            ),
            daily_candles=candles,
        )

    async def test_a_floor_bound_reading_says_so(self, monkeypatch) -> None:
        import evotrader.agents.tools as tools

        snap = self._snapshot([5.5, -6.0, 4.5, -5.0] * 8)  # very high vol
        monkeypatch.setattr(tools, "_config", None)
        out = await tools.compute_risk_budget(market_snapshot_json=snap.model_dump_json())

        assert out["floor_binding"] is True
        assert out["raw_exposure"] is not None
        assert out["raw_exposure"] < out["min_exposure"]
        assert out["risk_budget"] == pytest.approx(out["min_exposure"])

    async def test_a_measured_reading_is_not_flagged(self, monkeypatch) -> None:
        import evotrader.agents.tools as tools

        snap = self._snapshot([0.25, -0.2, 0.3, -0.15] * 8)  # calm
        monkeypatch.setattr(tools, "_config", None)
        out = await tools.compute_risk_budget(market_snapshot_json=snap.model_dump_json())
        assert out["floor_binding"] is False
