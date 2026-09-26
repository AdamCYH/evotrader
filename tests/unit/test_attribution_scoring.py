"""The live calibration loop must close: recorded witnesses become scored evidence.

Found 2026-09-18 while answering "does the strategy agent make the result better
or worse?". ``signal_attribution`` had 74 rows across two instruments and **zero**
scored: ``SignalAttributionStore.score()`` had no caller anywhere in the codebase,
and ``backtest/attribution.py`` was imported only by its own package ``__init__``.
The table that exists to answer "which witness actually predicts" had never been
asked, so every evolution cycle reasoned from trade P&L alone — which excludes
the no-trade cycles that are the control group.

The scoring convention is load-bearing and is pinned here:
the base price is the row's own ``price_at_decision``, and the 1-day horizon is
the first session that closes AFTER the decision's date. Counting from the last
available daily bar instead would measure the decision day's own close as a
one-day-ahead return, because the provider's daily series ends at the prior
session intraday — the defect that left ``gap_pct`` null on every live cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evotrader.db.signal_attribution import SignalAttributionStore
from evotrader.evolution.attribution_scorer import (
    AttributionScorer,
    forward_closes,
    returns_for,
)

_DECISION = datetime(2026, 9, 17, 17, 33, tzinfo=UTC)  # 13:33 ET Thursday


def _daily(*closes_by_day: tuple[str, float]) -> list[dict]:
    return [{"timestamp": f"{d}T20:00:00+00:00", "close": c} for d, c in closes_by_day]


# MSTR's actual closes that week: a decision on the 17th, +16% on the 18th.
_MSTR_WEEK = _daily(
    ("2026-09-15", 131.90),
    ("2026-09-16", 126.80),
    ("2026-09-17", 132.25),
    ("2026-09-18", 153.44),
    ("2026-09-21", 150.00),
    ("2026-09-22", 148.00),
    ("2026-09-23", 151.00),
    ("2026-09-24", 145.26),
)


class TestTheHorizonIsSessionsAfterTheDecisionDate:
    def test_one_day_is_the_next_session_close_not_the_decision_days_own(self) -> None:
        closes = forward_closes(_MSTR_WEEK, _DECISION.date())
        assert closes[0] == 153.44, "the 17th's own close must not count as one day forward"
        assert closes[:2] == [153.44, 150.00]

    def test_returns_are_percent_from_price_at_decision(self) -> None:
        got = returns_for(_MSTR_WEEK, _DECISION.date(), base_price=132.60)
        assert got["forward_return_1d"] == pytest.approx((153.44 / 132.60 - 1) * 100, abs=1e-3)
        assert got["forward_return_1d"] == pytest.approx(15.7164, abs=1e-3)
        assert got["forward_return_5d"] == pytest.approx((145.26 / 132.60 - 1) * 100, abs=1e-3)

    def test_a_series_ending_at_the_prior_session_yields_nothing(self) -> None:
        """THE PINNED BUG. Intraday the provider's daily series ends yesterday;
        naive 'last bar + 1' indexing would have invented a forward return."""
        stale = _daily(("2026-09-15", 131.90), ("2026-09-16", 126.80), ("2026-09-17", 132.25))
        assert forward_closes(stale, _DECISION.date()) == []
        assert returns_for(stale, _DECISION.date(), base_price=132.60) == {}

    def test_five_day_is_omitted_until_it_exists(self) -> None:
        got = returns_for(_MSTR_WEEK[:5], _DECISION.date(), base_price=132.60)
        assert "forward_return_1d" in got and "forward_return_5d" not in got


class TestScoringWritesBackToTheTable:
    @pytest.fixture
    def store(self, db) -> SignalAttributionStore:
        return SignalAttributionStore(db)

    async def _aged_row(self, store: SignalAttributionStore, **kw) -> int:
        rid = await store.record("MSTR", price_at_decision=132.60, **kw)
        assert rid is not None
        async with store._db.connection() as conn:
            await conn.execute(
                "UPDATE signal_attribution SET timestamp = ? WHERE id = ?",
                (_DECISION.isoformat(), rid),
            )
            await conn.commit()
        return rid

    async def test_a_pending_row_becomes_a_scored_row(self, store: SignalAttributionStore) -> None:
        """END TO END — this is what had never once happened in production."""
        rid = await self._aged_row(
            store, algo_direction=-1.0, llm_direction=0.0, final_direction=0.0, algo_strength=-0.034
        )
        assert len(await store.scored_rows()) == 0

        async def fetch(ticker: str, now: datetime) -> list[dict]:
            assert ticker == "MSTR"
            return _MSTR_WEEK

        stats = await AttributionScorer(store, fetch).run(now=datetime(2026, 9, 25, tzinfo=UTC))
        assert stats["scored"] == 1

        rows = await store.scored_rows()
        assert len(rows) == 1
        assert rows[0]["id"] == rid
        assert rows[0]["forward_return_1d"] == pytest.approx(15.7164, abs=1e-3)
        # The algorithm was short into a +15.7% day: the record must show it lost.
        assert rows[0]["algo_direction"] * rows[0]["forward_return_1d"] < 0

    async def test_a_row_whose_market_has_not_answered_stays_pending(
        self, store: SignalAttributionStore
    ) -> None:
        await self._aged_row(store, llm_direction=1.0)

        async def fetch(ticker: str, now: datetime) -> list[dict]:
            return _daily(("2026-09-16", 126.80), ("2026-09-17", 132.25))

        # A full day on, so the row IS eligible — but the feed still ends at the
        # decision's own session, so there is nothing to measure against.
        stats = await AttributionScorer(store, fetch).run(now=datetime(2026, 9, 19, tzinfo=UTC))
        assert stats == {"examined": 1, "scored": 0, "waiting": 1, "backfilled_5d": 0}
        assert await store.scored_rows() == []

    async def test_the_five_day_column_is_backfilled_without_erasing_the_one_day(
        self, store: SignalAttributionStore
    ) -> None:
        rid = await self._aged_row(store, llm_direction=1.0)

        async def short_series(ticker: str, now: datetime) -> list[dict]:
            return _MSTR_WEEK[:4]  # only the next session exists yet

        first = await AttributionScorer(store, short_series).run(
            now=datetime(2026, 9, 19, tzinfo=UTC)
        )
        assert first["scored"] == 1
        row = (await store.scored_rows())[0]
        assert row["forward_return_1d"] is not None and row["forward_return_5d"] is None

        async def full_series(ticker: str, now: datetime) -> list[dict]:
            return _MSTR_WEEK

        second = await AttributionScorer(store, full_series).run(
            now=datetime(2026, 9, 25, tzinfo=UTC)
        )
        assert second["backfilled_5d"] == 1
        row = (await store.scored_rows())[0]
        assert row["id"] == rid
        assert row["forward_return_1d"] == pytest.approx(15.7164, abs=1e-3), "must not be erased"
        assert row["forward_return_5d"] == pytest.approx((145.26 / 132.60 - 1) * 100, abs=1e-3)

    async def test_scoring_is_idempotent(self, store: SignalAttributionStore) -> None:
        await self._aged_row(store, llm_direction=1.0)

        async def fetch(ticker: str, now: datetime) -> list[dict]:
            return _MSTR_WEEK

        scorer = AttributionScorer(store, fetch)
        now = datetime(2026, 9, 25, tzinfo=UTC)
        assert (await scorer.run(now=now))["scored"] == 1
        assert (await scorer.run(now=now)) == {
            "examined": 0,
            "scored": 0,
            "waiting": 0,
            "backfilled_5d": 0,
        }

    async def test_a_data_outage_does_not_raise(self, store: SignalAttributionStore) -> None:
        """A missing feed must leave rows pending, never break the trading cycle."""
        await self._aged_row(store, llm_direction=1.0)

        async def broken(ticker: str, now: datetime) -> list[dict]:
            raise RuntimeError("MCP unavailable")

        stats = await AttributionScorer(store, broken).run(now=datetime(2026, 9, 25, tzinfo=UTC))
        assert stats["scored"] == 0 and stats["waiting"] == 1

    async def test_each_instrument_is_fetched_once(self, store: SignalAttributionStore) -> None:
        for _ in range(3):
            await self._aged_row(store, llm_direction=1.0)
        calls: list[str] = []

        async def fetch(ticker: str, now: datetime) -> list[dict]:
            calls.append(ticker)
            return _MSTR_WEEK

        await AttributionScorer(store, fetch).score_pending(now=datetime(2026, 9, 25, tzinfo=UTC))
        assert calls == ["MSTR"], "one fetch per instrument, not per row"


class TestTheCycleActuallyCallsIt:
    def test_main_runs_the_scorer_after_every_cycle(self) -> None:
        """The defect was never the scorer — it was that nothing called one."""
        from pathlib import Path

        src = Path("src/evotrader/main.py").read_text()
        assert "AttributionScorer" in src, "no caller = no calibration data, which is the bug"


class TestTheEvolutionAgentCanReadIt:
    """The scored record has to reach the agent, on both runtimes."""

    @pytest.fixture
    def store(self, db) -> SignalAttributionStore:
        return SignalAttributionStore(db)

    async def _populate(self, store: SignalAttributionStore) -> None:
        """A tape that rose 60% of the time, an algorithm that faded it."""
        base = datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
        for i in range(40):
            up = i % 5 != 0 and i % 3 != 0  # ~53% up, not a clean pattern
            ret = 2.0 if up else -1.5
            rid = await store.record(
                "MSTR",
                algo_direction=-1.0 if up else 1.0,  # systematically wrong
                algo_strength=0.05,
                algo_author="intraday_vwap_zscore (counter-trend)" if up else "momentum",
                news_direction=1.0 if up else -1.0,  # systematically right
                llm_direction=1.0 if up else 0.0,
                final_direction=1.0 if up else 0.0,
                final_conviction=0.8 if up else 0.0,
                price_at_decision=100.0,
            )
            assert rid is not None
            async with store._db.connection() as conn:
                await conn.execute(
                    "UPDATE signal_attribution SET timestamp = ? WHERE id = ?",
                    ((base + timedelta(days=i)).isoformat(), rid),
                )
                await conn.commit()
            await store.score(rid, forward_return_1d=ret, forward_return_5d=ret * 1.5)

    async def test_calibration_tool_separates_a_right_witness_from_a_wrong_one(
        self, store: SignalAttributionStore, monkeypatch
    ) -> None:
        import evotrader.evolution.tools as evo

        monkeypatch.setattr(evo, "_attribution_store", store)
        await self._populate(store)

        out = await evo.get_signal_calibration(lookback_days=365, ticker="MSTR")
        assert out["n_scored"] == 40
        assert out["horizon"] == "forward_return_1d"

        by_name = {w["source"]: w for w in out["witnesses"]}
        assert by_name["algo"]["hit_rate"] == 0.0, "a systematically wrong witness must read 0"
        assert by_name["news"]["hit_rate"] == 1.0
        assert (
            by_name["algo"]["mean_signed_return_pct"]
            < 0
            < by_name["news"]["mean_signed_return_pct"]
        )
        # Baseline is stated so a rising tape cannot masquerade as skill.
        assert 0.5 <= by_name["algo"]["baseline_hit_rate"] <= 1.0

    async def test_it_reports_by_side_and_by_author(
        self, store: SignalAttributionStore, monkeypatch
    ) -> None:
        import evotrader.evolution.tools as evo

        monkeypatch.setattr(evo, "_attribution_store", store)
        await self._populate(store)
        out = await evo.get_signal_calibration(lookback_days=365)

        assert set(out["by_side"]["algo"]) == {"long", "short"}
        authors = {a["author"]: a for a in out["by_author"]}
        assert "intraday_vwap_zscore" in authors, "the author string must be normalised"
        assert authors["intraday_vwap_zscore"]["hit_rate"] == 0.0
        assert (
            "intraday_vwap_zscore (counter-trend)"
            in authors["intraday_vwap_zscore"]["qualifiers_seen"]
        )
        assert all(isinstance(b["n"], int) for b in out["conviction_calibration"])

    async def test_an_empty_record_says_so_instead_of_inventing_statistics(
        self, store: SignalAttributionStore, monkeypatch
    ) -> None:
        import evotrader.evolution.tools as evo

        monkeypatch.setattr(evo, "_attribution_store", store)
        out = await evo.get_signal_calibration()
        assert out["n_scored"] == 0
        assert "witnesses" not in out
        assert "broken" in out["note"]

    async def test_the_result_is_json_serialisable(
        self, store: SignalAttributionStore, monkeypatch
    ) -> None:
        """NaN reaches the model as an unparseable token; it must be None."""
        import json

        import evotrader.evolution.tools as evo

        monkeypatch.setattr(evo, "_attribution_store", store)
        await self._populate(store)
        text = json.dumps(await evo.get_signal_calibration(lookback_days=365))
        assert "NaN" not in text and "Infinity" not in text

    def test_both_runtimes_expose_the_tool(self) -> None:
        """The ADK agent and the Claude CLI agent must not drift apart."""
        from pathlib import Path

        from evotrader.evolution.claude_code_tools import (
            _READ_ONLY_TOOLS,
            evolution_tool_functions,
        )

        assert "get_signal_calibration" in {f.__name__ for f in evolution_tool_functions()}
        assert "get_signal_calibration" in _READ_ONLY_TOOLS
        factory = Path("src/evotrader/agents/factory.py").read_text()
        assert "get_signal_calibration" in factory, "ADK backend must expose it too"


class TestAuthorshipIsGroupedByChannelNotByFreeText:
    """`algo_author` is prose the strategy agent writes each cycle."""

    def test_the_author_is_the_channel_named_first_not_the_longest_match(self) -> None:
        """THE BUG: 'momentum_solo_2of9_mean_reversion_dissenting' was authored
        by momentum and names mean_reversion only as the DISSENTER. Longest-match
        filed it under mean_reversion, inverting the record for both channels."""
        from evotrader.evolution.tools import _base_channel, _known_channel_names

        known = _known_channel_names()
        assert "momentum" in known and "mean_reversion" in known
        assert _base_channel("momentum_solo_2of9_mean_reversion_dissenting", known) == "momentum"
        assert (
            _base_channel("momentum_sole_bull_author_with_qualified_vwap_z_opposing", known)
            == "momentum"
        )
        assert _base_channel("mean_reversion solo, vwap_z abstaining", known) == "mean_reversion"

    def test_qualifiers_do_not_shatter_the_record(self) -> None:
        from evotrader.evolution.tools import _base_channel, _known_channel_names

        known = _known_channel_names()
        live_strings = [
            "momentum",
            "momentum_solo_in_trending_tag",
            "momentum_solo_unqualified",
            "momentum_solo_trending_tag_UNQUALIFIED",
            "momentum_solo_in_trending_tag_UNQUALIFIED_0for8",
        ]
        assert {_base_channel(a, known) for a in live_strings} == {"momentum"}

    def test_a_longer_name_is_not_swallowed_by_a_shorter_one(self) -> None:
        from evotrader.evolution.tools import _base_channel, _known_channel_names

        known = _known_channel_names()
        assert _base_channel("vwap_reclaim_continuation", known) == "vwap_reclaim_continuation"
        assert (
            _base_channel("range_break_continuation (vwap+ibs)", known)
            == "range_break_continuation"
        )

    def test_an_unknown_author_is_kept_verbatim(self) -> None:
        from evotrader.evolution.tools import _base_channel

        assert _base_channel("NOT_SUPPLIED_TAG", []) == "NOT_SUPPLIED_TAG"
        assert _base_channel("", []) == "unattributed"
