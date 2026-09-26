"""Memory must be scoped to the instrument being traded, and to recent history.

Operator request, 2026-09-18: "if there is memory that is stale — trading
history that is too old, or that failed initially for an irrelevant cause — we
shouldn't let it affect our current decision, especially since the ticker has
changed."

What the survey found: all 141 stored trade experiences carried NO ticker in
their metadata, and ``query_similar_trades`` could filter only by regime. The
strategy agent's ``query_past_trades`` therefore returned QQQ-era experiences
for MSTR decisions as if they were the same market — an index ETF with a 1% ATR
and a single stock with a 6.5% ATR do not share a base rate. On top of that,
447 agent-written notes had accumulated through the QQQ era, the oldest from
2026-06-22, and were retrieved as if still current.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evotrader.tools.memory import SemanticMemory


@pytest.fixture
def memory(tmp_path: Path) -> SemanticMemory:
    return SemanticMemory(tmp_path / "mem")


def _store(
    memory: SemanticMemory,
    trade_id: int,
    ticker: str,
    days_ago: int = 0,
    regime: str = "trending_bull",
    pnl: float | None = None,
) -> None:
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    memory.store_trade_experience(
        trade_id=trade_id,
        direction="LONG",
        regime=regime,
        algo_signal=0.2,
        llm_signal=0.1,
        hybrid_score=0.3,
        reasoning=f"{ticker} momentum entry, bull stack, dip bought",
        outcome_pnl=pnl,
        timestamp=ts.isoformat(),
        ticker=ticker,
    )
    # store_trade_experience stamps timestamp_epoch as NOW; push it back so
    # the recency filter has something to bite on.
    got = memory._trades.get(ids=[f"trade_{trade_id}"], include=["metadatas"])
    if got["ids"]:
        md = dict(got["metadatas"][0])
        md["timestamp_epoch"] = ts.timestamp()
        memory._trades.update(ids=got["ids"], metadatas=[md])


class TestExperiencesCarryTheirInstrument:
    def test_ticker_is_stored_upper_cased(self, memory: SemanticMemory) -> None:
        _store(memory, 1, "mstr")
        exps = memory.get_all_trade_experiences()
        assert exps[0]["metadata"]["ticker"] == "MSTR"

    def test_query_scoped_to_one_ticker_excludes_the_other(self, memory: SemanticMemory) -> None:
        _store(memory, 1, "QQQ", pnl=-12.0)
        _store(memory, 2, "MSTR", pnl=+13.6)
        res = memory.query_similar_trades("momentum entry bull stack", n_results=5, ticker="MSTR")
        assert res, "the MSTR experience must be found"
        assert {r["metadata"]["ticker"] for r in res} == {"MSTR"}

    def test_unscoped_query_still_sees_everything(self, memory: SemanticMemory) -> None:
        _store(memory, 1, "QQQ")
        _store(memory, 2, "MSTR")
        res = memory.query_similar_trades("momentum entry", n_results=5)
        assert {r["metadata"]["ticker"] for r in res} == {"QQQ", "MSTR"}


class TestOldHistoryIsNotCurrentEvidence:
    def test_recency_window_drops_stale_experiences(self, memory: SemanticMemory) -> None:
        _store(memory, 1, "MSTR", days_ago=120)
        _store(memory, 2, "MSTR", days_ago=3)
        res = memory.query_similar_trades(
            "momentum entry", n_results=5, ticker="MSTR", max_age_days=60
        )
        assert [r["metadata"]["trade_id"] for r in res] == [2]

    def test_ticker_and_age_combine(self, memory: SemanticMemory) -> None:
        _store(memory, 1, "QQQ", days_ago=3)
        _store(memory, 2, "MSTR", days_ago=120)
        _store(memory, 3, "MSTR", days_ago=3)
        res = memory.query_similar_trades(
            "momentum entry", n_results=5, ticker="MSTR", max_age_days=60
        )
        assert [r["metadata"]["trade_id"] for r in res] == [3]


class TestBackfillRepairsThePreExistingHistory:
    def test_experiences_stored_without_a_ticker_are_stamped_from_the_trades_table(
        self, memory: SemanticMemory
    ) -> None:
        # Simulate the pre-2026-09-18 store: no ticker key at all.
        memory.store_trade_experience(
            trade_id=161,
            direction="LONG",
            regime="range_bound",
            algo_signal=0.1,
            llm_signal=0.0,
            hybrid_score=0.2,
            reasoning="old",
        )
        got = memory._trades.get(ids=["trade_161"], include=["metadatas"])
        md = dict(got["metadatas"][0])
        md.pop("ticker", None)
        memory._trades.update(ids=["trade_161"], metadatas=[md])

        stats = memory.backfill_trade_tickers({161: "QQQ", 999: "MSTR"})
        assert stats == {"examined": 1, "updated": 1, "unresolved": 0}
        got = memory._trades.get(ids=["trade_161"], include=["metadatas"])
        assert got["metadatas"][0]["ticker"] == "QQQ"

    def test_backfill_is_idempotent_and_leaves_unknowns_alone(self, memory: SemanticMemory) -> None:
        _store(memory, 5, "MSTR")
        memory.store_trade_experience(
            trade_id=77,
            direction="LONG",
            regime="x",
            algo_signal=0,
            llm_signal=0,
            hybrid_score=0,
            reasoning="?",
        )
        got = memory._trades.get(ids=["trade_77"], include=["metadatas"])
        md = dict(got["metadatas"][0])
        md.pop("ticker", None)
        memory._trades.update(ids=["trade_77"], metadatas=[md])

        stats = memory.backfill_trade_tickers({})  # nothing resolvable
        assert stats["updated"] == 0 and stats["unresolved"] == 1
        again = memory.backfill_trade_tickers({})
        assert again["updated"] == 0


class TestAgentNotesAreRecentHumanNotesAreForever:
    def test_old_agent_notes_are_filtered_new_ones_and_human_notes_are_not(
        self, memory: SemanticMemory
    ) -> None:
        memory.store_note(
            note_id="agent_old",
            text="QQQ no trade, composite weakly bearish",
            source="agent_learning",
            category="observation",
        )
        memory.store_note(
            note_id="agent_new",
            text="MSTR gap-and-go: silence is the trend's signature",
            source="agent_learning",
            category="observation",
        )
        memory.store_note(
            note_id="human_old",
            text="Never trade the first 15 minutes",
            source="user",
            category="rule",
        )
        # Age the old ones.
        old_epoch = (datetime.now(UTC) - timedelta(days=200)).timestamp()
        for nid in ("agent_old", "human_old"):
            got = memory._notes.get(ids=[nid], include=["metadatas"])
            md = dict(got["metadatas"][0])
            md["timestamp_epoch"] = old_epoch
            memory._notes.update(ids=[nid], metadatas=[md])

        res = memory.query_notes("trading guidance", n_results=10, agent_note_max_age_days=30)
        docs = " | ".join(r["document"] for r in res)
        assert "QQQ no trade" not in docs, "a 200-day-old AGENT note must not steer today"
        assert "gap-and-go" in docs, "a fresh agent note must survive"
        assert "first 15 minutes" in docs, "HUMAN notes are never filtered, however old"

    def test_no_filter_when_not_requested(self, memory: SemanticMemory) -> None:
        memory.store_note(note_id="a", text="alpha note", source="agent_learning")
        res = memory.query_notes("alpha", n_results=5)
        assert any("alpha" in r["document"] for r in res)


class TestTheToolDefaultsToTheInstrumentBeingTraded:
    def test_query_past_trades_scopes_to_primary_ticker_by_default(self, monkeypatch) -> None:
        import types

        import evotrader.agents.tools as tools

        captured: dict = {}

        class _Mem:
            def query_similar_trades(self, **kw):
                captured.update(kw)
                return []

        monkeypatch.setattr(tools, "_memory", _Mem())
        monkeypatch.setattr(
            tools,
            "_config",
            types.SimpleNamespace(
                settings=types.SimpleNamespace(asset=types.SimpleNamespace(primary_ticker="MSTR"))
            ),
        )
        out = tools.query_past_trades("dip bought in bull stack")
        assert captured["ticker"] == "MSTR"
        assert captured["max_age_days"] == 60
        assert out["scope"] == {"ticker": "MSTR", "max_age_days": 60}

    def test_star_opts_out_of_the_ticker_scope(self, monkeypatch) -> None:
        import evotrader.agents.tools as tools

        captured: dict = {}

        class _Mem:
            def query_similar_trades(self, **kw):
                captured.update(kw)
                return []

        monkeypatch.setattr(tools, "_memory", _Mem())
        tools.query_past_trades("anything", ticker="*", max_age_days=0)
        assert captured["ticker"] is None
        assert captured["max_age_days"] is None
