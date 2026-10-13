"""The VWAP fade is damped when it fades from the session's own extreme.

Found by the evolution agent's code review. The channel fades the distance
from session VWAP and never asked where in the session's range the price sat.
On a day that grinds lower the price is below VWAP and at the session low
every hour, and the buy leg fired each cycle while the dip was still
extending; without a stack no dampener engaged at all.

``session_extreme_ibs`` (a band) and ``session_extreme_dampener`` scale the
emission when the buy leg fires with the session's IBS at or below the band,
or the sell leg at or above one minus it. Both are identity by default, so
every stored version computes what it computed before.

Made-up ticker and prices.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.strategies.intraday_vwap_zscore import IntradayVwapZscoreStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

_ATR = 6.0
_VWAP = 100.0
_WHEN = datetime(2026, 3, 4, 13, 30)


def _snapshot(price: float, ibs: float | None, *, stack: str = "none") -> MarketSnapshot:
    if stack == "bull":
        ema_9, ema_21, sma_20, sma_50 = price - 2, price - 4, 120.0, 100.0
    else:  # interleaved: no stack
        ema_9, ema_21, sma_20, sma_50 = price + 2, price - 4, 120.0, 100.0
    candles = [
        OHLCV(
            timestamp=datetime(2026, 3, 4, 10, (i * 5) % 60),
            open=c,
            high=c + 0.2,
            low=c - 0.2,
            close=c,
            volume=50000.0,
        )
        for i, c in enumerate(_VWAP + 0.05 * i % 0.2 for i in range(24))
    ]
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=_WHEN,
        quote=Quote(
            ticker="XYZ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=_WHEN,
        ),
        indicators=TechnicalIndicators(
            vwap=_VWAP,
            vwap_anchor="current_session",
            atr_14=_ATR,
            relative_volume=0.8,
            ibs=ibs,
            ema_9=ema_9,
            ema_21=ema_21,
            sma_20=sma_20,
            sma_50=sma_50,
        ),
        regime=RegimeClassification(regime=MarketRegime.RANGE_BOUND, confidence=0.6, reasoning="t"),
        recent_candles=candles,
    )


BELOW = _VWAP - 0.28 * _ATR  # a dip: the buy leg
ABOVE = _VWAP + 0.28 * _ATR  # a stretch: the sell leg


def _channel(**kw) -> IntradayVwapZscoreStrategy:
    kw.setdefault("entry_z", 1.1)
    kw.setdefault("z_scale", 0.5)
    kw.setdefault("staleness_decay", 1.0)
    return IntradayVwapZscoreStrategy(**kw)


ON = {"session_extreme_ibs": 0.15, "session_extreme_dampener": 0.5}


class TestOffByDefault:
    @pytest.mark.parametrize("ibs", [0.0, 0.05, 0.5, 0.95, 1.0])
    def test_at_the_defaults_the_reading_changes_nothing(self, ibs) -> None:
        for price in (BELOW, ABOVE):
            out = _channel().compute_signal(_snapshot(price, ibs))
            assert out.value == _channel().compute_signal(_snapshot(price, None)).value
            assert out.metadata["session_extreme_applied"] is False
            assert out.metadata["extreme_factor"] == 1.0
            assert out.metadata["session_extreme_band"] == 0.0

    def test_a_dampener_without_a_band_changes_nothing(self) -> None:
        snap = _snapshot(BELOW, 0.0)
        no_band = _channel(session_extreme_dampener=0.5).compute_signal(snap)
        assert no_band.value == _channel().compute_signal(snap).value


class TestAtTheSessionsExtreme:
    def test_the_buy_leg_at_the_low_is_damped(self) -> None:
        snap = _snapshot(BELOW, 0.10)
        plain, damped = _channel().compute_signal(snap), _channel(**ON).compute_signal(snap)
        assert plain.value > 0, "a dip below VWAP is faded with a buy"
        assert damped.value == pytest.approx(plain.value * 0.5, rel=1e-9)
        assert damped.metadata["session_extreme_applied"] is True
        assert damped.metadata["ibs_at_compute"] == 0.1

    def test_just_above_the_band_it_is_not(self) -> None:
        snap = _snapshot(BELOW, 0.23)
        assert _channel(**ON).compute_signal(snap).value == _channel().compute_signal(snap).value

    def test_the_sell_leg_at_the_high_is_damped(self) -> None:
        snap = _snapshot(ABOVE, 0.90)
        plain, damped = _channel().compute_signal(snap), _channel(**ON).compute_signal(snap)
        assert plain.value < 0
        assert damped.value == pytest.approx(plain.value * 0.5, rel=1e-9)

    def test_each_leg_only_at_its_own_extreme(self) -> None:
        for price, ibs in ((BELOW, 0.90), (ABOVE, 0.10)):
            snap = _snapshot(price, ibs)
            out = _channel(**ON).compute_signal(snap)
            assert out.metadata["session_extreme_applied"] is False
            assert out.value == _channel().compute_signal(snap).value

    def test_against_the_stack_both_dampeners_apply(self) -> None:
        snap = _snapshot(ABOVE, 0.90, stack="bull")  # a sell against a bull stack
        plain = _channel().compute_signal(snap)
        both = _channel(countertrend_dampener=0.5, **ON).compute_signal(snap)
        assert both.metadata["leg"] == "counter_trend"
        assert both.value == pytest.approx(plain.value * 0.25, rel=1e-9)


class TestTheReading:
    def test_no_reading_no_gate(self) -> None:
        snap = _snapshot(BELOW, None)
        out = _channel(**ON).compute_signal(snap)
        assert out.metadata["ibs_at_compute"] is None
        assert out.value == _channel().compute_signal(snap).value

    @pytest.mark.parametrize(("price", "ibs"), [(BELOW, -0.003), (ABOVE, 1.02)])
    def test_readings_past_the_range_are_clamped(self, price, ibs) -> None:
        out = _channel(**ON).compute_signal(_snapshot(price, ibs))
        assert out.metadata["session_extreme_applied"] is True
        assert out.metadata["ibs_at_compute"] in (0.0, 1.0)


class TestTheParameters:
    def test_they_round_trip(self) -> None:
        channel = _channel()
        channel.set_parameters(ON)
        params = channel.get_parameters()
        assert (params["session_extreme_ibs"], params["session_extreme_dampener"]) == (0.15, 0.5)

    @pytest.mark.parametrize(
        ("params", "problem"),
        [
            ({"session_extreme_ibs": 0.6}, "session_extreme_ibs must be in [0, 0.5]"),
            ({"session_extreme_ibs": -0.1}, "session_extreme_ibs must be in [0, 0.5]"),
            ({"session_extreme_dampener": 1.2}, "session_extreme_dampener must be in [0, 1]"),
        ],
    )
    def test_out_of_range_is_refused(self, params, problem) -> None:
        assert any(problem in e for e in _channel().validate_parameters(params))

    def test_the_proposed_values_are_valid(self) -> None:
        assert _channel().validate_parameters(ON) == []
