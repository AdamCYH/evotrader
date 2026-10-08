"""Momentum's fast-dissent gate: the slow stack stops voting alone against the fast reads.

See: data/evolution/reviews/
20261007_201942_momentum_fast_dissent_gate_price_vs_ema9_and_macd.md

The momentum channel blends a moving-average term (60%) with the daily MACD
(40%). Every part of the MA term lags a reversal by days: after a long advance
it reads how far above its averages price still is, not which way it is going.
On a day that gapped down, never filled and closed far lower, with price below
EMA 9 all session and the MACD histogram well outside its neutral band, the
channel still voted long all day, halved by the divergence guard and still long.

With ``fast_dissent_decay`` set, the MA term is decayed when BOTH fast reads
oppose it: price beyond EMA 9 by ``fast_dissent_min_atr`` daily ATRs, and the
MACD histogram outside its neutral band. The default, 1.0, is off. The
condition is recorded (``fast_dissent``) either way, so its days can be counted
before a version acts on it. All numbers are made up.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evotrader.algorithms.strategies.momentum import MACD_NEUTRAL_ATR, MomentumStrategy
from evotrader.db.signal_attribution import channel_votes_from_sub_signals
from evotrader.indicators.macd import macd_signal
from evotrader.indicators.moving_averages import moving_average_signal
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

NOW = datetime(2026, 3, 10, 16, 0, tzinfo=UTC)
ATR = 4.0
# A bull stack that price has just broken under: EMA 9 above EMA 21, SMA 20
# above SMA 50, price below EMA 9 by 1.25 ATRs.
STACK = {"ema_9": 100.0, "ema_21": 98.0, "sma_20": 97.0, "sma_50": 90.0}


def _snapshot(
    price: float = 95.0,
    hist: float = -0.6,
    day_change_pct: float = -2.0,
    stack: dict | None = None,
) -> MarketSnapshot:
    stack = stack or STACK
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=NOW,
            previous_close=round(price / (1 + day_change_pct / 100), 4),
        ),
        indicators=TechnicalIndicators(
            atr_14=ATR,
            macd_line=hist,
            macd_signal=0.0,
            macd_histogram=hist,
            relative_volume=None,
            **stack,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.6, reasoning="fixture"
        ),
        daily_change_pct=day_change_pct,
    )


def _terms(snap: MarketSnapshot) -> tuple[float, float]:
    ind = snap.indicators
    ma = moving_average_signal(
        close=snap.quote.last,
        ema_9=ind.ema_9,
        ema_21=ind.ema_21,
        sma_20=ind.sma_20,
        sma_50=ind.sma_50,
        atr=ATR,
    )
    macd = macd_signal(
        macd_line_value=ind.macd_line,
        signal_line_value=ind.macd_signal,
        histogram_value=ind.macd_histogram,
        atr=ATR,
    )
    return ma, macd


def _vote(decay: float | None = None, **kw):
    params = {} if decay is None else {"fast_dissent_decay": decay}
    return MomentumStrategy(**params).compute_signal(_snapshot(**kw))


class TestThePremise:
    def test_the_stack_reads_long_while_both_fast_reads_point_down(self) -> None:
        ma, macd = _terms(_snapshot())
        assert ma > 0, "the MA term still reads the advance"
        assert macd < 0
        assert abs(-0.6 / ATR) >= MACD_NEUTRAL_ATR, "the MACD is a real dissent"


class TestOffByDefault:
    def test_the_value_is_unchanged_and_the_condition_is_recorded(self) -> None:
        sig = _vote()
        ma, macd = _terms(_snapshot())
        raw = 0.6 * ma + 0.4 * macd
        assert raw > 0
        # A strong down day against a long read: the divergence guard halves it.
        assert sig.value == pytest.approx(raw * 0.5)
        meta = sig.metadata
        assert meta["fast_dissent"] is True
        assert meta["fast_dissent_applied"] is False
        assert meta["ma_signal_effective"] == meta["ma_signal"] == ma
        assert meta["price_vs_ema9_atr"] == pytest.approx(-1.25)

    def test_explicitly_off_is_the_same_as_the_default(self) -> None:
        assert _vote(1.0).value == _vote().value


class TestTheGate:
    def test_at_zero_the_macd_sets_the_sign(self) -> None:
        sig = _vote(0.0)
        _, macd = _terms(_snapshot())
        # Raw and the day now agree, so the divergence guard does not fire.
        assert sig.value == pytest.approx(0.4 * macd)
        assert sig.value < 0
        meta = sig.metadata
        assert meta["fast_dissent_applied"] is True
        assert meta["ma_signal_effective"] == 0.0
        assert meta["divergence_applied"] is False

    def test_half_decay_is_in_between(self) -> None:
        ma, macd = _terms(_snapshot())
        sig = _vote(0.5)
        raw = 0.6 * 0.5 * ma + 0.4 * macd
        expected = raw * 0.5 if raw > 0 else raw  # the guard halves a long read on a down day
        assert sig.value == pytest.approx(expected)
        assert sig.metadata["ma_signal_effective"] == pytest.approx(0.5 * ma)

    def test_a_bear_stack_mirrors(self) -> None:
        bear = {"ema_9": 100.0, "ema_21": 102.0, "sma_20": 103.0, "sma_50": 110.0}
        sig = _vote(0.0, price=105.0, hist=0.6, day_change_pct=2.0, stack=bear)
        assert sig.metadata["fast_dissent_applied"] is True
        assert sig.value > 0


class TestBothFastReadsMustDissent:
    def test_price_above_ema9_does_not_gate(self) -> None:
        sig = _vote(0.0, price=100.5)
        assert sig.metadata["fast_dissent"] is False
        assert sig.metadata["ma_signal_effective"] == sig.metadata["ma_signal"]

    def test_a_shallow_dip_under_ema9_does_not_gate(self) -> None:
        """Half a dollar is 0.125 ATR, under the 0.25 ATR margin."""
        assert _vote(0.0, price=99.5).metadata["fast_dissent"] is False

    def test_a_neutral_macd_does_not_gate(self) -> None:
        """A histogram of 0.05 ATR is inside the neutral band: neither co-sign nor dissent."""
        assert _vote(0.0, hist=-0.2).metadata["fast_dissent"] is False

    def test_a_macd_on_the_stacks_side_does_not_gate(self) -> None:
        assert _vote(0.0, hist=0.6).metadata["fast_dissent"] is False

    def test_the_margin_is_a_parameter(self) -> None:
        sig = MomentumStrategy(fast_dissent_decay=0.0, fast_dissent_min_atr=0.1).compute_signal(
            _snapshot(price=99.5)
        )
        assert sig.metadata["fast_dissent_applied"] is True


class TestParameters:
    def test_round_trip(self) -> None:
        strat = MomentumStrategy()
        params = strat.get_parameters()
        assert params["fast_dissent_decay"] == 1.0
        assert params["fast_dissent_min_atr"] == 0.25
        strat.set_parameters({"fast_dissent_decay": 0.0, "fast_dissent_min_atr": 0.3})
        assert strat.get_parameters()["fast_dissent_decay"] == 0.0
        assert strat.get_parameters()["fast_dissent_min_atr"] == 0.3

    @pytest.mark.parametrize(
        "bad",
        [{"fast_dissent_decay": 1.2}, {"fast_dissent_decay": -0.1}, {"fast_dissent_min_atr": 4}],
    )
    def test_validation(self, bad: dict) -> None:
        assert MomentumStrategy().validate_parameters(bad)

    def test_valid_values_pass(self) -> None:
        good = {"fast_dissent_decay": 0.0, "fast_dissent_min_atr": 0.25}
        assert MomentumStrategy().validate_parameters(good) == []


class TestTheRecordKeepsTheCohort:
    def test_the_flags_reach_the_channel_votes(self) -> None:
        sig = _vote()
        votes = channel_votes_from_sub_signals(
            [
                {"name": "momentum", "value": sig.value, "weight": 0.2, "metadata": sig.metadata},
                {"name": "gap", "value": 0.0, "weight": 0.0, "metadata": {"reason": "x"}},
            ]
        )
        assert votes["momentum"]["fast_dissent"] is True
        assert votes["momentum"]["fast_dissent_applied"] is False
        assert votes["momentum"]["divergence_applied"] is True
        assert set(votes["gap"]) == {"value", "weight", "applicable", "reason"}, (
            "a channel without the flags is stored as before"
        )
