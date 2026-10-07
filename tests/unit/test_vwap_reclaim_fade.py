"""The VWAP reclaim fade channel: the same reclaim, the opposite thesis, in shadow.

See: data/evolution/proposals/p_new_vwap_reclaim_fade_20261006_155319.md

``vwap_reclaim_continuation`` votes WITH the daily trend after an intraday dip
through VWAP is reclaimed. ``vwap_reclaim_fade`` votes AGAINST the same reclaim
when it happened on light volume, on the thesis that a reclaim without
participation is the end of the bounce. Both read the reclaim through one
function, ``locate_vwap_dip_episode``, so they can only disagree about what a
reclaim means, never about whether it happened. The fade ships at weight 0 in
every regime: recorded, not voting.

All numbers are made up.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.vwap_reclaim_continuation import (
    VwapReclaimContinuationStrategy,
)
from evotrader.algorithms.strategies.vwap_reclaim_fade import VwapReclaimFadeStrategy
from evotrader.indicators.vwap_episode import locate_vwap_dip_episode
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal

VWAP = 100.0
ATR = 2.0
# 11:00 ET on a made-up session day.
NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def _bars(closes: list[float]) -> list[OHLCV]:
    """Five-minute bars, the last one opening 5 minutes before NOW."""
    n = len(closes)
    return [
        OHLCV(
            timestamp=NOW - timedelta(minutes=5 * (n - i)),
            open=c,
            high=c + 0.05,
            low=c - 0.05,
            close=c,
            volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]


def _bull_dip(depth: float = 0.60, dip_bars: int = 12, reclaim_bars: int = 3) -> list[float]:
    """Above VWAP, a dip whose lowest close is ``depth`` below it, then back above."""
    dip = [VWAP - 0.2] * dip_bars
    dip[dip_bars // 2] = VWAP - depth
    return [VWAP + 0.4] * 5 + dip + [VWAP + 0.3] * reclaim_bars


def _snapshot(
    closes: list[float],
    *,
    price: float = VWAP + 0.3,
    stack: str = "bull",
    rvol: float | None = 0.8,
    daily_rvol: float | None = None,
    anchor: str = "current_session",
) -> MarketSnapshot:
    if stack == "bull":
        mas = {"ema_9": price - 0.5, "ema_21": price - 1.0, "sma_20": 98.0, "sma_50": 95.0}
    elif stack == "bear":
        mas = {"ema_9": price + 0.5, "ema_21": price + 1.0, "sma_20": 102.0, "sma_50": 105.0}
    else:
        mas = {"ema_9": price - 0.5, "ema_21": price + 1.0, "sma_20": 98.0, "sma_50": 95.0}
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ", bid=price - 0.01, ask=price + 0.01, last=price, volume=1e6, timestamp=NOW
        ),
        indicators=TechnicalIndicators(
            vwap=VWAP,
            vwap_anchor=anchor,
            atr_14=ATR,
            session_relative_volume=rvol,
            relative_volume=daily_rvol,
            **mas,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.6, reasoning="fixture"
        ),
        recent_candles=_bars(closes),
    )


def _mirror(closes: list[float]) -> list[float]:
    return [2 * VWAP - c for c in closes]


class TestTheWorkedExample:
    def test_a_low_volume_reclaim_in_a_bull_stack_is_faded(self) -> None:
        """The docstring's example: a 12-bar dip 0.30 ATR deep, reclaimed 15
        minutes ago, on relative volume 0.80."""
        sig = VwapReclaimFadeStrategy().compute_signal(_snapshot(_bull_dip()))
        meta = sig.metadata

        assert meta["reason"] == "reclaim_faded" and meta["fired"] is True
        assert meta["dip_bars"] == 12
        assert meta["depth_atr"] == pytest.approx(0.30)
        assert meta["age_min"] == pytest.approx(15.0)
        assert meta["rvol_source"] == "session_profile"
        assert meta["vol_fade_term"] == pytest.approx(0.5)
        expected = -0.6 * math.tanh(0.30 / 0.35) * 0.8**0.5 * 0.5
        assert sig.value == pytest.approx(expected, abs=1e-9)
        assert sig.value == pytest.approx(-0.186, abs=5e-4)
        assert meta["leg"] == "fade_bull_reclaim"
        assert meta["session_minute"] == pytest.approx(90.0)

    def test_the_bear_stack_mirror_fades_upward(self) -> None:
        bull = VwapReclaimFadeStrategy().compute_signal(_snapshot(_bull_dip()))
        bear = VwapReclaimFadeStrategy().compute_signal(
            _snapshot(_mirror(_bull_dip()), price=VWAP - 0.3, stack="bear")
        )
        assert bear.metadata["leg"] == "fade_bear_reclaim"
        assert bear.value == pytest.approx(-bull.value, abs=1e-12)
        assert bear.value > 0


class TestVolumeDecides:
    @pytest.mark.parametrize(
        ("rvol", "term"),
        [(0.4, 1.0), (0.6, 1.0), (0.7, 0.75), (0.9, 0.25), (0.99, 0.025)],
    )
    def test_the_fade_term(self, rvol: float, term: float) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(_snapshot(_bull_dip(), rvol=rvol))
        assert sig.metadata["vol_fade_term"] == pytest.approx(term, abs=1e-4)
        assert sig.value < 0

    @pytest.mark.parametrize("rvol", [1.0, 1.4, 3.0])
    def test_a_reclaim_on_volume_is_left_alone(self, rvol: float) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(_snapshot(_bull_dip(), rvol=rvol))
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "reclaim_on_volume"
        assert sig.metadata["applicable"] is True, "a real abstention, not missing data"

    def test_no_volume_reading_is_missing_data(self) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(
            _snapshot(_bull_dip(), rvol=None, daily_rvol=None)
        )
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "rvol_unavailable"
        assert sig.metadata["applicable"] is False

    def test_before_the_session_profile_is_warm_the_daily_ratio_is_labelled(self) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(
            _snapshot(_bull_dip(), rvol=None, daily_rvol=0.7)
        )
        assert sig.metadata["rvol_source"] == "prior_session_daily_fallback"
        assert sig.value < 0


class TestOneDetectorTwoReadings:
    EPISODE_KEYS = (
        "dip_bars",
        "depth_atr",
        "bars_since_reclaim",
        "age_min",
        "age_source",
        "reclaimed_now",
    )

    @pytest.mark.parametrize(
        "closes",
        [
            _bull_dip(),
            _bull_dip(depth=0.3, dip_bars=3, reclaim_bars=1),
            _bull_dip(depth=1.5, dip_bars=20, reclaim_bars=8),
            _bull_dip(dip_bars=1),
        ],
    )
    def test_they_agree_on_the_episode(self, closes: list[float]) -> None:
        snap = _snapshot(closes)
        cont = VwapReclaimContinuationStrategy(rvol_source="session").compute_signal(snap)
        fade = VwapReclaimFadeStrategy().compute_signal(snap)
        for key in self.EPISODE_KEYS:
            assert cont.metadata[key] == fade.metadata[key], key

    def test_when_both_fire_they_point_opposite_ways(self) -> None:
        snap = _snapshot(_bull_dip())
        cont = VwapReclaimContinuationStrategy(rvol_source="session").compute_signal(snap)
        fade = VwapReclaimFadeStrategy().compute_signal(snap)
        assert cont.value > 0 > fade.value

    def test_the_gates_are_the_same_gates(self) -> None:
        cases = {
            "not_reclaimed": _snapshot(_bull_dip(), price=VWAP - 0.1),
            "dip_too_deep_trend_break": _snapshot(_bull_dip(depth=2.0)),
            "dip_too_brief": _snapshot(_bull_dip(dip_bars=1)),
            "reclaim_too_old": _snapshot(_bull_dip(reclaim_bars=20)),
        }
        for reason, snap in cases.items():
            cont = VwapReclaimContinuationStrategy().compute_signal(snap)
            fade = VwapReclaimFadeStrategy().compute_signal(snap)
            assert cont.metadata["reason"] == fade.metadata["reason"] == reason
            assert fade.value == 0.0

    def test_the_helper_reports_no_cross(self) -> None:
        episode = locate_vwap_dip_episode(
            _bars([VWAP + 0.5] * 10),
            vwap=VWAP,
            atr=ATR,
            bull_stack=True,
            price=VWAP + 0.5,
            lookback_bars=30,
            now=NOW,
        )
        assert episode.no_cross and episode.lookback_bars_seen == 10


class TestAbstentions:
    def test_a_stale_anchor(self) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(
            _snapshot(_bull_dip(), anchor="prior_session")
        )
        assert sig.metadata["reason"] == "stale_vwap_anchor"
        assert sig.metadata["applicable"] is False

    def test_no_stack_is_out_of_scope(self) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(_snapshot(_bull_dip(), stack="mixed"))
        assert sig.metadata["in_scope"] is False
        assert sig.metadata["out_of_scope_reason"] == "no_ma_stack"

    def test_too_few_bars(self) -> None:
        sig = VwapReclaimFadeStrategy().compute_signal(_snapshot([VWAP + 0.3]))
        assert sig.metadata["reason"] == "insufficient_intraday_candles"
        assert sig.value == 0.0


class TestParameters:
    def test_round_trip(self) -> None:
        strat = VwapReclaimFadeStrategy()
        params = strat.get_parameters()
        params.update(max_rvol_for_fade=1.2, full_fade_rvol=0.5, lookback_bars=24)
        strat.set_parameters(params)
        assert strat.get_parameters() == params

    @pytest.mark.parametrize(
        "bad",
        [
            {"full_fade_rvol": 1.0, "max_rvol_for_fade": 1.0},
            {"min_dip_atr": 0.9, "max_dip_atr": 0.5},
            {"rvol_source": "weekly"},
            {"base_strength": 0.0},
            {"lookback_bars": 2},
        ],
    )
    def test_validation_catches_it(self, bad: dict) -> None:
        assert VwapReclaimFadeStrategy().validate_parameters(bad)

    def test_the_starter_values_validate(self) -> None:
        config = yaml.safe_load(
            Path("starter_data/algorithms/v001_initial/config.yaml").read_text()
        )
        params = config["vwap_reclaim_fade"]
        assert VwapReclaimFadeStrategy().validate_parameters(params) == []


class TestItIsAShadow:
    def test_weight_zero_in_every_regime_of_the_starter_algorithm(self) -> None:
        config = yaml.safe_load(
            Path("starter_data/algorithms/v001_initial/config.yaml").read_text()
        )
        composite = config["composite"]
        assert composite["vwap_reclaim_fade_weight"] == 0.0
        for regime, weights in composite["regime_weights"].items():
            assert weights["vwap_reclaim_fade"] == 0.0, regime

    def test_the_starter_composite_builds_it_at_weight_zero(self) -> None:
        from evotrader.algorithms.loader import StrategyLoader

        loader = StrategyLoader(Path("starter_data/algorithms/strategy_manifest.yaml"))
        params = yaml.safe_load(
            Path("starter_data/algorithms/v001_initial/config.yaml").read_text()
        )
        composite = loader.build_composite(params)
        assert "vwap_reclaim_fade" in composite._strategies
        for regime in MarketRegime:
            weights = composite._regime_weights_map.get(regime) or {}
            assert weights.get("vwap_reclaim_fade", 0.0) == 0.0, regime

    def test_the_default_weight_maps_hold_it_at_zero(self) -> None:
        defaults = CompositeStrategy._get_default_regime_weights_map()
        for regime, weights in defaults.items():
            assert weights["vwap_reclaim_fade"] == 0.0, regime

    def test_a_firing_fade_moves_neither_the_composite_nor_the_count(self) -> None:
        """Excluded from the weighted sum AND from the participation count."""
        snap = _snapshot(_bull_dip())
        assert VwapReclaimFadeStrategy().compute_signal(snap).value < 0, "precondition: it fires"

        voters = {"alpha": (0.30, 0.5), "beta": (0.0, 0.3), "gamma": (-0.10, 0.2)}

        def run(with_fade: bool):
            subs = {n: _Fixed(n, v) for n, (v, _) in voters.items()}
            weights = {n: w for n, (_, w) in voters.items()}
            if with_fade:
                subs["vwap_reclaim_fade"] = VwapReclaimFadeStrategy()
                weights["vwap_reclaim_fade"] = 0.0
            return CompositeStrategy(
                sub_strategies=subs, weights=weights, regime_adaptive=False, version="t"
            ).compute_detailed_signal(snap)

        without, with_ = run(False), run(True)
        assert with_.composite_value == pytest.approx(without.composite_value, abs=1e-12)
        assert with_.participation_denominator == without.participation_denominator
        assert with_.participation_numerator == without.participation_numerator
        assert with_.participation_scale == pytest.approx(without.participation_scale)


class _Fixed(TradingAlgorithm):
    def __init__(self, name: str, value: float) -> None:
        self._name, self._value = name, value

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "fixture"

    def compute_signal(self, snapshot) -> AlgoSignal:
        return AlgoSignal(name=self._name, value=self._value, weight=1.0, metadata={})

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        return None

    def validate_parameters(self, params: dict) -> list[str]:
        return []
