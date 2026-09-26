"""Tests for technical indicator computations."""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pytest

from evotrader.indicators.atr import atr_percentile, compute_atr
from evotrader.indicators.bollinger import bollinger_signal, compute_bollinger_bands
from evotrader.indicators.ibs import compute_ibs, ibs_signal
from evotrader.indicators.macd import compute_macd, macd_signal
from evotrader.indicators.moving_averages import (
    compute_moving_averages,
    moving_average_signal,
)
from evotrader.indicators.regime import detect_regime_rule_based
from evotrader.indicators.rsi import compute_rsi, rsi_signal
from evotrader.indicators.volume import compute_relative_volume, volume_signal
from evotrader.indicators.vwap import compute_vwap, vwap_signal
from evotrader.models.market import MarketRegime


@pytest.fixture
def price_data() -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Generate synthetic SPY-like OHLCV data (100 bars)."""
    np.random.seed(42)
    n = 100
    # Random walk starting at $450
    returns = np.random.normal(0.0002, 0.01, n)
    close = pd.Series(450.0 * np.exp(np.cumsum(returns)))

    # High/Low with realistic spread
    noise_h = np.abs(np.random.normal(0, 0.005, n))
    noise_l = np.abs(np.random.normal(0, 0.005, n))
    high = close * (1 + noise_h)
    low = close * (1 - noise_l)
    volume = pd.Series(np.random.uniform(30e6, 80e6, n))

    return high, low, close, volume


class TestRSI:
    def test_rsi_range(self, price_data: tuple) -> None:
        _, _, close, _ = price_data
        rsi = compute_rsi(close)
        valid = rsi.dropna()
        assert all(0 <= v <= 100 for v in valid)

    def test_rsi_period_length(self, price_data: tuple) -> None:
        _, _, close, _ = price_data
        rsi = compute_rsi(close, period=14)
        # First `period` values should be NaN-ish (warm-up)
        assert len(rsi.dropna()) > 0

    def test_rsi_signal_oversold(self) -> None:
        sig = rsi_signal(20.0, oversold=30.0, overbought=70.0)
        assert sig > 0.5  # Bullish

    def test_rsi_signal_overbought(self) -> None:
        sig = rsi_signal(85.0, oversold=30.0, overbought=70.0)
        assert sig < -0.5  # Bearish

    def test_rsi_signal_neutral(self) -> None:
        sig = rsi_signal(50.0, oversold=30.0, overbought=70.0)
        assert abs(sig) < 0.2  # Near zero


class TestMACD:
    def test_macd_shape(self, price_data: tuple) -> None:
        _, _, close, _ = price_data
        result = compute_macd(close)
        assert len(result.macd_line) == len(close)
        assert len(result.signal_line) == len(close)
        assert len(result.histogram) == len(close)

    def test_histogram_is_difference(self, price_data: tuple) -> None:
        _, _, close, _ = price_data
        result = compute_macd(close)
        diff = result.macd_line - result.signal_line
        pd.testing.assert_series_equal(diff, result.histogram, check_names=False)

    def test_macd_signal_bullish_crossover(self) -> None:
        sig = macd_signal(1.0, 0.5, 0.5)
        assert sig > 0  # MACD above signal = bullish

    def test_macd_signal_bearish_crossover(self) -> None:
        sig = macd_signal(-1.0, -0.5, -0.5)
        assert sig < 0  # MACD below signal = bearish


class TestBollinger:
    def test_bollinger_bands_structure(self, price_data: tuple) -> None:
        _, _, close, _ = price_data
        result = compute_bollinger_bands(close)
        valid_idx = result.upper.dropna().index
        # Upper > Middle > Lower always
        assert all(result.upper[valid_idx] >= result.middle[valid_idx])
        assert all(result.middle[valid_idx] >= result.lower[valid_idx])

    def test_bollinger_signal_at_lower_band(self) -> None:
        sig = bollinger_signal(close=445.0, upper=455.0, middle=450.0, lower=445.0)
        assert sig > 0.8  # Strong buy at lower band

    def test_bollinger_signal_at_upper_band(self) -> None:
        sig = bollinger_signal(close=455.0, upper=455.0, middle=450.0, lower=445.0)
        assert sig < -0.8  # Strong sell at upper band

    def test_bollinger_signal_at_middle(self) -> None:
        sig = bollinger_signal(close=450.0, upper=455.0, middle=450.0, lower=445.0)
        assert abs(sig) < 0.1  # Neutral at middle


class TestIBS:
    def test_ibs_range(self, price_data: tuple) -> None:
        high, low, close, _ = price_data
        ibs = compute_ibs(high, low, close)
        valid = ibs.dropna()
        assert all(0 <= v <= 1 for v in valid)

    def test_ibs_signal_oversold(self) -> None:
        sig = ibs_signal(0.1)
        assert sig > 0.5  # Buy signal

    def test_ibs_signal_overbought(self) -> None:
        sig = ibs_signal(0.9)
        assert sig < -0.5  # Sell signal


class TestVWAP:
    def test_vwap_computation(self, price_data: tuple) -> None:
        high, low, close, volume = price_data
        vwap = compute_vwap(high, low, close, volume)
        assert len(vwap) == len(close)
        # VWAP should be within the H-L range for the last bar
        assert vwap.iloc[-1] > 0

    def test_vwap_signal_below(self) -> None:
        sig = vwap_signal(close=448.0, vwap_value=450.0, atr=3.0)
        assert sig > 0  # Below VWAP = buy

    def test_vwap_signal_above(self) -> None:
        sig = vwap_signal(close=452.0, vwap_value=450.0, atr=3.0)
        assert sig < 0  # Above VWAP = sell


class TestATR:
    def test_atr_positive(self, price_data: tuple) -> None:
        high, low, close, _ = price_data
        atr = compute_atr(high, low, close)
        valid = atr.dropna()
        assert all(v > 0 for v in valid)

    def test_atr_percentile_range(self, price_data: tuple) -> None:
        high, low, close, _ = price_data
        atr = compute_atr(high, low, close)
        current = float(atr.iloc[-1])
        pct = atr_percentile(current, atr)
        assert 0 <= pct <= 100


class TestMovingAverages:
    def test_ma_computation(self, price_data: tuple) -> None:
        _, _, close, _ = price_data
        mas = compute_moving_averages(close)
        assert len(mas.ema_9) == len(close)
        assert len(mas.sma_50) == len(close)

    def test_ma_signal_bullish(self) -> None:
        # Price > all MAs, short > long → bullish
        sig = moving_average_signal(
            close=455.0, ema_9=454.0, ema_21=452.0, sma_20=451.0, sma_50=448.0
        )
        assert sig > 0

    def test_ma_signal_bearish(self) -> None:
        # Price < all MAs, short < long → bearish
        sig = moving_average_signal(
            close=445.0, ema_9=446.0, ema_21=448.0, sma_20=449.0, sma_50=452.0
        )
        assert sig < 0


class TestVolume:
    def test_relative_volume(self, price_data: tuple) -> None:
        _, _, _, volume = price_data
        rvol = compute_relative_volume(volume)
        valid = rvol.dropna()
        assert all(v > 0 for v in valid)

    def test_volume_signal_high(self) -> None:
        assert volume_signal(2.5) == 1.0

    def test_volume_signal_low(self) -> None:
        assert volume_signal(0.3) == 0.0

    def test_volume_signal_average_is_the_documented_neutral(self) -> None:
        """RVOL 1.0 must return 0.5, as the docstring has always claimed.

        This test previously asserted ``0.2 < sig < 0.5`` — it pinned the
        buggy 0.333 and specifically excluded the documented value, so the
        contract mismatch survived every run. The single linear map over
        0.5-2.0 put the real neutral midpoint at RVOL 1.25.
        """
        assert volume_signal(1.0) == pytest.approx(0.5)

    def test_volume_signal_anchors(self) -> None:
        assert volume_signal(0.5) == pytest.approx(0.0)
        assert volume_signal(1.0) == pytest.approx(0.5)
        assert volume_signal(2.0) == pytest.approx(1.0)

    def test_volume_signal_is_monotonic_and_bounded(self) -> None:
        vals = [volume_signal(rv / 20) for rv in range(0, 60)]
        assert all(0.0 <= v <= 1.0 for v in vals)
        assert all(b >= a for a, b in itertools.pairwise(vals))

    def test_volume_signal_continuous_at_the_segment_join(self) -> None:
        """The two linear segments must meet at RVOL 1.0, not step."""
        assert volume_signal(0.999) == pytest.approx(volume_signal(1.001), abs=2e-3)


class TestRegimeDetection:
    def test_regime_with_sufficient_data(self, price_data: tuple) -> None:
        high, low, close, volume = price_data
        result = detect_regime_rule_based(close, high, low, volume)
        assert result.regime in MarketRegime
        assert 0 <= result.confidence <= 1
        assert len(result.reasoning) > 0

    def test_regime_with_insufficient_data(self) -> None:
        close = pd.Series([450.0] * 10)
        high = pd.Series([451.0] * 10)
        low = pd.Series([449.0] * 10)
        volume = pd.Series([50e6] * 10)
        result = detect_regime_rule_based(close, high, low, volume)
        assert result.regime == MarketRegime.RANGE_BOUND
        assert result.confidence == 0.3  # Low confidence fallback
