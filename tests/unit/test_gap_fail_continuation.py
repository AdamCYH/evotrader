"""gap_fail_continuation: a gap that filled and kept going.

The ``gap`` channel reads 0.0 the moment a gap has filled, so nothing voted on
what a failed gap does next. This channel votes in the direction of the fade
once price is through the prior close, past session VWAP and has held there.
Made-up numbers: previous close 100, daily ATR 10, so a 5% gap is 0.5 ATR.
2026-03-04 is a Wednesday, before daylight saving time (17:30 UTC is 12:30 ET).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.gap_fail_continuation import GapFailContinuationStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal

PREV_CLOSE = 100.0
ATR = 10.0
MIDDAY = datetime(2026, 3, 4, 17, 30, tzinfo=UTC)  # 12:30 ET, 180 minutes in


def _snapshot(
    *,
    price: float,
    gap_pct: float | None = 5.0,
    vwap: float | None = 101.0,
    anchor: str | None = "current_session",
    closes: tuple[float, ...] = (98.5, 98.2, 98.0),
    now: datetime = MIDDAY,
    quoted_previous_close: float | None = PREV_CLOSE,
    stack: str = "bull",
) -> MarketSnapshot:
    candles = [
        OHLCV(
            timestamp=now - timedelta(minutes=5 * (len(closes) - i)),
            open=c,
            high=c + 0.2,
            low=c - 0.2,
            close=c,
            volume=10_000.0,
        )
        for i, c in enumerate(closes)
    ]
    if stack == "bull":
        mas = {"ema_9": price - 3, "ema_21": price - 6, "sma_20": 95.0, "sma_50": 90.0}
    elif stack == "bear":
        mas = {"ema_9": price + 3, "ema_21": price + 6, "sma_20": 95.0, "sma_50": 99.0}
    else:
        mas = {"ema_9": price + 3, "ema_21": price - 6, "sma_20": 95.0, "sma_50": 90.0}
    return MarketSnapshot(
        ticker="T",
        timestamp=now,
        quote=Quote(
            ticker="T",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=now,
            previous_close=quoted_previous_close,
        ),
        indicators=TechnicalIndicators(atr_14=ATR, vwap=vwap, vwap_anchor=anchor, **mas),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.7, reasoning="t"
        ),
        gap_pct=gap_pct,
        daily_change_pct=(price - PREV_CLOSE) / PREV_CLOSE * 100,
        recent_candles=candles,
    )


# A 5% gap-up (0.5 ATR) that has fully filled: price 98, two dollars through the
# prior close (penetration 0.2 ATR), with VWAP still up at 101.
GAP_TERM = math.tanh(0.5 / 0.5)  # 0.7616
PEN_TERM = 0.5 + 0.2 / 0.5  # 0.9
FIRED = -0.6 * GAP_TERM * PEN_TERM  # -0.4113


class TestAFailedGapUpVotesBearish:
    def test_fires_in_the_direction_of_the_fade(self) -> None:
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0))

        assert sig.value == pytest.approx(FIRED, abs=1e-4)
        meta = sig.metadata
        assert meta["applicable"] is True and meta["reason"] == "gap_failed"
        assert meta["leg"] == "failed_gap_up"
        assert meta["gap_atr"] == pytest.approx(0.5)
        assert meta["gap_basis"] == "atr"
        assert meta["penetration_atr"] == pytest.approx(0.2)
        assert meta["vwap_dev_atr"] == pytest.approx(0.3)
        assert meta["fill_held"] is True
        assert meta["decay_factor"] == 1.0
        assert meta["stack_context"] == "bull"

    def test_a_failed_gap_down_is_the_mirror_image(self) -> None:
        # Gapped down 5%, then rallied through the prior close to 102 with VWAP at 99.
        snap = _snapshot(
            price=102.0, gap_pct=-5.0, vwap=99.0, closes=(101.5, 101.8, 102.0), stack="bear"
        )
        sig = GapFailContinuationStrategy().compute_signal(snap)

        assert sig.value == pytest.approx(-FIRED, abs=1e-4)
        assert sig.metadata["leg"] == "failed_gap_down"
        assert sig.metadata["penetration_atr"] == pytest.approx(0.2)
        assert sig.metadata["stack_context"] == "bear"

    def test_a_deeper_break_is_a_stronger_vote_up_to_a_cap(self) -> None:
        strat = GapFailContinuationStrategy()
        shallow = strat.compute_signal(_snapshot(price=99.0, closes=(99.4, 99.2, 99.0)))
        deep = strat.compute_signal(_snapshot(price=96.0, closes=(96.5, 96.2, 96.0)))
        assert shallow.value > deep.value, "both bearish, the deeper break more so"
        assert deep.metadata["penetration_term"] == 1.0, "0.4 ATR through: capped"

    def test_the_previous_close_can_come_from_the_day_change(self) -> None:
        snap = _snapshot(price=98.0, quoted_previous_close=None)
        sig = GapFailContinuationStrategy().compute_signal(snap)
        assert sig.metadata["previous_close"] == pytest.approx(PREV_CLOSE)
        assert sig.value == pytest.approx(FIRED, abs=1e-4)


class TestOffDutyAndAbstentions:
    def test_a_small_gap_is_off_duty(self) -> None:
        """2% is 0.2 ATR: under the ATR gate even though it clears the percent fallback."""
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, gap_pct=2.0))
        assert sig.value == 0.0
        assert sig.metadata["applicable"] is False
        assert sig.metadata["reason"] == "gap_too_small"
        assert sig.metadata["gap_basis"] == "atr"

    def test_without_an_atr_threshold_the_percent_rule_decides(self) -> None:
        strat = GapFailContinuationStrategy(min_gap_atr=None)
        sig = strat.compute_signal(_snapshot(price=98.0, gap_pct=2.0))
        assert sig.metadata["gap_basis"] == "fallback"
        assert sig.metadata["reason"] == "gap_failed"

    def test_no_gap_reading_is_off_duty(self) -> None:
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, gap_pct=None))
        assert sig.metadata == {"applicable": False, "reason": "no_gap_or_atr"}

    def test_the_first_hour_belongs_to_the_gap_channel(self) -> None:
        early = MIDDAY.replace(hour=15, minute=15)  # 10:15 ET, 45 minutes in
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, now=early))
        assert sig.metadata["applicable"] is False
        assert sig.metadata["reason"] == "opening_range_forming"
        assert sig.metadata["elapsed_min"] == pytest.approx(45.0)

        on_the_hour = MIDDAY.replace(hour=15, minute=30)  # 10:30 ET
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, now=on_the_hour))
        assert sig.metadata["reason"] == "gap_failed"

    def test_before_the_open_there_is_no_session_to_read(self) -> None:
        pre_market = MIDDAY.replace(hour=13, minute=30)  # 08:30 ET
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, now=pre_market))
        assert sig.metadata["applicable"] is False
        assert sig.metadata["reason"] == "opening_range_forming"
        assert sig.metadata["elapsed_min"] is None

    def test_after_the_close_the_vwap_is_the_prior_sessions(self) -> None:
        """The 17:00 read: a gap is still reported, but the session is over."""
        evening = MIDDAY.replace(hour=22, minute=0)  # 17:00 ET
        snap = _snapshot(price=98.0, anchor="prior_session", closes=(), now=evening)
        sig = GapFailContinuationStrategy().compute_signal(snap)
        assert sig.metadata["applicable"] is False
        assert sig.metadata["reason"] == "no_session_vwap"
        assert sig.metadata["vwap_anchor"] == "prior_session"

    def test_a_gap_that_has_not_failed_is_a_genuine_zero(self) -> None:
        """Price still above the prior close: in play, nothing to vote on."""
        snap = _snapshot(price=100.3, closes=(100.9, 100.6, 100.3))
        sig = GapFailContinuationStrategy().compute_signal(snap)
        assert sig.value == 0.0
        assert sig.metadata["applicable"] is True
        assert sig.metadata["reason"] == "gap_not_failed"
        assert sig.metadata["penetration_atr"] == pytest.approx(-0.03)

    def test_through_the_close_but_not_past_vwap_waits(self) -> None:
        snap = _snapshot(price=99.4, vwap=99.5, closes=(99.6, 99.5, 99.4))
        sig = GapFailContinuationStrategy().compute_signal(snap)
        assert sig.metadata["reason"] == "vwap_not_lost"
        assert sig.metadata["vwap_dev_atr"] == pytest.approx(0.01)

    def test_one_print_through_the_prior_close_does_not_count(self) -> None:
        """Two of the last three bars closed back above the prior close."""
        snap = _snapshot(price=98.0, closes=(100.5, 100.2, 98.0))
        sig = GapFailContinuationStrategy().compute_signal(snap)
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "fill_not_held"
        assert sig.metadata["fill_held"] is False

    def test_the_hold_test_can_be_switched_off(self) -> None:
        snap = _snapshot(price=98.0, closes=(100.5, 100.2, 98.0))
        sig = GapFailContinuationStrategy(hold_bars=0).compute_signal(snap)
        assert sig.metadata["reason"] == "gap_failed"
        assert sig.metadata["fill_held"] is True


class TestTheLateSessionFade:
    def test_no_decay_until_the_last_half_hour(self) -> None:
        at_1529 = MIDDAY.replace(hour=20, minute=29)
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, now=at_1529))
        assert sig.metadata["decay_factor"] == 1.0

    def test_half_way_through_the_fade_the_vote_is_halved(self) -> None:
        at_1545 = MIDDAY.replace(hour=20, minute=45)  # 375 minutes in
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, now=at_1545))
        assert sig.metadata["decay_factor"] == pytest.approx(0.5)
        assert sig.value == pytest.approx(FIRED / 2, abs=1e-4)

    def test_nothing_is_left_at_the_bell(self) -> None:
        at_1600 = MIDDAY.replace(hour=21, minute=0)
        sig = GapFailContinuationStrategy().compute_signal(_snapshot(price=98.0, now=at_1600))
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "gap_failed", "it fired; the fade took it to zero"


class TestParameters:
    def test_round_trip(self) -> None:
        strat = GapFailContinuationStrategy()
        strat.set_parameters({"hold_bars": "4", "min_gap_atr": None, "base_strength": 0.5})
        params = strat.get_parameters()
        assert params["hold_bars"] == 4
        assert params["min_gap_atr"] is None
        assert params["base_strength"] == 0.5

    def test_bad_values_are_named(self) -> None:
        strat = GapFailContinuationStrategy()
        assert strat.validate_parameters(strat.get_parameters()) == []
        errors = strat.validate_parameters(
            {
                "base_strength": 0.0,
                "hold_bars": 25,
                "min_gap_atr": 7.0,
                "late_session_minute": 390.0,
                "session_minutes": 390.0,
            }
        )
        joined = "\n".join(errors)
        for key in ("base_strength", "hold_bars", "min_gap_atr", "late_session_minute"):
            assert key in joined, joined
        assert len(errors) == 4

    def test_the_shipped_config_keeps_it_in_shadow(self, active_algorithm_config) -> None:
        """Weight 0 in every regime: recorded, not voting, until promoted on its record."""
        cfg = active_algorithm_config
        assert cfg["gap_fail_continuation"]["hold_bars"] == 3
        assert cfg["gap_fail_continuation"]["min_gap_atr"] == 0.3
        assert cfg["composite"]["gap_fail_continuation_weight"] == 0.0
        for regime, rw in cfg["composite"]["regime_weights"].items():
            assert rw["gap_fail_continuation"] == 0.0, (regime, "still shadow")


class _Fixed(TradingAlgorithm):
    def __init__(self, name: str, value: float) -> None:
        self._name, self._value = name, value

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "t"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        return AlgoSignal(name=self._name, value=self._value, weight=1.0, metadata={})


def test_at_weight_zero_a_firing_leaves_the_composite_alone() -> None:
    snap = _snapshot(price=98.0)
    voters = {"momentum": _Fixed("momentum", 0.5), "mean_reversion": _Fixed("mean_reversion", -0.1)}
    weights = {"momentum": 0.2, "mean_reversion": 0.13}
    without = CompositeStrategy(dict(voters), dict(weights), regime_adaptive=False)
    with_shadow = CompositeStrategy(
        {**voters, "gap_fail_continuation": GapFailContinuationStrategy()},
        {**weights, "gap_fail_continuation": 0.0},
        regime_adaptive=False,
    )
    a = without.compute_detailed_signal(snap)
    b = with_shadow.compute_detailed_signal(snap)
    assert b.composite_value == pytest.approx(a.composite_value, abs=1e-9)
    shadow = next(s for s in b.signals if s.name == "gap_fail_continuation")
    assert shadow.value == pytest.approx(FIRED, abs=1e-4), "recorded all the same"
    assert shadow.weight == 0.0
