"""mean_reversion's trend guard can be set apart from the oscillators' damping.

One setting, ``trend_fade_decay``, did two jobs: it damped the Bollinger and IBS
parts on every cycle, and it was the factor the trend guard applied to a vote
against the daily stack. Raising it to let the guard speak louder also raised
the two oscillators against RSI on every cycle, so the channel became a
different mix, not the same channel with a lighter guard. ``trend_guard_decay``
sets the guard's factor alone; unset, it is ``trend_fade_decay`` and every vote
is unchanged. Made-up ticker and prices.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

NOW = datetime(2026, 3, 4, 16, 30, tzinfo=UTC)
PARAMS = {
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "ibs_oversold": 0.2,
    "ibs_overbought": 0.8,
    "rsi_weight": 0.6,
    "bollinger_weight": 0.2,
    "ibs_weight": 0.2,
    "trend_fade_decay": 0.2,
}
# Price under EMA 9 under EMA 21 and SMA 20 under SMA 50: a bear stack, in
# which a buy is the counter-trend leg the guard decays.
BEAR = {"ema_9": 96.0, "ema_21": 98.0, "sma_20": 99.0, "sma_50": 104.0}
BULL = {"ema_9": 104.0, "ema_21": 102.0, "sma_20": 101.0, "sma_50": 96.0}


def _snapshot(price: float, rsi: float, ibs: float, stack: dict) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(ticker="XYZ", bid=price, ask=price, last=price, volume=1e5, timestamp=NOW),
        indicators=TechnicalIndicators(
            rsi_14=rsi,
            ibs=ibs,
            bollinger_upper=106.0,
            bollinger_middle=100.0,
            bollinger_lower=94.0,
            **stack,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="fixture"
        ),
    )


def _vote(snap: MarketSnapshot, **kw):
    return MeanReversionStrategy(**{**PARAMS, **kw}).compute_signal(snap)


# An oversold dip in a bear stack: the guard decays the buy.
DIP = _snapshot(price=95.0, rsi=28.0, ibs=0.1, stack=BEAR)


class TestUnsetIsTheOldRule:
    def test_the_guard_uses_trend_fade_decay(self) -> None:
        sig = _vote(DIP)
        assert sig.metadata["trend_guard_applied"] is True
        assert sig.metadata["trend_guard_decay"] == 0.2

    def test_unset_and_set_to_trend_fade_decay_are_the_same_vote(self) -> None:
        rng = random.Random(5)
        for _ in range(300):
            snap = _snapshot(
                price=rng.uniform(92, 108),
                rsi=rng.uniform(15, 85),
                ibs=rng.uniform(0, 1),
                stack=rng.choice([BEAR, BULL, {}]),
            )
            decay = rng.choice([0.0, 0.2, 0.35, 1.0])
            unset = _vote(snap, trend_fade_decay=decay)
            same = _vote(snap, trend_fade_decay=decay, trend_guard_decay=decay)
            assert unset.value == same.value
            assert unset.metadata == same.metadata


class TestSetItChangesTheGuardAlone:
    def test_a_counter_trend_vote_is_decayed_by_the_new_factor(self) -> None:
        old, new = _vote(DIP), _vote(DIP, trend_guard_decay=0.5)
        assert new.metadata["trend_guard_applied"] is True
        assert new.value == pytest.approx(old.value / 0.2 * 0.5)
        assert new.metadata["trend_guard_decay"] == 0.5

    def test_the_oscillators_keep_their_damping(self) -> None:
        old, new = _vote(DIP), _vote(DIP, trend_guard_decay=0.5)
        for part in ("rsi_signal", "bollinger_signal", "ibs_signal"):
            assert new.metadata[part] == old.metadata[part], part

    def test_raising_trend_fade_decay_instead_changes_the_mix(self) -> None:
        """What the one setting did: the oscillators move too."""
        guard_only = _vote(DIP, trend_guard_decay=0.5)
        both = _vote(DIP, trend_fade_decay=0.5)
        assert both.metadata["bollinger_signal"] == pytest.approx(
            guard_only.metadata["bollinger_signal"] / 0.2 * 0.5
        )
        assert both.value != pytest.approx(guard_only.value)

    def test_a_with_trend_vote_is_untouched(self) -> None:
        rally = _snapshot(price=95.5, rsi=78.0, ibs=0.95, stack=BEAR)  # a sell in a bear stack
        old, new = _vote(rally), _vote(rally, trend_guard_decay=0.9)
        assert old.metadata["trend_guard_applied"] is False
        assert new.value == old.value


class TestParameters:
    def test_round_trip(self) -> None:
        strat = MeanReversionStrategy()
        assert strat.get_parameters()["trend_guard_decay"] is None
        strat.set_parameters({"trend_guard_decay": 0.5})
        assert strat.get_parameters()["trend_guard_decay"] == 0.5

    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_validation(self, bad: float) -> None:
        assert MeanReversionStrategy().validate_parameters({"trend_guard_decay": bad})

    @pytest.mark.parametrize("good", [None, 0.0, 0.5, 1.0])
    def test_valid_values(self, good: float | None) -> None:
        assert MeanReversionStrategy().validate_parameters({"trend_guard_decay": good}) == []
