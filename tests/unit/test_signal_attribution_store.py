"""Tests for the signal_attribution store and its agent tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from evotrader.db.connection import Database
from evotrader.db.signal_attribution import SignalAttributionStore


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    yield SignalAttributionStore(db)
    await db.close()


class TestRecord:
    async def test_records_a_full_cycle(self, store: SignalAttributionStore) -> None:
        rid = await store.record(
            "QQQ",
            session_id="s1",
            algo_direction=1.0,
            algo_strength=0.4,
            algo_author="momentum",
            participation_ratio=0.66,
            news_direction=1.0,
            news_strength=0.8,
            catalyst="Q3 guidance raised",
            llm_direction=1.0,
            llm_conviction=0.75,
            mechanism="Guidance implies FY revenue +6%, consensus unrevised",
            priced_in_check={
                "known_fact": "guidance raise",
                "when_learned": "today 08:30",
                "mechanism": "consensus unrevised",
                "falsifier": "peers flat",
            },
            final_direction=1.0,
            final_conviction=0.75,
            risk_budget=1.3,
            position_size=0.98,
            traded=True,
            price_at_decision=580.25,
        )
        assert rid is not None and rid > 0

    async def test_records_a_no_trade_cycle(self, store: SignalAttributionStore) -> None:
        """No-trade cycles are the control group and must be recorded."""
        rid = await store.record(
            "QQQ",
            algo_direction=1.0,
            news_direction=-1.0,
            llm_direction=0.0,
            llm_conviction=0.0,
            final_direction=0.0,
            traded=False,
            price_at_decision=580.0,
        )
        assert rid is not None

    async def test_minimal_row_is_accepted(self, store: SignalAttributionStore) -> None:
        assert await store.record("SPY") is not None


class TestScoring:
    async def test_unscored_rows_are_not_returned_before_horizon(
        self, store: SignalAttributionStore
    ) -> None:
        await store.record("QQQ", price_at_decision=580.0)
        assert await store.pending_scoring(horizon_days=5) == []

    async def test_rows_without_price_are_never_pending(
        self, store: SignalAttributionStore
    ) -> None:
        await store.record("QQQ")  # no price_at_decision
        assert await store.pending_scoring(horizon_days=0) == []

    async def test_fresh_row_is_pending_at_zero_horizon(
        self, store: SignalAttributionStore
    ) -> None:
        """Regression: julianday('now') truncates to seconds, so a row written
        microseconds ago could compute as negative age and be skipped."""
        for _ in range(20):
            rid = await store.record("QQQ", price_at_decision=100.0)
            pending = await store.pending_scoring(horizon_days=0)
            assert rid in [p["id"] for p in pending]
            await store.score(rid, 0.0, 0.0)

    async def test_score_backfills_and_clears_pending(self, store: SignalAttributionStore) -> None:
        rid = await store.record(
            "QQQ", llm_direction=1.0, llm_conviction=0.8, price_at_decision=580.0
        )
        assert rid is not None
        pending = await store.pending_scoring(horizon_days=0)
        assert [p["id"] for p in pending] == [rid]

        await store.score(rid, forward_return_1d=0.012, forward_return_5d=0.031)
        assert await store.pending_scoring(horizon_days=0) == []

        rows = await store.scored_rows()
        assert len(rows) == 1
        assert rows[0]["forward_return_5d"] == pytest.approx(0.031)
        assert rows[0]["llm_conviction"] == pytest.approx(0.8)

    async def test_scored_rows_feed_the_attribution_scorer(
        self, store: SignalAttributionStore
    ) -> None:
        """End to end: logged witnesses become a scoreable record."""
        from evotrader.backtest.attribution import attribute

        for i in range(60):
            up = i % 2 == 0
            rid = await store.record(
                "QQQ",
                algo_direction=1.0 if up else -1.0,
                news_direction=1.0 if up else -1.0,
                llm_direction=1.0 if up else -1.0,
                price_at_decision=100.0,
            )
            assert rid is not None
            await store.score(rid, 0.01 if up else -0.01, 0.02 if up else -0.02)

        rows = await store.scored_rows()
        rep = attribute(
            {
                "algo": [r["algo_direction"] for r in rows],
                "news": [r["news_direction"] for r in rows],
                "llm": [r["llm_direction"] for r in rows],
            },
            [r["forward_return_5d"] for r in rows],
            [r["timestamp"] for r in rows],
        )
        assert len(rep.sources) == 3
        assert all(s.n_calls == 60 for s in rep.sources)


class TestRiskBudgetTool:
    async def test_bad_snapshot_falls_back_safely(self) -> None:
        from evotrader.agents.tools import compute_risk_budget

        out = await compute_risk_budget("not json at all")
        assert out["risk_budget"] == pytest.approx(1.0)
        assert "error" in out
        assert out["degraded"] is True

    async def test_returns_usage_formula(self) -> None:
        from evotrader.agents.tools import compute_risk_budget

        out = await compute_risk_budget(json.dumps({"ticker": "QQQ"}))
        assert "risk_budget" in out
