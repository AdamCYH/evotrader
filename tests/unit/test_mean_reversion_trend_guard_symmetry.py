"""Regression tests: mean_reversion's trend guard decays the counter-trend leg in BOTH trends.

The guard sat under ``if signal_value > 0``, so its uptrend branch
(``bull_stack and signal_value < 0``) could never run. Every regular-hours cycle
of MSTR 2026-09-22..24 had a full bull stack, RSI 64-71 and mean_reversion
-0.15..-0.25 with no ``trend_guard_applied`` — the channel dissented at full
strength on every cycle of an uptrend while its downtrend mirror worked.

The fixture is the live cycle the review cites: 2026-09-24 15:30 ET, session
81ff74ee, under v029. Its stored indicators reproduce the live components to
the last digit, so the fixture is the cycle, not an approximation of it.

See: data/evolution/reviews/20260924_224217_mean_reversion_bull_stack_guard_unreachable_strategy_stage_503_skip.md
"""

from __future__ import annotations

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

# v029's mean_reversion parameters (live on 2026-09-24).
_V029 = dict(
    rsi_oversold=28,
    rsi_overbought=72,
    ibs_oversold=0.2,
    ibs_overbought=0.8,
    rsi_weight=0.6,
    bollinger_weight=0.2,
    ibs_weight=0.2,
    trend_fade_decay=0.2,
)

# market_snapshots row for session 81ff74ee-fd8d-4efd-b15e-d2c949655cfa.
_LIVE_0924_1530 = dict(
    close=163.165,
    rsi_14=67.364559,
    ibs=0.7117411744094371,
    ema_9=150.393477,
    ema_21=137.80381,
    sma_20=138.15825,
    sma_50=115.1087,
    bollinger_upper=166.851157,
    bollinger_middle=138.15825,
    bollinger_lower=109.465343,
)
# What the live engine emitted for that cycle, undecayed.
_LIVE_VALUE = -0.2327624286477931
_LIVE_COMPONENTS = {
    "rsi_signal": -0.3157192545454546,
    "bollinger_signal": -0.17430614472071432,
    "ibs_signal": -0.04234823488188741,
}


def _snapshot(**overrides: float | None) -> MarketSnapshot:
    v = {**_LIVE_0924_1530, **overrides}
    ts = datetime(2026, 9, 24, 19, 30, tzinfo=UTC)
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=ts,
        quote=Quote(
            ticker="MSTR", bid=v["close"], ask=v["close"], last=v["close"], volume=0.0, timestamp=ts
        ),
        indicators=TechnicalIndicators(
            rsi_14=v["rsi_14"],
            ibs=v["ibs"],
            ema_9=v["ema_9"],
            ema_21=v["ema_21"],
            sma_20=v["sma_20"],
            sma_50=v["sma_50"],
            bollinger_upper=v["bollinger_upper"],
            bollinger_middle=v["bollinger_middle"],
            bollinger_lower=v["bollinger_lower"],
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL,
            confidence=0.85,
            reasoning="fixture",
        ),
    )


@pytest.fixture
def mr() -> MeanReversionStrategy:
    return MeanReversionStrategy(**_V029)


class TestLiveFixture:
    def test_fixture_reproduces_the_live_components(self, mr: MeanReversionStrategy) -> None:
        """The components are computed before the guard, so they must match the
        live cycle exactly — otherwise the fixture is not that cycle."""
        sig = mr.compute_signal(_snapshot())
        for name, live in _LIVE_COMPONENTS.items():
            assert sig.metadata[name] == pytest.approx(live, abs=1e-12)

    def test_sell_in_bull_stack_is_decayed(self, mr: MeanReversionStrategy) -> None:
        """Bull stack true on every term, SELL-leaning read -> decayed by 0.2."""
        sig = mr.compute_signal(_snapshot())
        assert sig.metadata["trend_stack"] == "bull"
        assert sig.metadata["trend_guard_applied"] is True
        assert sig.value == pytest.approx(_LIVE_VALUE * 0.2, abs=1e-9)
        assert sig.value < 0, "decay shrinks the dissent; it must not flip it"


class TestGuardDecaysOnlyTheCounterTrendLeg:
    def test_buy_in_bear_stack_still_decayed(self, mr: MeanReversionStrategy) -> None:
        """The branch that always worked keeps working (mirror of the fixture)."""
        bear = _snapshot(
            close=100.0,
            rsi_14=32.0,
            ibs=0.15,
            ema_9=105.0,
            ema_21=110.0,
            sma_20=108.0,
            sma_50=115.0,
            bollinger_upper=125.0,
            bollinger_middle=112.0,
            bollinger_lower=99.0,
        )
        none = _snapshot(
            close=100.0,
            rsi_14=32.0,
            ibs=0.15,
            ema_9=98.0,
            ema_21=110.0,
            sma_20=108.0,
            sma_50=115.0,
            bollinger_upper=125.0,
            bollinger_middle=112.0,
            bollinger_lower=99.0,
        )
        s_bear, s_none = mr.compute_signal(bear), mr.compute_signal(none)
        assert s_none.value > 0 and s_none.metadata["trend_stack"] == "none"
        assert s_bear.metadata["trend_stack"] == "bear"
        assert s_bear.metadata["trend_guard_applied"] is True
        assert s_bear.value == pytest.approx(s_none.value * 0.2)

    def test_with_trend_buy_in_bull_stack_untouched(self, mr: MeanReversionStrategy) -> None:
        """A BUY-leaning read in an uptrend agrees with the trend — no decay."""
        sig = mr.compute_signal(_snapshot(rsi_14=40.0, ibs=0.15))
        assert sig.value > 0
        assert sig.metadata["trend_stack"] == "bull"
        assert sig.metadata["trend_guard_applied"] is False

    def test_with_trend_sell_in_bear_stack_untouched(self, mr: MeanReversionStrategy) -> None:
        """A SELL-leaning read in a downtrend agrees with the trend — no decay."""
        sig = mr.compute_signal(
            _snapshot(
                close=100.0,
                rsi_14=60.0,
                ibs=0.9,
                ema_9=105.0,
                ema_21=110.0,
                sma_20=108.0,
                sma_50=115.0,
                bollinger_upper=125.0,
                bollinger_middle=112.0,
                bollinger_lower=99.0,
            )
        )
        assert sig.value < 0
        assert sig.metadata["trend_stack"] == "bear"
        assert sig.metadata["trend_guard_applied"] is False

    def test_sell_with_mixed_stack_untouched(self, mr: MeanReversionStrategy) -> None:
        """Price below ema_9 breaks the bull stack: no confirmed trend, no decay."""
        sig = mr.compute_signal(_snapshot(close=149.0))
        assert sig.value < 0
        assert sig.metadata["trend_stack"] == "none"
        assert sig.metadata["trend_guard_applied"] is False

    def test_missing_moving_averages_reported_not_guessed(self, mr: MeanReversionStrategy) -> None:
        sig = mr.compute_signal(_snapshot(sma_50=None))
        assert sig.metadata["trend_stack"] == "unavailable"
        assert sig.metadata["trend_guard_applied"] is False
        assert sig.value == pytest.approx(_LIVE_VALUE, abs=1e-9)

    def test_decay_is_the_configured_parameter(self) -> None:
        # trend_fade_decay also attenuates the Bollinger and IBS components, so
        # the undecayed reference is the same strategy with the stack unreadable.
        mr = MeanReversionStrategy(**{**_V029, "trend_fade_decay": 0.5})
        guarded = mr.compute_signal(_snapshot())
        reference = mr.compute_signal(_snapshot(sma_50=None))
        assert guarded.value == pytest.approx(reference.value * 0.5, abs=1e-9)
