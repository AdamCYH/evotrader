"""Regression tests: regime confidence saturation and price-location override.

See: data/evolution/reviews/20260806_204035_regime_confidence_saturation_and_reconciliation_pnl_exclusion.md
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from evotrader.indicators.regime import detect_regime_rule_based
from evotrader.models.market import MarketRegime


def _make_price_series(
    *,
    base: float = 500.0,
    n: int = 60,
    sma20_above_sma50: bool = True,
    price_above_fast_mas: bool | None = None,
    spread_pct: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Build synthetic OHLCV series for regime detection.

    Args:
        base: Base price level.
        n: Number of bars.
        sma20_above_sma50: If True, SMA20 > SMA50 (bullish structure).
        price_above_fast_mas: If set, forces the latest price above/below
            EMA9, EMA21, SMA20.  None = let the structure decide.
        spread_pct: MA separation as a percentage of base.
    """
    # Create a simple upward or downward trend so MAs separate
    if sma20_above_sma50:
        # Price gradually increasing
        prices = np.linspace(base - base * spread_pct / 100, base, n)
    else:
        # Price gradually decreasing
        prices = np.linspace(base + base * spread_pct / 100, base, n)

    if price_above_fast_mas is True:
        # Force last few bars well above the moving averages
        # (V-recovery scenario: price jumps up but MAs still catching up)
        prices[-5:] = base * (1 + spread_pct / 100 * 1.5)
    elif price_above_fast_mas is False:
        # Force last few bars well below the moving averages
        prices[-5:] = base * (1 - spread_pct / 100 * 1.5)

    close = pd.Series(prices)
    high = close * 1.002
    low = close * 0.998
    volume = pd.Series([1_000_000] * n)
    return close, high, low, volume


class TestConfidenceVariation:
    """Finding #1: confidence must vary with MA separation, not be pinned."""

    def test_different_ma_separations_produce_different_confidence(self) -> None:
        """Two different MA spreads MUST produce different confidence values.

        The old code (``* 100``) added a percentage to a probability and
        clamped — every call beyond ~0.5% separation returned the same
        railed confidence.
        """
        # Small MA separation (0.5%)
        close_small, high_small, low_small, vol_small = _make_price_series(
            spread_pct=0.5,
        )
        result_small = detect_regime_rule_based(
            close_small,
            high_small,
            low_small,
            vol_small,
        )

        # Large MA separation (3.0%)
        close_large, high_large, low_large, vol_large = _make_price_series(
            spread_pct=3.0,
        )
        result_large = detect_regime_rule_based(
            close_large,
            high_large,
            low_large,
            vol_large,
        )

        # Both should be trending, but with DIFFERENT confidence
        assert result_small.confidence != result_large.confidence, (
            f"Confidence should vary with MA separation, but both returned "
            f"{result_small.confidence:.4f}"
        )

    def test_confidence_is_a_valid_probability(self) -> None:
        """Confidence must be in [0, 1]."""
        close, high, low, vol = _make_price_series(spread_pct=5.0)
        result = detect_regime_rule_based(close, high, low, vol)
        assert 0.0 <= result.confidence <= 1.0

    def test_confidence_not_permanently_at_ceiling(self) -> None:
        """With moderate MA separation, confidence should NOT be at 0.85/0.90."""
        close, high, low, vol = _make_price_series(spread_pct=1.0)
        result = detect_regime_rule_based(close, high, low, vol)
        # With 1% separation: base_conf=0.01, confidence ~ 0.35 + 0.1 + 0.25 = 0.70
        # It should NOT be at the old ceiling values
        assert result.confidence < 0.85, (
            f"Confidence {result.confidence:.4f} is at the old ceiling — "
            f"units fix may not have applied"
        )


class TestPriceLocationOverride:
    """Finding #2: stale bearish cross should flip to bullish when price reclaims fast MAs."""

    def test_bearish_cross_with_price_above_fast_mas_classifies_bullish(self) -> None:
        """V-recovery: SMA20 < SMA50 but price >> EMA9, EMA21, SMA20.

        The old code classified this as TRENDING_BEAR. After the fix
        the price-location override should produce TRENDING_BULL.
        """
        close, high, low, vol = _make_price_series(
            sma20_above_sma50=False,
            price_above_fast_mas=True,
            spread_pct=2.0,
        )
        result = detect_regime_rule_based(close, high, low, vol)

        assert result.regime == MarketRegime.TRENDING_BULL, (
            f"Expected TRENDING_BULL (price-location override), "
            f"got {result.regime.value} with confidence {result.confidence:.4f}"
        )

    def test_bullish_cross_with_price_below_fast_mas_classifies_bearish(self) -> None:
        """Mirror: SMA20 > SMA50 but price << EMA9, EMA21, SMA20.

        Should produce TRENDING_BEAR via the price-location override.
        """
        close, high, low, vol = _make_price_series(
            sma20_above_sma50=True,
            price_above_fast_mas=False,
            spread_pct=2.0,
        )
        result = detect_regime_rule_based(close, high, low, vol)

        assert result.regime == MarketRegime.TRENDING_BEAR, (
            f"Expected TRENDING_BEAR (price-location override), "
            f"got {result.regime.value} with confidence {result.confidence:.4f}"
        )

    def test_location_override_reduces_confidence(self) -> None:
        """When the override fires, confidence should be penalised (0.6x)."""
        # Normal bullish (no override needed)
        close_normal, high_n, low_n, vol_n = _make_price_series(
            sma20_above_sma50=True,
            spread_pct=2.0,
        )
        result_normal = detect_regime_rule_based(
            close_normal,
            high_n,
            low_n,
            vol_n,
        )

        # V-recovery (override fires)
        close_override, high_o, low_o, vol_o = _make_price_series(
            sma20_above_sma50=False,
            price_above_fast_mas=True,
            spread_pct=2.0,
        )
        result_override = detect_regime_rule_based(
            close_override,
            high_o,
            low_o,
            vol_o,
        )

        # Both bullish, but the override should have lower confidence
        if (
            result_normal.regime == MarketRegime.TRENDING_BULL
            and result_override.regime == MarketRegime.TRENDING_BULL
        ):
            assert result_override.confidence < result_normal.confidence, (
                f"Override confidence ({result_override.confidence:.4f}) should be "
                f"lower than normal ({result_normal.confidence:.4f})"
            )

    def test_aligned_cross_and_price_no_override(self) -> None:
        """When cross and price agree, no override fires."""
        close, high, low, vol = _make_price_series(
            sma20_above_sma50=True,
            spread_pct=2.0,
        )
        result = detect_regime_rule_based(close, high, low, vol)
        assert result.regime == MarketRegime.TRENDING_BULL


class TestLowParticipationAttenuation:
    """Finding #3: composite should be attenuated when few strategies vote."""

    def test_low_participation_reduces_composite(self) -> None:
        """With 2/7 strategies applicable, composite should be scaled down."""
        from unittest.mock import MagicMock

        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.models.market import (
            MarketRegime,
            MarketSnapshot,
            RegimeClassification,
        )
        from evotrader.models.signals import AlgoSignal

        # Create mock strategies: 7 total, only 2 applicable
        strategies = {}
        for i in range(7):
            strat = MagicMock()
            strat.name = f"strat_{i}"
            # First 2 emit voting signals, rest are applicable but silent (within_band)
            if i < 2:
                strat.compute_signal.return_value = AlgoSignal(
                    name=f"strat_{i}",
                    value=0.5,
                    weight=1.0,
                    metadata={"applicable": True},
                )
            else:
                strat.compute_signal.return_value = AlgoSignal(
                    name=f"strat_{i}",
                    value=0.0,
                    weight=1.0,
                    metadata={"applicable": True, "reason": "within_band"},
                )
            strategies[f"strat_{i}"] = strat

        weights = {f"strat_{i}": 1.0 / 7 for i in range(7)}

        composite = CompositeStrategy(
            sub_strategies=strategies,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )

        # Create a minimal snapshot
        snapshot = MagicMock(spec=MarketSnapshot)
        snapshot.regime = RegimeClassification(
            regime=MarketRegime.TRENDING_BULL,
            confidence=0.7,
            reasoning="test",
        )

        detailed = composite.compute_detailed_signal(snapshot)

        # 2/7 = 0.286, below MIN_PARTICIPATION (0.4)
        # Composite should be attenuated by (2/7) / 0.4 = 0.714
        # Without attenuation, renormalized 2 strategies at 0.5 each = 0.5
        # With attenuation: 0.5 * 0.714 ≈ 0.357
        assert detailed.composite_value < 0.5, (
            f"Low-participation composite ({detailed.composite_value:.4f}) "
            f"should be attenuated below 0.5"
        )

    def test_full_participation_no_attenuation(self) -> None:
        """With all strategies applicable, no attenuation fires."""
        from unittest.mock import MagicMock

        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.models.market import (
            MarketRegime,
            MarketSnapshot,
            RegimeClassification,
        )
        from evotrader.models.signals import AlgoSignal

        strategies = {}
        for i in range(4):
            strat = MagicMock()
            strat.name = f"strat_{i}"
            strat.compute_signal.return_value = AlgoSignal(
                name=f"strat_{i}",
                value=0.5,
                weight=1.0,
                metadata={"applicable": True},
            )
            strategies[f"strat_{i}"] = strat

        weights = {f"strat_{i}": 0.25 for i in range(4)}

        composite = CompositeStrategy(
            sub_strategies=strategies,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )

        snapshot = MagicMock(spec=MarketSnapshot)
        snapshot.regime = RegimeClassification(
            regime=MarketRegime.TRENDING_BULL,
            confidence=0.7,
            reasoning="test",
        )

        detailed = composite.compute_detailed_signal(snapshot)
        # All applicable, all at 0.5, equal weights → composite = 0.5 exactly
        assert abs(detailed.composite_value - 0.5) < 0.01, (
            f"Full-participation composite ({detailed.composite_value:.4f}) "
            f"should be ~0.5 without attenuation"
        )
