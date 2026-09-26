"""Regression tests: Regime intraday trend override for ADX-lagging down-days.

Guards against the bug where daily ADX stayed below 25 on a -2.38% intraday
selloff, classifying the market as range_bound. This zeroed momentum weight
and over-weighted mean-reversion, producing a persistent long lean into a
falling market.

All _pct values use PERCENT units: -2.38 means -2.38%.

See: data/evolution/reviews/20260723_210605_range_break_silence_and_regime_downday_blindspot.md
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from evotrader.indicators.regime import detect_regime_rule_based
from evotrader.models.market import MarketRegime


def _make_rangebound_series(
    n: int = 100,
    base: float = 700.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Generate synthetic OHLCV data that ADX will classify as range_bound.

    Flat oscillation around ``base`` with small high/low spread → low ADX.
    """
    np.random.seed(42)
    close_vals = [base + np.sin(i * 0.3) * 2 + np.random.normal(0, 0.5) for i in range(n)]
    close = pd.Series(close_vals)
    high = close + np.random.uniform(0.5, 2.0, n)
    low = close - np.random.uniform(0.5, 2.0, n)
    volume = pd.Series(np.random.uniform(1e6, 5e6, n))
    return close, high, low, volume


class TestIntradayTrendOverride:
    """Verify that strong intraday moves override range_bound classification."""

    def test_bearish_override_on_large_down_day(self):
        """7/23 replay: -2.38% day, price < VWAP, MACD < 0 → TRENDING_BEAR."""
        close, high, low, volume = _make_rangebound_series()

        # Confirm baseline is range_bound without intraday params
        baseline = detect_regime_rule_based(close, high, low, volume)
        assert baseline.regime == MarketRegime.RANGE_BOUND, (
            f"Pre-condition: expected RANGE_BOUND without override, got {baseline.regime}"
        )

        # Now add intraday params matching the 7/23 scenario
        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-2.38,  # -2.38%
            vwap=float(close.iloc[-1]) + 5.0,  # price below VWAP
            macd_histogram=-3.033,
        )

        assert result.regime == MarketRegime.TRENDING_BEAR, (
            f"Expected TRENDING_BEAR on -2.38% day with VWAP/MACD confirm, "
            f"got {result.regime}. Reasoning: {result.reasoning}"
        )
        assert "Intraday trend override" in result.reasoning
        assert result.confidence >= 0.45
        assert result.confidence <= 0.65

    def test_bullish_override_on_large_up_day(self):
        """+2.0% day, price > VWAP, MACD > 0 → TRENDING_BULL."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=2.0,  # +2.0%
            vwap=float(close.iloc[-1]) - 5.0,  # price above VWAP
            macd_histogram=2.5,
        )

        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL on +2.0% day, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )
        assert "Intraday trend override" in result.reasoning

    def test_small_move_does_not_override(self):
        """-0.95% day (below 1.5% threshold) should stay RANGE_BOUND."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-0.95,  # -0.95%, below 1.5% threshold
            vwap=float(close.iloc[-1]) + 3.0,
            macd_histogram=-1.5,
        )

        assert result.regime == MarketRegime.RANGE_BOUND, (
            f"Expected RANGE_BOUND for -0.95% day (below threshold), "
            f"got {result.regime}. Reasoning: {result.reasoning}"
        )

    def test_direction_mismatch_does_not_override(self):
        """-2.0% day but price ABOVE VWAP → no override (direction mismatch)."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-2.0,  # -2.0%
            vwap=float(close.iloc[-1]) - 5.0,  # price ABOVE vwap → mismatch
            macd_histogram=-2.0,
        )

        assert result.regime == MarketRegime.RANGE_BOUND, (
            f"Expected RANGE_BOUND when price is above VWAP on a down day, got {result.regime}"
        )

    def test_macd_mismatch_does_not_override(self):
        """-2.0% day, price < VWAP, but MACD histogram POSITIVE → no override."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-2.0,  # -2.0%
            vwap=float(close.iloc[-1]) + 5.0,
            macd_histogram=1.5,  # positive → doesn't confirm
        )

        assert result.regime == MarketRegime.RANGE_BOUND, (
            f"Expected RANGE_BOUND when MACD doesn't confirm, got {result.regime}"
        )


class TestBackwardsCompatibility:
    """Verify that calling without new params behaves identically."""

    def test_no_intraday_params_returns_range_bound(self):
        """Without daily_change_pct/vwap/macd_histogram, the override
        cannot fire — behaviour is identical to the old code."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(close, high, low, volume)

        assert result.regime == MarketRegime.RANGE_BOUND

    def test_partial_intraday_params_no_override(self):
        """If only some intraday params are provided, override doesn't fire."""
        close, high, low, volume = _make_rangebound_series()

        # daily_change_pct present, but vwap is None
        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-2.5,  # -2.5%
            vwap=None,
            macd_histogram=-3.0,
        )

        assert result.regime == MarketRegime.RANGE_BOUND

    def test_none_daily_change_no_override(self):
        """Explicit None for daily_change_pct should not trigger override."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=None,
            vwap=700.0,
            macd_histogram=-3.0,
        )

        assert result.regime == MarketRegime.RANGE_BOUND


class TestConfidenceScaling:
    """Verify that override confidence scales correctly."""

    def test_confidence_at_threshold(self):
        """At exactly 1.5% move, confidence should be ~0.525."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-1.5,  # exactly -1.5%
            vwap=float(close.iloc[-1]) + 5.0,
            macd_histogram=-2.0,
        )

        assert result.regime == MarketRegime.TRENDING_BEAR
        # 0.45 + 1.5 * 0.05 = 0.525
        assert result.confidence == pytest.approx(0.525, abs=0.01)

    def test_confidence_capped_at_065(self):
        """Even for a very large move, confidence should cap at 0.65."""
        close, high, low, volume = _make_rangebound_series()

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            daily_change_pct=-5.0,  # -5%, very large
            vwap=float(close.iloc[-1]) + 10.0,
            macd_histogram=-5.0,
        )

        assert result.regime == MarketRegime.TRENDING_BEAR
        assert result.confidence == 0.65, (
            f"Expected confidence capped at 0.65, got {result.confidence}"
        )
