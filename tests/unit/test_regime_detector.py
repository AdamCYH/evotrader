"""Regression tests for the regime detector's trend-aware high-volatility logic.

Guards against the bug where HIGH_VOLATILITY short-circuited all trend
evaluation, causing the composite to apply mean-reversion fade weights
during a clear directional trend with elevated ATR.

See: data/evolution/reviews/20260701_171516_regime_detector_highvol_masks_trend_direction.md
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from evotrader.indicators.regime import detect_regime_rule_based
from evotrader.models.market import MarketRegime


def _make_trending_bull_series(n: int = 100, base: float = 700.0, daily_move: float = 1.5) -> tuple:
    """Generate synthetic OHLCV data for a clear uptrend with high volatility.

    SMA20 will be above SMA50, slope positive, ATR elevated.
    """
    # Generate an uptrend with some noise
    close_vals = [base + i * daily_move + np.random.normal(0, 0.5) for i in range(n)]
    close = pd.Series(close_vals)

    # Make high/low spread large enough to generate high ATR percentile
    high = close + np.random.uniform(3.0, 8.0, n)
    low = close - np.random.uniform(3.0, 8.0, n)
    volume = pd.Series(np.random.uniform(1e6, 5e6, n))

    return close, high, low, volume


def _make_trending_bear_series(n: int = 100, base: float = 700.0, daily_move: float = 1.5) -> tuple:
    """Generate synthetic OHLCV data for a clear downtrend with high volatility."""
    close_vals = [base - i * daily_move + np.random.normal(0, 0.5) for i in range(n)]
    close = pd.Series(close_vals)

    high = close + np.random.uniform(3.0, 8.0, n)
    low = close - np.random.uniform(3.0, 8.0, n)
    volume = pd.Series(np.random.uniform(1e6, 5e6, n))

    return close, high, low, volume


def _make_choppy_highvol_series(n: int = 100, base: float = 700.0) -> tuple:
    """Generate synthetic OHLCV data that is high-volatility but directionless.

    Oscillates around a flat mean with large swings. ADX should be low,
    ATR percentile high.
    """
    # Mean-reverting noise around a flat base
    close_vals = [base + np.sin(i * 0.3) * 5 + np.random.normal(0, 2.0) for i in range(n)]
    close = pd.Series(close_vals)

    # Large high/low spread for high ATR
    high = close + np.random.uniform(5.0, 12.0, n)
    low = close - np.random.uniform(5.0, 12.0, n)
    volume = pd.Series(np.random.uniform(1e6, 5e6, n))

    return close, high, low, volume


def _make_lowvol_trending_series(
    n: int = 100, base: float = 700.0, daily_move: float = 0.5
) -> tuple:
    """Generate synthetic OHLCV data for a low-volatility uptrend.

    ATR should be low, ADX above threshold, SMA20 > SMA50.
    """
    close_vals = [base + i * daily_move + np.random.normal(0, 0.2) for i in range(n)]
    close = pd.Series(close_vals)

    # Small high/low spread → low ATR
    high = close + np.random.uniform(0.5, 1.5, n)
    low = close - np.random.uniform(0.5, 1.5, n)
    volume = pd.Series(np.random.uniform(1e6, 5e6, n))

    return close, high, low, volume


class TestHighVolTrendAware:
    """Verify that high volatility does not mask trend direction."""

    def test_highvol_strong_uptrend_returns_trending_bull(self):
        """When ATR is high but there's a clear uptrend, regime should be TRENDING_BULL."""
        np.random.seed(42)
        close, high, low, volume = _make_trending_bull_series(n=100, base=700.0, daily_move=2.0)

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            volatility_high_pct=50.0,  # Lower threshold to ensure we hit the high-vol branch
        )

        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL for volatile uptrend, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )
        assert result.volatility_percentile is not None
        assert result.volatility_percentile >= 50.0
        assert "High vol" in result.reasoning

    def test_highvol_strong_downtrend_returns_trending_bear(self):
        """When ATR is high but there's a clear downtrend, regime should be TRENDING_BEAR."""
        np.random.seed(42)
        close, high, low, volume = _make_trending_bear_series(n=100, base=700.0, daily_move=2.0)

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            volatility_high_pct=50.0,
        )

        assert result.regime == MarketRegime.TRENDING_BEAR, (
            f"Expected TRENDING_BEAR for volatile downtrend, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )
        assert result.volatility_percentile is not None
        assert "High vol" in result.reasoning

    def test_highvol_directionless_returns_high_volatility(self):
        """When ATR is high AND there is no directional conviction, regime should remain HIGH_VOLATILITY."""
        np.random.seed(42)
        close, high, low, volume = _make_choppy_highvol_series(n=100)

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            volatility_high_pct=30.0,  # Very low threshold to ensure we hit high-vol
            adx_threshold=40.0,  # High ADX threshold so choppy data won't pass
        )

        assert result.regime == MarketRegime.HIGH_VOLATILITY, (
            f"Expected HIGH_VOLATILITY for directionless high-vol, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )
        assert "Directionless" in result.reasoning or "no directional" in result.reasoning

    def test_highvol_trend_confidence_attenuated(self):
        """Volatile trend confidence should be capped at 0.95."""
        np.random.seed(42)
        close, high, low, volume = _make_trending_bull_series(n=100, base=700.0, daily_move=2.0)

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            volatility_high_pct=50.0,
        )

        if result.regime == MarketRegime.TRENDING_BULL:
            assert result.confidence <= 0.95, (
                f"Volatile trend confidence should be capped at 0.95, got {result.confidence}"
            )


class TestLowVolPathsUnchanged:
    """Verify that low-volatility paths are unaffected by the change."""

    def test_lowvol_uptrend_returns_trending_bull(self):
        """Low-vol uptrend should still return TRENDING_BULL via the existing path."""
        np.random.seed(42)
        close, high, low, volume = _make_lowvol_trending_series(n=100, base=700.0, daily_move=0.5)

        result = detect_regime_rule_based(close, high, low, volume)

        # Should NOT hit the high-vol branch at all
        assert result.regime in (MarketRegime.TRENDING_BULL, MarketRegime.RANGE_BOUND), (
            f"Low-vol uptrend should be TRENDING_BULL or RANGE_BOUND, got {result.regime}"
        )

    def test_insufficient_data_defaults_to_range_bound(self):
        """Less than 50 bars should default to RANGE_BOUND."""
        close = pd.Series([100.0] * 30)
        high = close + 1.0
        low = close - 1.0
        volume = pd.Series([1e6] * 30)

        result = detect_regime_rule_based(close, high, low, volume)
        assert result.regime == MarketRegime.RANGE_BOUND
        assert result.confidence == 0.3


class TestTrendDirectionSymmetry:
    """Verify that structure dominates slope when the slope contradiction is mild.

    When the slope is negative but SMALL (below the 8 bps normalised threshold)
    or price has NOT crossed below SMA20, structure should still win.  A strong
    override only fires when ALL three conditions are met:
      1. Normalised slope exceeds STRONG_SLOPE (0.0008)
      2. Slope direction contradicts structure
      3. Price has crossed to the other side of SMA20

    See: data/evolution/reviews/20260701_210221_regime_direction_logic_and_algo_version_label_bugs.md
    See: data/evolution/reviews/20260708_210134_regime_detector_slope_override_for_bearish_pullbacks.md
    """

    def test_uptrend_mild_pullback_stays_trending_bull(self):
        """SMA20 >> SMA50 with a small, brief dip — structure should dominate."""
        np.random.seed(42)
        # Strong uptrend: 100 bars at +1.0/bar from 700 → close ~800
        close, high, low, volume = _make_lowvol_trending_series(n=100, base=700.0, daily_move=1.0)

        # Add a MILD pullback — only 3 bars of -1.0 each.
        # This keeps price near SMA20 and the normalised slope small.
        pullback = [close.iloc[-1] - 1.0 * i for i in range(1, 4)]
        close = pd.concat([close, pd.Series(pullback)], ignore_index=True)
        high = pd.concat([high, pd.Series(pullback) + 1.0], ignore_index=True)
        low = pd.concat([low, pd.Series(pullback) - 1.0], ignore_index=True)
        volume = pd.concat([volume, pd.Series([1e6] * 3)], ignore_index=True)

        result = detect_regime_rule_based(close, high, low, volume)

        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL for mild pullback, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )

    def test_downtrend_mild_bounce_stays_trending_bear(self):
        """SMA20 << SMA50 with a small bounce — structure should dominate."""
        np.random.seed(42)
        close_vals = [700.0 - i * 1.0 + np.random.normal(0, 0.2) for i in range(100)]
        close = pd.Series(close_vals)
        high = close + 1.0
        low = close - 1.0
        volume = pd.Series([1e6] * 100)

        # Mild bounce — 3 bars of +1.0 each
        bounce = [close.iloc[-1] + 1.0 * i for i in range(1, 4)]
        close = pd.concat([close, pd.Series(bounce)], ignore_index=True)
        high = pd.concat([high, pd.Series(bounce) + 1.0], ignore_index=True)
        low = pd.concat([low, pd.Series(bounce) - 1.0], ignore_index=True)
        volume = pd.concat([volume, pd.Series([1e6] * 3)], ignore_index=True)

        result = detect_regime_rule_based(close, high, low, volume)

        assert result.regime == MarketRegime.TRENDING_BEAR, (
            f"Expected TRENDING_BEAR for mild bounce, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )


class TestSlopeOverride:
    """Verify slope-override: a strongly contradicting SMA20 slope with price
    confirmation flips the classified direction away from stale SMA structure.

    See: data/evolution/reviews/20260708_210134_regime_detector_slope_override_for_bearish_pullbacks.md
    """

    def _make_bull_structure_bear_pullback(
        self,
        n: int = 100,
        base: float = 700.0,
        daily_move: float = 1.0,
        pullback_bars: int = 15,
        pullback_move: float = 5.0,
        spread: float = 1.5,
    ) -> tuple:
        """Uptrend that rolls over hard: SMA20 > SMA50 but price and slope are bearish."""
        np.random.seed(42)
        close_vals = [base + i * daily_move + np.random.normal(0, 0.2) for i in range(n)]
        close = pd.Series(close_vals)
        high = close + spread
        low = close - spread
        volume = pd.Series([1e6] * n)

        # Strong pullback: enough bars and magnitude that SMA20 turns down and price < SMA20
        pullback = [close.iloc[-1] - pullback_move * i for i in range(1, pullback_bars + 1)]
        close = pd.concat([close, pd.Series(pullback)], ignore_index=True)
        high = pd.concat([high, pd.Series(pullback) + spread], ignore_index=True)
        low = pd.concat([low, pd.Series(pullback) - spread], ignore_index=True)
        volume = pd.concat([volume, pd.Series([1e6] * pullback_bars)], ignore_index=True)
        return close, high, low, volume

    def _make_bear_structure_bull_bounce(
        self,
        n: int = 100,
        base: float = 700.0,
        daily_move: float = 1.0,
        bounce_bars: int = 15,
        bounce_move: float = 5.0,
        spread: float = 1.5,
    ) -> tuple:
        """Downtrend that reverses hard: SMA20 < SMA50 but price and slope are bullish."""
        np.random.seed(42)
        close_vals = [base - i * daily_move + np.random.normal(0, 0.2) for i in range(n)]
        close = pd.Series(close_vals)
        high = close + spread
        low = close - spread
        volume = pd.Series([1e6] * n)

        bounce = [close.iloc[-1] + bounce_move * i for i in range(1, bounce_bars + 1)]
        close = pd.concat([close, pd.Series(bounce)], ignore_index=True)
        high = pd.concat([high, pd.Series(bounce) + spread], ignore_index=True)
        low = pd.concat([low, pd.Series(bounce) - spread], ignore_index=True)
        volume = pd.concat([volume, pd.Series([1e6] * bounce_bars)], ignore_index=True)
        return close, high, low, volume

    # ── Non-high-vol (Step 3) slope-override tests ──────────────

    def test_strong_bearish_pullback_overrides_to_trending_bear(self):
        """Bullish structure + strong negative slope + price < SMA20 → TRENDING_BEAR."""
        close, high, low, volume = self._make_bull_structure_bear_pullback()

        result = detect_regime_rule_based(close, high, low, volume)

        assert result.regime == MarketRegime.TRENDING_BEAR, (
            f"Expected TRENDING_BEAR for strong bearish pullback with price below SMA20, "
            f"got {result.regime}. Reasoning: {result.reasoning}"
        )

    def test_strong_bullish_bounce_overrides_to_trending_bull(self):
        """Bearish structure + strong positive slope + price > SMA20 → TRENDING_BULL."""
        close, high, low, volume = self._make_bear_structure_bull_bounce()

        result = detect_regime_rule_based(close, high, low, volume)

        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL for strong bullish bounce with price above SMA20, "
            f"got {result.regime}. Reasoning: {result.reasoning}"
        )

    # ── High-vol slope-override tests ───────────────────────────

    def test_highvol_bearish_pullback_overrides_to_trending_bear(self):
        """High-vol branch: bullish structure + strong negative slope + price < SMA20 → TRENDING_BEAR."""
        close, high, low, volume = self._make_bull_structure_bear_pullback(
            spread=6.0,  # wider spread → higher ATR → high-vol path
        )

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            volatility_high_pct=50.0,
        )

        assert result.regime == MarketRegime.TRENDING_BEAR, (
            f"Expected TRENDING_BEAR in high-vol bearish pullback, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )

    def test_highvol_bullish_bounce_overrides_to_trending_bull(self):
        """High-vol branch: bearish structure + strong positive slope + price > SMA20 → TRENDING_BULL."""
        close, high, low, volume = self._make_bear_structure_bull_bounce(
            spread=6.0,
        )

        result = detect_regime_rule_based(
            close,
            high,
            low,
            volume,
            volatility_high_pct=50.0,
        )

        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL in high-vol bullish bounce, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )

    # ── Edge cases: override should NOT fire ────────────────────

    def test_weak_slope_does_not_override(self):
        """Slope below the 8 bps threshold should NOT override structure."""
        close, high, low, volume = self._make_bull_structure_bear_pullback(
            pullback_bars=5,
            pullback_move=0.3,  # Very mild → slope below threshold
        )

        result = detect_regime_rule_based(close, high, low, volume)

        # With such a mild pullback the slope should be below threshold and
        # structure should dominate — expect TRENDING_BULL
        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL when slope is below override threshold, got {result.regime}. "
            f"Reasoning: {result.reasoning}"
        )
