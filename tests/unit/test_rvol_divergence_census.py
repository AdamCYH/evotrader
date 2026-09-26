"""Measure things in the units they mean, on the day they describe.

See: data/evolution/reviews/20260923_214957_rvol_is_prior_session_daily_divergence_in_atr_units_channel_vote_census.md
and data/algorithms/v029_momentum_divergence_threshold_in_instrument_units/.

Operator's constraint for this change: generic, nothing hacky. Each fix below
is the instrument- and time-independent form of the problem, and each departs
from the review where the review's version was not:

  1. relative_volume is the PRIOR session's daily ratio (one constant per
     session). It is labelled now, and today's reading is built from a
     time-of-day volume profile — the same "daily value, then live override
     with a label" pattern VWAP already uses. Channels opt in per version.
  2. The momentum divergence threshold is expressed in ATRs of the stock's own
     daily range, not in percent. v029's `divergence_day_change_pct: 3.0` was a
     number chosen for MSTR that would silently break on the next instrument.
  3. Per-channel votes are taken from the system's own stored snapshot, never
     transcribed by the AI agent, so every channel can be scored whether or not
     it led the weighted sum — and all past records can be backfilled.
  4. Stale pending orders are resolved by asking the broker, not by a timeout
     that would wrongly cancel a genuinely long-resting order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evotrader.tools.market_hours import ET

# ═════════════════════════════════════════════════════════════════════════
# Finding 1 — relative volume
# ═════════════════════════════════════════════════════════════════════════


class TestTheDailyReadingIsLabelled:
    def test_compute_indicators_says_what_it_measured(self) -> None:
        """Live: 1.5236 on every cycle of 09-22, 0.8959 on every cycle of 09-23,
        from 08:30 pre-market to 17:00 — the prior session's bar, not today."""
        from evotrader.agents.tools import compute_indicators

        candles = [
            {
                "timestamp": f"2026-08-{d:02d}T20:00:00+00:00",
                "open": 100 + d,
                "high": 102 + d,
                "low": 99 + d,
                "close": 101 + d,
                "volume": 1e6 + d * 1e4,
            }
            for d in range(1, 31)
        ]
        out = compute_indicators(json.dumps(candles))
        assert out["relative_volume_source"] == "prior_session_daily"

    def test_the_model_declares_the_label_and_the_session_reading(self) -> None:
        """Declared, not merely tolerated: the model accepts unknown keys, so a
        construct-and-read test would pass even if the fields did not exist."""
        from evotrader.models.market import TechnicalIndicators

        for field in (
            "relative_volume_source",
            "session_relative_volume",
            "session_relative_volume_sessions",
        ):
            assert field in TechnicalIndicators.model_fields, field

    def test_the_normalizer_passes_them_through(self) -> None:
        from evotrader.agents.tools import normalize_market_snapshot

        out = normalize_market_snapshot(
            {
                "ticker": "MSTR",
                "quote": {"last": 165.0},
                "indicators": {
                    "relative_volume": 0.8959,
                    "relative_volume_source": "prior_session_daily",
                    "session_relative_volume": 1.31,
                    "session_relative_volume_sessions": 12,
                },
            }
        )
        ind = out["indicators"]
        assert ind["relative_volume_source"] == "prior_session_daily"
        assert ind["session_relative_volume"] == 1.31
        assert ind["session_relative_volume_sessions"] == 12


def _session_candles(day: datetime, bars: int, vol_per_bar: float) -> list[dict]:
    """`bars` five-minute regular-session bars from 09:30 ET on `day`."""
    open_et = day.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    return [
        {
            "timestamp": (open_et + timedelta(minutes=5 * i)).astimezone(UTC).isoformat(),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": vol_per_bar,
        }
        for i in range(bars)
    ]


def _warm_profile(
    tmp_path: Path,
    sessions: int,
    vol_per_bar: float = 1000.0,
    end: datetime = datetime(2026, 9, 23, 19, 30, tzinfo=UTC),
) -> None:
    from evotrader.tools.session_volume import record_session_curve

    for k in range(sessions, 0, -1):
        day = end - timedelta(days=k)
        record_session_curve(tmp_path, "MSTR", _session_candles(day, 78, vol_per_bar), now=day)


class TestTheSessionVolumeProfile:
    """Today's cumulative volume vs. the average cumulative volume at the SAME
    minute on prior sessions — volume is U-shaped through the day, so a flat
    'fraction of the day elapsed' estimate would inflate every morning."""

    def test_it_abstains_until_the_profile_is_warm(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import session_relative_volume

        _warm_profile(tmp_path, sessions=9)
        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)  # 11:30 ET
        r = session_relative_volume(tmp_path, "MSTR", _session_candles(now, 24, 1000.0), now=now)
        assert r["value"] is None and r["sessions"] == 9

    def test_a_normal_day_reads_one(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import session_relative_volume

        _warm_profile(tmp_path, sessions=10)
        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        r = session_relative_volume(tmp_path, "MSTR", _session_candles(now, 24, 1000.0), now=now)
        assert r["value"] == pytest.approx(1.0) and r["sessions"] == 10

    def test_a_busy_morning_reads_high(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import session_relative_volume

        _warm_profile(tmp_path, sessions=10)
        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        r = session_relative_volume(tmp_path, "MSTR", _session_candles(now, 24, 2500.0), now=now)
        assert r["value"] == pytest.approx(2.5)

    def test_it_compares_like_minute_with_like_minute(self, tmp_path: Path) -> None:
        """Prior sessions heavy at the open, light later. At 10:00 today the
        comparison must use their 10:00 cumulative, not their daily total."""
        from evotrader.tools.session_volume import record_session_curve, session_relative_volume

        end = datetime(2026, 9, 23, 19, 30, tzinfo=UTC)
        for k in range(10, 0, -1):
            day = end - timedelta(days=k)
            c = _session_candles(day, 78, 100.0)
            for b in c[:6]:
                b["volume"] = 5000.0  # heavy first half hour
            record_session_curve(tmp_path, "MSTR", c, now=day)
        now = end.replace(hour=14, minute=0)  # 10:00 ET
        today = _session_candles(now, 6, 5000.0)
        r = session_relative_volume(tmp_path, "MSTR", today, now=now)
        assert r["value"] == pytest.approx(1.0), "same shape as a normal open"

    def test_today_is_never_its_own_baseline(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import record_session_curve, session_relative_volume

        _warm_profile(tmp_path, sessions=10)
        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        today = _session_candles(now, 24, 3000.0)
        record_session_curve(tmp_path, "MSTR", today, now=now)  # recorded BEFORE reading
        r = session_relative_volume(tmp_path, "MSTR", today, now=now)
        assert r["value"] == pytest.approx(3.0)

    def test_re_recording_a_session_replaces_it(self, tmp_path: Path) -> None:
        """Each cycle re-records today's longer curve; it must not duplicate."""
        from evotrader.tools.session_volume import record_session_curve, sessions_in_profile

        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        record_session_curve(tmp_path, "MSTR", _session_candles(now, 12, 1000.0), now=now)
        record_session_curve(tmp_path, "MSTR", _session_candles(now, 24, 1000.0), now=now)
        assert sessions_in_profile(tmp_path, "MSTR") == 1

    def test_no_bars_means_no_reading(self, tmp_path: Path) -> None:
        """The 08:30 and 17:00 cycles have no regular-session bars."""
        from evotrader.tools.session_volume import session_relative_volume

        _warm_profile(tmp_path, sessions=10)
        now = datetime(2026, 9, 23, 12, 30, tzinfo=UTC)
        assert session_relative_volume(tmp_path, "MSTR", [], now=now)["value"] is None

    def test_a_write_failure_never_raises(self, tmp_path: Path) -> None:
        from evotrader.tools.session_volume import record_session_curve

        (tmp_path / "trading").write_text("not a directory")
        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        assert (
            record_session_curve(tmp_path, "MSTR", _session_candles(now, 3, 1.0), now=now) is False
        )

    def test_the_market_data_tool_records_and_reads_it(self) -> None:
        src = Path("src/evotrader/agents/tools.py").read_text()
        assert "record_session_curve" in src and "session_relative_volume(" in src


class TestOneHelperDecidesWhichVolumeReading:
    def _ind(self, **kw):
        from evotrader.models.market import TechnicalIndicators

        return TechnicalIndicators(
            relative_volume=0.8959, relative_volume_source="prior_session_daily", **kw
        )

    def test_daily_mode_is_todays_behaviour(self) -> None:
        from evotrader.indicators.volume import select_relative_volume

        assert select_relative_volume(self._ind(session_relative_volume=2.0), "daily") == (
            0.8959,
            "prior_session_daily",
        )

    def test_session_mode_uses_the_live_reading_when_warm(self) -> None:
        from evotrader.indicators.volume import select_relative_volume

        assert select_relative_volume(self._ind(session_relative_volume=2.0), "session") == (
            2.0,
            "session_profile",
        )

    def test_session_mode_falls_back_visibly_while_warming(self) -> None:
        from evotrader.indicators.volume import select_relative_volume

        value, source = select_relative_volume(self._ind(), "session")
        assert value == 0.8959 and source == "prior_session_daily_fallback"


class TestChannelsLabelTheirVolumeAndDefaultToNoChange:
    """`rvol_source` defaults to 'daily' so every existing version keeps its
    exact behaviour; a later version opts in once the profile is warm."""

    def _snapshot(self, **ind_extra):
        from evotrader.models.market import (
            MarketRegime,
            MarketSnapshot,
            Quote,
            RegimeClassification,
            TechnicalIndicators,
        )

        now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        return MarketSnapshot(
            ticker="MSTR",
            timestamp=now,
            quote=Quote(
                ticker="MSTR",
                bid=165.1,
                ask=165.2,
                last=165.16,
                volume=0,
                timestamp=now,
                previous_close=167.33,
            ),
            indicators=TechnicalIndicators(
                ema_9=164.0,
                ema_21=160.0,
                sma_20=158.0,
                sma_50=150.0,
                macd_line=2.0,
                macd_signal=1.0,
                macd_histogram=1.0,
                atr_14=9.5232,
                relative_volume=0.8959,
                relative_volume_source="prior_session_daily",
                **ind_extra,
            ),
            regime=RegimeClassification(
                regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
            ),
            daily_change_pct=-1.2939,
        )

    @pytest.mark.parametrize(
        "module,cls",
        [
            ("momentum", "MomentumStrategy"),
            ("intraday_vwap_zscore", "IntradayVwapZscoreStrategy"),
            ("vwap_reclaim_continuation", "VwapReclaimContinuationStrategy"),
        ],
    )
    def test_default_is_daily_everywhere(self, module: str, cls: str) -> None:
        import importlib

        klass = getattr(importlib.import_module(f"evotrader.algorithms.strategies.{module}"), cls)
        assert klass().get_parameters()["rvol_source"] == "daily"

    def test_momentum_value_is_unchanged_by_a_session_reading_it_did_not_opt_into(self) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        plain = MomentumStrategy().compute_signal(self._snapshot())
        with_session = MomentumStrategy().compute_signal(
            self._snapshot(session_relative_volume=3.0)
        )
        assert plain.value == with_session.value
        assert with_session.metadata["rvol_source"] == "prior_session_daily"

    def test_momentum_uses_the_session_reading_when_opted_in(self) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        sig = MomentumStrategy(rvol_source="session").compute_signal(
            self._snapshot(session_relative_volume=2.0)
        )
        assert sig.metadata["rvol_source"] == "session_profile"
        assert sig.metadata["volume_multiplier_raw"] == pytest.approx(1.0)  # volume_signal(2.0)


# ═════════════════════════════════════════════════════════════════════════
# Finding 2 — the divergence threshold in ATRs
# ═════════════════════════════════════════════════════════════════════════

# (label, day_change_pct, previous_close, atr_14, applied at 0.5 ATR)
_LIVE = [
    ("09-22 15:30", +0.1721, 168.50, 9.8029, False),  # same direction as the trend
    ("09-22 17:00", -0.7537, 168.50, 9.8029, False),  # 0.13 ATR
    ("09-23 08:30", -0.9442, 167.33, 9.4815, False),  # 0.17 ATR
    ("09-23 09:30", -0.1524, 167.33, 9.3868, False),  # 0.03 ATR
    ("09-23 10:30", -2.9552, 167.33, 9.7218, True),  # 0.51 ATR — v029's 3.0% misses it
    ("09-23 11:30", -1.2939, 167.33, 9.5232, False),  # 0.23 ATR
    ("09-23 13:30", -2.6026, 167.33, 9.6797, False),  # 0.45 ATR
    ("09-23 14:30", -2.9821, 167.33, 9.7250, True),  # 0.51 ATR — v029's 3.0% misses it
    ("09-23 17:00", -3.4363, 167.33, 9.7793, True),  # 0.59 ATR
]


def _momentum_snapshot(day_change: float, prev_close: float, atr: float | None):
    from evotrader.models.market import (
        MarketRegime,
        MarketSnapshot,
        Quote,
        RegimeClassification,
        TechnicalIndicators,
    )

    now = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
    px = prev_close * (1 + day_change / 100)
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=now,
        quote=Quote(
            ticker="MSTR",
            bid=px - 0.02,
            ask=px + 0.02,
            last=px,
            volume=0,
            timestamp=now,
            previous_close=prev_close,
        ),
        indicators=TechnicalIndicators(
            ema_9=164.0,
            ema_21=160.0,
            sma_20=158.0,
            sma_50=150.0,
            macd_line=2.0,
            macd_signal=1.0,
            macd_histogram=1.0,
            atr_14=atr,
            relative_volume=0.8959,
            relative_volume_source="prior_session_daily",
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
        ),
        daily_change_pct=day_change,
    )


class TestDivergenceInAtrs:
    @pytest.mark.parametrize("label,dc,pc,atr,expected", _LIVE)
    def test_the_live_cycles(self, label, dc, pc, atr, expected) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        sig = MomentumStrategy(divergence_day_change_atr=0.5).compute_signal(
            _momentum_snapshot(dc, pc, atr)
        )
        assert sig.metadata["divergence_applied"] is expected, label
        assert sig.metadata["divergence_threshold_source"] == "atr"
        assert sig.metadata["divergence_atr_observed"] == pytest.approx(
            abs(dc / 100 * pc) / atr, abs=1e-3
        )

    def test_it_fires_on_about_one_in_three_red_cycles(self) -> None:
        """v029's own stated expectation ("ON on ~1 in 3 red cycles") — met by
        the ATR form, not by v029's own 3.0%. Run through the real strategy."""
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        red = [c for c in _LIVE if c[1] < 0]
        atr = MomentumStrategy(divergence_day_change_atr=0.5)
        pct = MomentumStrategy(divergence_day_change_pct=3.0)
        atr_hits = sum(
            atr.compute_signal(_momentum_snapshot(dc, pc, a)).metadata["divergence_applied"]
            for _, dc, pc, a, _ in red
        )
        pct_hits = sum(
            pct.compute_signal(_momentum_snapshot(dc, pc, a)).metadata["divergence_applied"]
            for _, dc, pc, a, _ in red
        )
        assert (atr_hits, pct_hits, len(red)) == (3, 1, 8)

    def test_the_same_half_atr_move_fires_on_any_instrument(self) -> None:
        """Generic: a half-ATR move against the trend fires whether ATR is 1%
        of price (QQQ-like) or 6% (MSTR-like). A percent threshold cannot."""
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        s = MomentumStrategy(divergence_day_change_atr=0.5)
        qqq = s.compute_signal(_momentum_snapshot(-0.55, 700.0, 7.0))  # 0.55 ATR, 0.55%
        mstr = s.compute_signal(_momentum_snapshot(-3.30, 167.0, 10.0))  # 0.55 ATR, 3.3%
        assert qqq.metadata["divergence_applied"] and mstr.metadata["divergence_applied"]

    def test_without_atr_it_falls_back_to_percent_and_says_so(self) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        sig = MomentumStrategy(
            divergence_day_change_atr=0.5, divergence_day_change_pct=0.6
        ).compute_signal(_momentum_snapshot(-0.94, 167.33, None))
        assert sig.metadata["divergence_threshold_source"] == "pct"
        assert sig.metadata["divergence_applied"] is True

    def test_identity_default_keeps_every_existing_version_unchanged(self) -> None:
        """No `divergence_day_change_atr` in config -> today's percent test,
        so v028 and earlier behave exactly as they did."""
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        assert MomentumStrategy().get_parameters()["divergence_day_change_atr"] is None
        sig = MomentumStrategy(divergence_day_change_pct=0.6).compute_signal(
            _momentum_snapshot(-0.9442, 167.33, 9.4815)
        )
        assert sig.metadata["divergence_threshold_source"] == "pct"
        assert sig.metadata["divergence_applied"] is True  # v028's live reading at 08:30

    def test_validation(self) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        assert s.validate_parameters({"divergence_day_change_atr": 0.5}) == []
        assert s.validate_parameters({"divergence_day_change_atr": None}) == []
        assert s.validate_parameters({"divergence_day_change_atr": 0.0})
        assert s.validate_parameters({"divergence_day_change_atr": 4.0})


class TestTheShippedVersionIsTheGenericForm:
    """v029 moved the threshold into ATRs; the version a new user starts with
    carries the same generic form."""

    def test_it_sets_the_threshold_in_atrs(self, active_algorithm_config) -> None:
        mo = active_algorithm_config["momentum"]
        assert mo["divergence_day_change_atr"] == 0.5
        assert mo["divergence_day_change_pct"] == 0.6, (
            "percent is only the no-ATR fallback, unchanged"
        )

    def test_the_composite_builds(self, starter_data_dir, active_algorithm_config) -> None:
        from evotrader.algorithms.loader import StrategyLoader

        comp = StrategyLoader(
            starter_data_dir / "algorithms" / "strategy_manifest.yaml"
        ).build_composite(active_algorithm_config)
        assert comp._strategies["momentum"].get_parameters()["divergence_day_change_atr"] == 0.5


# ═════════════════════════════════════════════════════════════════════════
# Finding 3 — score every channel, not only the one that led
# ═════════════════════════════════════════════════════════════════════════

_SUBS_0923_1130 = [
    {"name": "momentum", "value": 0.2741, "weight": 0.2045, "metadata": {}},
    {"name": "mean_reversion", "value": -0.244, "weight": 0.129, "metadata": {}},
    {
        "name": "swing_failure_reversal",
        "value": 0.0453,
        "weight": 0.129,
        "metadata": {"applicable": True, "reason": "confirmed_reversal"},
    },
    {
        "name": "options_positioning",
        "value": 0.0,
        "weight": 0.086,
        "metadata": {"applicable": False, "reason": "no_options_data"},
    },
]


class TestChannelVotesComeFromTheSystemsOwnRecord:
    async def _row_with_snapshot(self, db, session: str, when: datetime):
        from evotrader.db.signal_attribution import SignalAttributionStore

        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO market_snapshots (timestamp, session_id, ticker, close_price, "
                "composite_signal, regime, sub_signals_json) VALUES (?,?,?,?,?,?,?)",
                (
                    when.isoformat(),
                    session,
                    "MSTR",
                    165.16,
                    0.0658,
                    "trending_bull",
                    json.dumps(_SUBS_0923_1130),
                ),
            )
        store = SignalAttributionStore(db)
        rid = await store.record(
            "MSTR",
            session_id=session,
            algo_author="momentum(+0.274)+swing",
            algo_direction=1.0,
            price_at_decision=165.16,
        )
        return store, rid

    async def test_backfill_attaches_every_channel(self, db) -> None:
        """THE CASE. 09-23 11:30: swing_failure's first-ever confirmed reversal
        (+0.045) sat under momentum's +0.274 and was invisible to by_author."""
        store, rid = await self._row_with_snapshot(
            db, "909c6484", datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        )
        assert await store.backfill_channel_votes() == 1
        rows = [r async for r in _all(store)]
        votes = json.loads(next(r for r in rows if r["id"] == rid)["channel_votes"])
        assert votes["swing_failure_reversal"]["value"] == pytest.approx(0.0453)
        assert votes["swing_failure_reversal"]["reason"] == "confirmed_reversal"
        assert votes["options_positioning"]["applicable"] is False

    async def test_backfill_is_idempotent(self, db) -> None:
        store, _ = await self._row_with_snapshot(
            db, "s2", datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
        )
        assert await store.backfill_channel_votes() == 1
        assert await store.backfill_channel_votes() == 0

    async def test_a_row_without_a_snapshot_is_left_alone(self, db) -> None:
        from evotrader.db.signal_attribution import SignalAttributionStore

        store = SignalAttributionStore(db)
        await store.record("MSTR", session_id="orphan", price_at_decision=1.0)
        assert await store.backfill_channel_votes() == 0

    def test_the_agent_is_not_asked_to_transcribe_votes(self) -> None:
        """Ten numbers copied by an LLM is the fragile form; the tool doc must
        not ask for them."""
        from evotrader.agents.tools import log_signal_attribution

        assert "channel_votes" not in (log_signal_attribution.__doc__ or "")

    def test_the_cycle_backfills_before_scoring(self) -> None:
        src = Path("src/evotrader/main.py").read_text()
        assert "backfill_channel_votes" in src


async def _all(store):
    async with store._db.connection() as conn:
        cur = await conn.execute("SELECT * FROM signal_attribution")
        for r in await cur.fetchall():
            yield dict(r)


class TestTheCalibrationReportScoresEveryChannel:
    async def test_by_channel_scores_a_minority_vote(self, db, monkeypatch) -> None:
        """A channel that never led the weighted sum still gets a record."""
        import evotrader.evolution.tools as evo
        from evotrader.db.signal_attribution import SignalAttributionStore

        store = SignalAttributionStore(db)
        base = datetime(2026, 8, 1, 15, 30, tzinfo=UTC)
        for i in range(12):
            up = i % 3 != 0
            rid = await store.record(
                "MSTR",
                session_id=f"s{i}",
                algo_author="momentum",
                algo_direction=1.0,
                price_at_decision=100.0,
            )
            votes = {
                "momentum": {"value": 0.27, "weight": 0.2, "applicable": True, "reason": None},
                "swing_failure_reversal": {
                    "value": 0.05 if up else 0.0,
                    "weight": 0.13,
                    "applicable": True,
                    "reason": None,
                },
            }
            async with db.transaction() as conn:
                await conn.execute(
                    "UPDATE signal_attribution SET timestamp=?, channel_votes=? WHERE id=?",
                    ((base + timedelta(days=i)).isoformat(), json.dumps(votes), rid),
                )
            await store.score(rid, forward_return_1d=1.5 if up else -1.0, forward_return_5d=None)

        monkeypatch.setattr(evo, "_attribution_store", store)
        out = await evo.get_signal_calibration(lookback_days=365, ticker="MSTR")
        by = {c["channel"]: c for c in out["by_channel"]}
        assert "swing_failure_reversal" in by, "invisible to by_author, visible here"
        assert by["swing_failure_reversal"]["n_calls"] == 8
        assert by["swing_failure_reversal"]["hit_rate"] == pytest.approx(1.0)
        assert by["momentum"]["n_calls"] == 12


# ═════════════════════════════════════════════════════════════════════════
# Finding 4 — the broker decides whether a pending order is still pending
# ═════════════════════════════════════════════════════════════════════════


class TestEveryPendingJournalRowIsCheckedAgainstTheBroker:
    async def test_an_order_missing_from_the_watch_list_is_still_checked(self, db) -> None:
        """A take-profit from 09-14 has a broker order id, is PENDING in
        the journal, and was never on the watch list — so reconciliation has
        not asked the broker about it for nine sessions."""
        from evotrader.db.journal import TradeJournal
        from evotrader.models.trade import OrderType, TradeAction, TradeDirection, TradeProposal

        j = TradeJournal(db)
        await j.record_trade(
            TradeProposal(
                ticker="MSTR",
                direction=TradeDirection.LONG,
                action=TradeAction.TAKE_PROFIT,
                quantity=1.0,
                order_type=OrderType.LIMIT,
                limit_price=145.0,
            ),
            order_status="PENDING",
            order_id="6aa83111",
        )
        await j.save_pending_order("6ab12222", "{}")
        ids = {r["order_id"] for r in await j.get_order_ids_awaiting_broker()}
        assert {"6aa83111", "6ab12222"} <= ids

    async def test_a_resolved_order_is_not_rechecked(self, db) -> None:
        from evotrader.db.journal import TradeJournal

        j = TradeJournal(db)
        await j.save_pending_order("done", "{}")
        await j.resolve_pending_order("done", "FILLED")
        assert "done" not in {r["order_id"] for r in await j.get_order_ids_awaiting_broker()}

    def test_there_is_no_time_based_expiry(self) -> None:
        """A far gtc take-profit can legitimately rest for weeks; an age cutoff
        would cancel it in the journal while it is still live at the broker."""
        src = Path("src/evotrader/agents/tools.py").read_text()
        assert "expired_by_reconciliation" not in src
        assert "get_order_ids_awaiting_broker" in src


class TestTheNormalizerFollowsTheModel:
    """The normalizer forced every indicator to float except a hand-kept list
    of text fields, so `relative_volume_source` was silently nulled. It now
    reads each field's type from TechnicalIndicators."""

    def test_every_declared_field_keeps_its_type(self) -> None:
        import typing

        from evotrader.agents.tools import _indicator_field_kinds
        from evotrader.models.market import TechnicalIndicators

        kinds = _indicator_field_kinds()
        assert set(kinds) == set(TechnicalIndicators.model_fields)
        assert kinds["vwap_anchor"] is str and kinds["event_type"] is str
        assert kinds["relative_volume_source"] is str
        assert kinds["session_relative_volume_sessions"] is int
        assert kinds["rsi_14"] is float
        _ = typing  # imported for clarity of intent

    def test_existing_fields_are_unchanged(self) -> None:
        from evotrader.agents.tools import normalize_market_snapshot

        out = normalize_market_snapshot(
            {
                "ticker": "MSTR",
                "quote": {"last": 165.0},
                "indicators": {"rsi_14": "52", "vwap_anchor": "current_session"},
            }
        )
        assert out["indicators"]["rsi_14"] == 52.0
        assert out["indicators"]["vwap_anchor"] == "current_session"


class TestSmallSamplesAreNotReportedAsZeroPercent:
    async def test_a_one_call_channel_reads_none_not_zero(self, db, monkeypatch) -> None:
        """score_source returns 0.0 placeholders below 3 calls; reported raw that
        read as 'right 0% of the time' on the live table for four channels."""
        import evotrader.evolution.tools as evo
        from evotrader.db.signal_attribution import SignalAttributionStore

        store = SignalAttributionStore(db)
        base = datetime(2026, 8, 1, 15, 30, tzinfo=UTC)
        for i in range(6):
            rid = await store.record(
                "MSTR", session_id=f"s{i}", algo_direction=1.0, price_at_decision=100.0
            )
            votes = {"momentum": {"value": 0.3}, "gap": {"value": 0.2 if i == 0 else 0.0}}
            async with db.transaction() as conn:
                await conn.execute(
                    "UPDATE signal_attribution SET timestamp=?, channel_votes=? WHERE id=?",
                    ((base + timedelta(days=i)).isoformat(), json.dumps(votes), rid),
                )
            await store.score(rid, forward_return_1d=1.0, forward_return_5d=None)
        monkeypatch.setattr(evo, "_attribution_store", store)
        by = {
            c["channel"]: c
            for c in (await evo.get_signal_calibration(lookback_days=365))["by_channel"]
        }
        assert by["gap"]["n_calls"] == 1
        assert by["gap"]["hit_rate"] is None and "fewer than 3" in by["gap"]["note"]
        assert by["momentum"]["hit_rate"] == pytest.approx(1.0)
