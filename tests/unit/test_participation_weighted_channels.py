"""Participation counts the channels that carry weight, and counts early ones as on duty.

See: data/evolution/reviews/
20261005_211602_participation_attenuation_counts_zero_weight_shadow_channels.md
(findings 1, 2 and 3)

The low-participation attenuation scales the composite down when few channels
corroborate it: below 0.4 of the channels on duty voting, the composite is
multiplied by (voting / on duty) / 0.4. It counted every channel, including the
shadow channels that carry weight 0.0 in the regime (gap,
vwap_reclaim_continuation and gap_fail_continuation in every regime of the
starter algorithm). They add nothing to the weighted mean, but they moved the
scale: a silent shadow channel counted as an absent witness, a voting one as a
corroborating witness.

Four hourly cycles of one made-up session, with the starter algorithm's
trending_bull weights, on the old count and the corrected one:

    10:30  gap (weight 0) votes           3 of 7, no scale       +0.2053 -> +0.2053, 2 of 5
    11:30  gap silent again               2 of 8, scale 0.625    +0.1257 -> +0.2011, 2 of 5
    12:30  intraday_vwap_zscore fires     3 of 8, scale 0.9375   +0.3794 -> +0.4047, 3 of 5
    14:30  vwap_reclaim (weight 0) votes  3 of 8, scale 0.9375   +0.1881 -> +0.2007, 2 of 5

On the old count the composite fell by 39% from 10:30 to 11:30 with every
weighted input nearly unchanged, because a channel with no authority went
quiet. Silent shadow channels also sat in the 12:30 denominator. Counted over
the weighted channels, with swing_failure_reversal on duty while it collects
its 20 bars at 10:30 (base.warming_up), all four read N of 5 and none is scaled.
"""

from __future__ import annotations

import ast
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evotrader.algorithms import composite as composite_module
from evotrader.algorithms.base import TradingAlgorithm, warming_up
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.momentum import MACD_NEUTRAL_ATR, MomentumStrategy
from evotrader.algorithms.strategies.swing_failure_reversal import SwingFailureReversalStrategy
from evotrader.algorithms.strategies.vwap_reclaim_continuation import (
    VwapReclaimContinuationStrategy,
)
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal

# The starter algorithm's trending_bull weights (they sum to 1).
_W = {
    "momentum": 0.2045,
    "mean_reversion": 0.129,
    "gap": 0.0,
    "intraday_vwap_zscore": 0.1935,
    "event_window_timing": 0.0215,
    "range_break_continuation": 0.1075,
    "options_positioning": 0.086,
    "swing_failure_reversal": 0.129,
    "trend_persistence": 0.129,
    "vwap_reclaim_continuation": 0.0,
    "gap_fail_continuation": 0.0,
}
_SHADOWS = ("gap", "vwap_reclaim_continuation", "gap_fail_continuation")

# How each channel reported itself: voted, in scope and silent, applicable but
# off duty, not applicable, or not applicable while collecting its bars.
VOTE, IN, OFF, NA, WARM = "vote", "in", "off", "na", "warm"

_QUIET = {
    "intraday_vwap_zscore": (0.0, IN),
    "event_window_timing": (0.0, NA),
    "range_break_continuation": (0.0, OFF),
    "options_positioning": (0.0, NA),
    "swing_failure_reversal": (0.0, IN),
    "trend_persistence": (0.0, IN),
    "gap": (0.0, IN),
    "vwap_reclaim_continuation": (0.0, IN),
    "gap_fail_continuation": (0.0, IN),
}

# (composite on the old count, {channel: (value, status)}). At 10:30
# swing_failure_reversal has 12 of the 20 five-minute bars it needs.
_CYCLES: dict[str, tuple[float, dict[str, tuple[float, str]]]] = {
    "10:30": (
        0.2053,
        {
            **_QUIET,
            "momentum": (+0.36, VOTE),
            "mean_reversion": (-0.04, VOTE),
            "gap": (-0.50, VOTE),
            "swing_failure_reversal": (0.0, WARM),
        },
    ),
    "11:30": (
        0.1257,
        {**_QUIET, "momentum": (+0.35, VOTE), "mean_reversion": (-0.035, VOTE)},
    ),
    "12:30": (
        0.3794,
        {
            **_QUIET,
            "momentum": (+0.33, VOTE),
            "mean_reversion": (-0.025, VOTE),
            "intraday_vwap_zscore": (+0.77, VOTE),
        },
    ),
    "14:30": (
        0.1881,
        {
            **_QUIET,
            "momentum": (+0.35, VOTE),
            "mean_reversion": (-0.036, VOTE),
            "vwap_reclaim_continuation": (+0.41, VOTE),
        },
    ),
}

# The corrected engine on the same votes: (composite, voting, on duty).
_CORRECTED = {
    "10:30": (0.2053, 2, 5),
    "11:30": (0.2011, 2, 5),
    "12:30": (0.4047, 3, 5),
    "14:30": (0.2007, 2, 5),
}


class _Stub(TradingAlgorithm):
    def __init__(self, name: str, value: float, status: str) -> None:
        self._name, self._value, self._status = name, value, status

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "fixture"

    def compute_signal(self, snapshot) -> AlgoSignal:
        meta: dict = {}
        if self._status == OFF:
            meta["in_scope"] = False
        elif self._status == NA:
            meta["applicable"] = False
        elif self._status == WARM:
            meta.update(applicable=False, warming_up=True)
        return AlgoSignal(name=self._name, value=self._value, weight=1.0, metadata=meta)

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        return None

    def validate_parameters(self, params: dict) -> list[str]:
        return []


def _snapshot():
    return types.SimpleNamespace(regime=types.SimpleNamespace(regime=MarketRegime.TRENDING_BULL))


def _composite(channels: dict[str, tuple[float, str]], weights: dict[str, float] | None = None):
    weights = dict(_W if weights is None else weights)
    return CompositeStrategy(
        sub_strategies={n: _Stub(n, v, s) for n, (v, s) in channels.items()},
        weights={n: weights[n] for n in channels},
        regime_adaptive=False,
        version="fixture",
    )


def _detailed(channels: dict[str, tuple[float, str]], weights: dict[str, float] | None = None):
    return _composite(channels, weights).compute_detailed_signal(_snapshot())


def _with(cycle: str, **changes: tuple[float, str]) -> dict[str, tuple[float, str]]:
    return {**_CYCLES[cycle][1], **changes}


class TestTheCyclesOnTheCorrectedEngine:
    @pytest.mark.parametrize("cycle", list(_CYCLES))
    def test_each_cycle(self, cycle: str) -> None:
        composite, voting, duty = _CORRECTED[cycle]
        sig = _detailed(_CYCLES[cycle][1])
        assert sig.composite_value == pytest.approx(composite, abs=5e-5)
        assert (sig.participation_numerator, sig.participation_denominator) == (voting, duty)
        assert sig.participation_scale == pytest.approx(1.0)
        assert sig.composite_value == pytest.approx(sig.composite_unattenuated, abs=1e-12)

    def test_the_eleven_thirty_drop_is_gone(self) -> None:
        """10:30 to 11:30 now moves by what the weighted inputs moved, -0.004,
        not by the -0.080 that a shadow channel's silence produced."""
        before = _detailed(_CYCLES["10:30"][1]).composite_value
        after = _detailed(_CYCLES["11:30"][1]).composite_value
        assert after - before == pytest.approx(0.2011 - 0.2053, abs=1e-4)
        old_drop = _CYCLES["11:30"][0] - _CYCLES["10:30"][0]
        assert old_drop == pytest.approx(-0.0796, abs=1e-4)

    def test_the_review_cycles_all_read_the_same_count(self) -> None:
        """The review's expectation: 10:30, 11:30 and 14:30 read two votes of
        the same denominator with the same scale."""
        counts = {
            c: (
                _detailed(_CYCLES[c][1]).participation_numerator,
                _detailed(_CYCLES[c][1]).participation_denominator,
                _detailed(_CYCLES[c][1]).participation_scale,
            )
            for c in ("10:30", "11:30", "14:30")
        }
        assert set(counts.values()) == {(2, 5, 1.0)}

    def test_twelve_thirty_was_scaled_by_silent_shadows(self) -> None:
        """The review expected 12:30 to keep its scale; it held three silent
        shadow channels in the denominator (3 of 8), so it does not."""
        sig = _detailed(_CYCLES["12:30"][1])
        assert (sig.participation_numerator, sig.participation_denominator) == (3, 5)
        assert sig.n_in_scope == 8  # every channel in scope, shadows included
        assert sig.composite_value > _CYCLES["12:30"][0]

    @pytest.mark.parametrize("cycle", ["11:30", "12:30", "14:30"])
    def test_weighting_the_shadows_restores_the_old_numbers(self, cycle: str) -> None:
        """The only difference between the two counts is whether a weight-0
        channel is counted: give the shadows a negligible weight and the old
        composite comes back to four decimals."""
        weights = {**_W, **dict.fromkeys(_SHADOWS, 1e-9)}
        sig = _detailed(_CYCLES[cycle][1], weights)
        assert sig.composite_value == pytest.approx(_CYCLES[cycle][0], abs=5e-5)


class TestAShadowChannelChangesNothing:
    @pytest.mark.parametrize("cycle", list(_CYCLES))
    @pytest.mark.parametrize("value", [-0.9, -0.3, 0.0, 0.4, 0.9])
    def test_whatever_a_shadow_emits(self, cycle: str, value: float) -> None:
        base = _detailed(_CYCLES[cycle][1])
        for shadow in _SHADOWS:
            moved = _detailed(_with(cycle, **{shadow: (value, VOTE if value else IN)}))
            assert moved.composite_value == pytest.approx(base.composite_value, abs=1e-12)
            assert moved.participation_scale == pytest.approx(base.participation_scale)
            assert moved.participation_denominator == base.participation_denominator

    def test_or_whether_it_is_on_duty_at_all(self) -> None:
        base = _detailed(_CYCLES["11:30"][1])
        for status in (OFF, NA, WARM):
            moved = _detailed(_with("11:30", gap=(0.0, status)))
            assert moved.composite_value == pytest.approx(base.composite_value, abs=1e-12)
            assert moved.participation_denominator == base.participation_denominator


class TestBelowTheLine:
    """The scale still applies when weighted corroboration is thin."""

    def _thin(self, **changes: tuple[float, str]):
        # momentum alone: mean_reversion silent.
        return _detailed(_with("11:30", mean_reversion=(0.0, IN), **changes))

    def test_one_of_five_is_scaled_by_half(self) -> None:
        sig = self._thin()
        assert (sig.participation_numerator, sig.participation_denominator) == (1, 5)
        assert sig.participation_scale == pytest.approx((1 / 5) / 0.4)
        assert sig.composite_value == pytest.approx(sig.composite_unattenuated * 0.5, abs=1e-12)

    def test_a_voting_shadow_does_not_lift_it(self) -> None:
        sig = self._thin(gap=(-0.6, VOTE), vwap_reclaim_continuation=(0.5, VOTE))
        assert sig.participation_scale == pytest.approx(0.5)


class TestWarmingUpCounts:
    def test_an_early_weighted_channel_is_on_duty(self) -> None:
        with_warming = _detailed(_CYCLES["10:30"][1])
        without = _detailed(_with("10:30", swing_failure_reversal=(0.0, NA)))
        assert with_warming.participation_denominator == 5
        assert without.participation_denominator == 4

    def test_the_same_votes_scale_alike_before_and_after_the_bars_arrive(self) -> None:
        """One vote at 10:30 (swing still collecting bars) and at 11:30 (swing
        reading): without the warming-up count these scaled 0.625 and 0.5."""
        early = _detailed(
            _with("10:30", mean_reversion=(0.0, IN), gap=(0.0, IN))
        ).participation_scale
        later = _detailed(_with("11:30", mean_reversion=(0.0, IN))).participation_scale
        assert early == pytest.approx(later) == pytest.approx(0.5)

    def test_a_shadow_warming_up_is_not_counted(self) -> None:
        base = _detailed(_CYCLES["11:30"][1])
        sig = _detailed(_with("11:30", vwap_reclaim_continuation=(0.0, WARM)))
        assert sig.participation_denominator == base.participation_denominator

    def test_a_channel_without_a_feed_is_not_counted(self) -> None:
        """options_positioning has no data at all: still out, as before."""
        sig = _detailed(_CYCLES["11:30"][1])
        assert sig.participation_denominator == 5  # options and event_window excluded

    def test_the_names_are_reported(self) -> None:
        meta = _composite(_CYCLES["10:30"][1]).compute_signal(_snapshot()).metadata
        assert meta["warming_up_signals"] == "swing_failure_reversal"
        assert meta["participation_numerator"] == 2
        assert meta["participation_denominator"] == 5
        assert meta["participation_scale"] == pytest.approx(1.0)


class TestNoWeightedChannelOnDuty:
    def test_falls_back_to_every_channel(self) -> None:
        """Weights only on a channel that cannot speak: the composite is zero
        whatever the scale, and the counts are the old ones."""
        channels = {"a": (0.0, NA), "shadow": (0.5, VOTE), "quiet": (0.0, IN)}
        sig = _detailed(channels, {"a": 1.0, "shadow": 0.0, "quiet": 0.0})
        assert sig.composite_value == pytest.approx(0.0)
        assert (sig.participation_numerator, sig.participation_denominator) == (1, 2)


class TestTheCountsAreReported:
    def test_on_the_detailed_signal_and_in_the_agent_payload(self) -> None:
        from evotrader.agents.tools import _composite_provenance

        sig = _detailed(_with("11:30", mean_reversion=(0.0, IN)))
        block = _composite_provenance(sig)
        assert block["participation_numerator"] == 1
        assert block["participation_denominator"] == 5
        assert block["participation_scale"] == pytest.approx(0.5)
        assert block["attenuation_applied"] is True

    def test_an_older_signal_shape_reports_none(self) -> None:
        from evotrader.agents.tools import _composite_provenance

        old = types.SimpleNamespace(
            n_additive=2, n_applicable=2, n_voting=2, n_in_scope=2, amplification_capped=[]
        )
        block = _composite_provenance(old)
        assert block["participation_numerator"] is None
        assert block["participation_denominator"] is None


# ── base.warming_up and the two channels that report it ──────────────────────

_OPEN = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)  # 09:30 ET


def _bars(n: int, minutes: float = 5.0, price: float = 160.0) -> list[OHLCV]:
    return [
        OHLCV(
            timestamp=_OPEN + timedelta(minutes=minutes * i),
            open=price,
            high=price + 0.5,
            low=price - 0.5,
            close=price,
            volume=1e5,
        )
        for i in range(n)
    ]


class TestWarmingUpRule:
    def test_twelve_five_minute_bars_of_twenty(self) -> None:
        """12 five-minute bars at 10:30 ET; 78 fit in a full session."""
        assert warming_up(_bars(12), 20) is True

    def test_twenty_hourly_bars_never_fit(self) -> None:
        """The 1h backtest: seven bars a session at most, so a 20-bar channel
        is structurally dark, not early."""
        assert warming_up(_bars(2, minutes=60), 20) is False
        assert warming_up(_bars(6, minutes=60), 20) is False

    def test_a_small_need_at_an_hourly_pace_is_early(self) -> None:
        assert warming_up(_bars(2, minutes=60), 3) is True

    def test_too_few_bars_to_measure_the_pace(self) -> None:
        assert warming_up([], 20) is False
        assert warming_up(_bars(1), 20) is False

    def test_not_short_is_not_warming_up(self) -> None:
        assert warming_up(_bars(20), 20) is False

    def test_a_missing_bar_does_not_change_the_pace(self) -> None:
        bars = _bars(4)
        del bars[2]  # 09:30, 09:35, 09:45
        assert warming_up(bars, 20) is True

    def test_identical_timestamps_give_no_pace(self) -> None:
        same = [_bars(1)[0]] * 3
        assert warming_up(same, 20) is False


def _market(candles: list[OHLCV], *, anchor: str = "current_session", atr: float | None = 9.0):
    now = candles[-1].timestamp if candles else _OPEN
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=now,
        quote=Quote(ticker="XYZ", bid=159.98, ask=160.02, last=160.0, volume=1e6, timestamp=now),
        indicators=TechnicalIndicators(
            atr_14=atr,
            vwap=160.0,
            vwap_anchor=anchor,
            ema_9=157.0,
            ema_21=149.0,
            sma_20=148.6,
            sma_50=125.0,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.6, reasoning="fixture"
        ),
        recent_candles=candles,
    )


class TestSwingFailureReportsWarmingUp:
    def test_early_in_the_session(self) -> None:
        meta = (
            SwingFailureReversalStrategy(lookback_bars=20)
            .compute_signal(_market(_bars(12)))
            .metadata
        )
        assert meta["applicable"] is False
        assert meta["reason"] == "insufficient_data"
        assert meta["warming_up"] is True
        assert (meta["bars"], meta["bars_needed"]) == (12, 20)

    @pytest.mark.parametrize(
        ("candles", "anchor", "atr"),
        [
            ([], "current_session", 9.0),  # pre-market and the opening print
            (_bars(12), "prior_session", 9.0),  # after the close
            (_bars(12), "current_session", None),  # a data fault, not an early hour
            (_bars(6, minutes=60), "current_session", 9.0),  # 20 bars never fit
        ],
        ids=["no-bars", "prior-session", "no-atr", "hourly-bars"],
    )
    def test_not_otherwise(self, candles, anchor, atr) -> None:
        meta = (
            SwingFailureReversalStrategy(lookback_bars=20)
            .compute_signal(_market(candles, anchor=anchor, atr=atr))
            .metadata
        )
        assert meta["reason"] == "insufficient_data"
        assert "warming_up" not in meta


class TestVwapReclaimReportsWarmingUp:
    def test_early_in_the_session(self) -> None:
        meta = (
            VwapReclaimContinuationStrategy(min_dip_bars=2)
            .compute_signal(_market(_bars(2)))
            .metadata
        )
        assert meta["reason"] == "insufficient_intraday_candles"
        assert meta["warming_up"] is True

    def test_not_before_the_session_has_bars(self) -> None:
        meta = VwapReclaimContinuationStrategy(min_dip_bars=2).compute_signal(_market([])).metadata
        assert meta["reason"] == "insufficient_intraday_candles"
        assert "warming_up" not in meta


# ── Finding 3: the MACD neutral flag ─────────────────────────────────────────


def _momentum_meta(histogram: float, atr: float | None) -> dict:
    """A bull-stacked stock a little above its prior close, with the histogram
    and ATR given."""
    now = datetime(2026, 3, 2, 17, 30, tzinfo=UTC)
    macd_signal_line = 10.7
    snapshot = MarketSnapshot(
        ticker="XYZ",
        timestamp=now,
        quote=Quote(ticker="XYZ", bid=160.48, ask=160.52, last=160.5, volume=5e6, timestamp=now),
        indicators=TechnicalIndicators(
            ema_9=157.0,
            ema_21=149.0,
            sma_20=148.5,
            sma_50=125.0,
            macd_line=macd_signal_line + histogram,
            macd_signal=macd_signal_line,
            macd_histogram=histogram,
            atr_14=atr,
            rsi_14=63.0,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.6, reasoning="fixture"
        ),
        daily_change_pct=0.3,
    )
    return MomentumStrategy().compute_signal(snapshot).metadata


class TestMacdNeutralFlag:
    def test_a_small_histogram_is_neutral(self) -> None:
        """-0.6 against an ATR of 9: the MACD sub-signal reads -0.18, which is
        noise rather than dissent."""
        meta = _momentum_meta(-0.6, 9.0)
        assert meta["macd_signal"] == pytest.approx(-0.1824, abs=1e-4)
        assert meta["macd_hist_atr"] == pytest.approx(-0.0667, abs=1e-4)
        assert meta["macd_neutral"] is True
        assert meta["macd_neutral_atr"] == MACD_NEUTRAL_ATR == 0.10

    def test_a_histogram_beyond_the_band_is_not(self) -> None:
        meta = _momentum_meta(1.5, 9.0)
        assert meta["macd_hist_atr"] == pytest.approx(0.1667, abs=1e-4)
        assert meta["macd_neutral"] is False

    def test_no_atr_no_flag(self) -> None:
        meta = _momentum_meta(-0.6, None)
        assert meta["macd_hist_atr"] is None
        assert meta["macd_neutral"] is None


# ── Provenance: every shared module the engine imports is in its fingerprint ──


def test_every_algorithms_module_the_engine_imports_is_fingerprinted() -> None:
    """units.py was imported by six strategies and sat outside the hash, so a
    change to it would have moved composites with no engine-change notice."""
    pkg = Path(composite_module.__file__).resolve().parents[1]
    covered = {
        f.relative_to(pkg).as_posix()
        for rel in composite_module._ENGINE_SOURCES
        for f in ((pkg / rel).rglob("*.py") if (pkg / rel).is_dir() else [pkg / rel])
    }
    missing = set()
    for rel in covered:
        for node in ast.walk(ast.parse((pkg / rel).read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "evotrader.algorithms"
            ):
                mod = node.module.removeprefix("evotrader.").replace(".", "/")
                target = next(
                    (c for c in (f"{mod}.py", f"{mod}/__init__.py") if (pkg / c).exists()), None
                )
                if target and target not in covered:
                    missing.add(target)
    assert not missing, f"engine modules outside the fingerprint: {sorted(missing)}"
