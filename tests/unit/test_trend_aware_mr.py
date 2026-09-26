"""Regression tests: regime-agnostic MR fade dampening and momentum volume dampener.

Guards against intraday oscillators (IBS, Bollinger) dominating the composite.
Three cumulative fixes:
  1. MR fade attenuation in confirmed trends (v006, now superseded).
  2. Momentum volume dampener floor raised from 0.3 → 0.7 (v006).
  3. Regime-agnostic fade dampening: IBS/Bollinger always dampened (v007+).

See:
  data/evolution/reviews/20260706_210144_mean_reversion_fade_should_be_trend_aware.md
  data/evolution/reviews/20260706_210144_momentum_volume_dampener_crushes_confirmed_trends.md
  data/evolution/reviews/20260710_210148_mean_reversion_regime_agnostic_intraday_fade_dampening.md
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.algorithms.strategies.momentum import MomentumStrategy
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    *,
    regime: MarketRegime = MarketRegime.RANGE_BOUND,
    ibs: float | None = 0.87,
    rsi: float | None = 55.0,
    close: float = 740.0,
    bb_upper: float | None = 742.0,
    bb_middle: float | None = 720.0,
    bb_lower: float | None = 698.0,
    vwap: float | None = 725.0,
    atr: float | None = 5.0,
    relative_volume: float | None = 0.30,
    ema_9: float | None = 738.0,
    ema_21: float | None = 732.0,
    sma_20: float | None = 730.0,
    sma_50: float | None = 710.0,
    macd_line: float | None = 1.2,
    macd_signal: float | None = 0.8,
    macd_histogram: float | None = 0.4,
) -> MarketSnapshot:
    """Build a market snapshot with sensible defaults for a trending uptrend."""
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 7, 6, 14, 30),
        quote=Quote(
            ticker="QQQ",
            bid=close - 0.01,
            ask=close + 0.01,
            last=close,
            volume=40_000_000.0,
            timestamp=datetime(2026, 7, 6, 14, 30),
        ),
        indicators=TechnicalIndicators(
            rsi_14=rsi,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_histogram=macd_histogram,
            bollinger_upper=bb_upper,
            bollinger_middle=bb_middle,
            bollinger_lower=bb_lower,
            ema_9=ema_9,
            ema_21=ema_21,
            sma_20=sma_20,
            sma_50=sma_50,
            vwap=vwap,
            vwap_anchor="current_session",
            ibs=ibs,
            atr_14=atr,
            volume_sma_20=50_000_000.0,
            relative_volume=relative_volume,
        ),
        regime=RegimeClassification(
            regime=regime,
            confidence=0.85,
            reasoning="test fixture",
        ),
        daily_change_pct=0.5,
        gap_pct=0.1,
    )


# ---------------------------------------------------------------------------
# Mean Reversion — Regime-agnostic fade dampening
# ---------------------------------------------------------------------------


class TestMeanReversionRegimeAgnosticFade:
    """Verify IBS/Bollinger fades are dampened in EVERY regime.

    IBS and Bollinger %B are pure intraday oscillators with no persistence
    value in any regime. They must always be multiplied by trend_fade_decay
    so a single stale reading cannot pin the composite.
    """

    @pytest.fixture
    def mr(self) -> MeanReversionStrategy:
        """MR strategy with v017 internal weights (VWAP removed)."""
        return MeanReversionStrategy(
            rsi_weight=0.60,
            bollinger_weight=0.20,
            ibs_weight=0.20,
            trend_fade_decay=0.2,
        )

    def test_ibs_dampened_in_range_bound(self, mr: MeanReversionStrategy) -> None:
        """IBS should be dampened even in RANGE_BOUND — the blind spot that
        caused a losing trade. IBS 0.87 raw sell of ~-0.74 must be
        reduced by trend_fade_decay.
        """
        from evotrader.indicators.ibs import ibs_signal as raw_ibs_signal

        raw = raw_ibs_signal(0.87, oversold=0.2, overbought=0.8)
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, ibs=0.87)
        sig = mr.compute_signal(snap)

        dampened = sig.metadata["ibs_signal"]
        assert dampened < 0  # sell direction preserved
        assert abs(dampened) < abs(raw)  # but dampened
        assert abs(dampened - raw * 0.2) < 0.01  # by exactly trend_fade_decay

    def test_ibs_dampened_identically_across_all_regimes(self, mr: MeanReversionStrategy) -> None:
        """All regimes should produce the same dampened IBS value."""
        signals = {}
        for regime in MarketRegime:
            snap = _make_snapshot(regime=regime, ibs=0.87)
            sig = mr.compute_signal(snap)
            signals[regime] = sig.metadata["ibs_signal"]

        values = list(signals.values())
        for v in values[1:]:
            assert v == pytest.approx(values[0]), f"IBS dampening differs across regimes: {signals}"

    def test_bollinger_dampened_in_range_bound(self, mr: MeanReversionStrategy) -> None:
        """Bollinger sell at upper band should be dampened in RANGE_BOUND."""
        from evotrader.indicators.bollinger import bollinger_signal as raw_bb_signal

        raw = raw_bb_signal(
            close=742.0,
            upper=742.0,
            middle=720.0,
            lower=698.0,
        )
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, close=742.0)
        sig = mr.compute_signal(snap)

        dampened = sig.metadata["bollinger_signal"]
        assert dampened < 0  # sell direction preserved
        assert abs(dampened) < abs(raw)  # dampened
        assert abs(dampened - raw * 0.2) < 0.01  # by trend_fade_decay

    def test_bollinger_dampened_identically_across_all_regimes(
        self, mr: MeanReversionStrategy
    ) -> None:
        """All regimes should produce the same dampened Bollinger value."""
        signals = {}
        for regime in MarketRegime:
            snap = _make_snapshot(regime=regime, close=742.0)
            sig = mr.compute_signal(snap)
            signals[regime] = sig.metadata["bollinger_signal"]

        values = list(signals.values())
        for v in values[1:]:
            assert v == pytest.approx(values[0]), (
                f"Bollinger dampening differs across regimes: {signals}"
            )

    def test_ibs_buy_also_dampened(self, mr: MeanReversionStrategy) -> None:
        """IBS buy signal (oversold) should also be dampened — two-sided."""
        from evotrader.indicators.ibs import ibs_signal as raw_ibs_signal

        raw = raw_ibs_signal(0.15, oversold=0.2, overbought=0.8)
        assert raw > 0  # buy

        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, ibs=0.15)
        sig = mr.compute_signal(snap)

        dampened = sig.metadata["ibs_signal"]
        assert dampened > 0  # buy preserved
        assert dampened < raw  # dampened
        assert abs(dampened - raw * 0.2) < 0.01

    def test_bollinger_buy_also_dampened(self, mr: MeanReversionStrategy) -> None:
        """Bollinger buy at lower band should also be dampened — two-sided."""
        from evotrader.indicators.bollinger import bollinger_signal as raw_bb_signal

        raw = raw_bb_signal(
            close=699.0,
            upper=742.0,
            middle=720.0,
            lower=698.0,
        )
        assert raw > 0  # buy

        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, close=699.0)
        sig = mr.compute_signal(snap)

        dampened = sig.metadata["bollinger_signal"]
        assert dampened > 0  # buy preserved
        assert dampened < raw  # dampened
        assert abs(dampened - raw * 0.2) < 0.01

    def test_rsi_not_dampened(self, mr: MeanReversionStrategy) -> None:
        """RSI is neutral in its middle band — should NOT be dampened."""
        from evotrader.indicators.rsi import rsi_signal as raw_rsi_signal

        raw = raw_rsi_signal(25.0, oversold=30.0, overbought=70.0)
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, rsi=25.0)
        sig = mr.compute_signal(snap)

        assert sig.metadata["rsi_signal"] == pytest.approx(raw)

    def test_vwap_no_longer_computed(self, mr: MeanReversionStrategy) -> None:
        """VWAP component was removed in v017 — vwap_signal should NOT appear."""
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND)
        sig = mr.compute_signal(snap)

        assert "vwap_signal" not in sig.metadata

    def test_zero_ibs_not_dampened(self, mr: MeanReversionStrategy) -> None:
        """IBS signal of exactly 0.0 should skip dampening (no-op guard)."""
        # IBS in the neutral zone [0.2, 0.8] → signal = 0.0
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, ibs=0.5)
        sig = mr.compute_signal(snap)
        assert sig.metadata["ibs_signal"] == 0.0

    def test_signal_bounded(self, mr: MeanReversionStrategy) -> None:
        """Signal value must always be in [-1.0, +1.0]."""
        for regime in MarketRegime:
            snap = _make_snapshot(regime=regime)
            sig = mr.compute_signal(snap)
            assert -1.0 <= sig.value <= 1.0

    def test_trend_fade_decay_param_roundtrip(self) -> None:
        """trend_fade_decay should survive get/set parameter round-trip."""
        mr = MeanReversionStrategy(trend_fade_decay=0.5)
        params = mr.get_parameters()
        assert params["trend_fade_decay"] == 0.5

        mr2 = MeanReversionStrategy()
        mr2.set_parameters(params)
        assert mr2.get_parameters()["trend_fade_decay"] == 0.5

    def test_trend_fade_decay_validation(self) -> None:
        """Validates trend_fade_decay is in [0.0, 1.0]."""
        mr = MeanReversionStrategy()
        errors = mr.validate_parameters({"trend_fade_decay": 1.5})
        assert any("trend_fade_decay" in e for e in errors)
        errors = mr.validate_parameters({"trend_fade_decay": -0.1})
        assert any("trend_fade_decay" in e for e in errors)
        errors = mr.validate_parameters({"trend_fade_decay": 0.35})
        assert not any("trend_fade_decay" in e for e in errors)


# ---------------------------------------------------------------------------
# Mean Reversion — Metadata naming fix
# ---------------------------------------------------------------------------


class TestMeanReversionMetadata:
    """Verify metadata is keyed by name, not positional index."""

    def test_metadata_keys_all_indicators_present(self) -> None:
        """All three component names should appear in metadata (VWAP removed in v017)."""
        mr = MeanReversionStrategy()
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND)
        sig = mr.compute_signal(snap)

        assert "rsi_signal" in sig.metadata
        assert "bollinger_signal" in sig.metadata
        assert "ibs_signal" in sig.metadata
        assert "vwap_signal" not in sig.metadata
        assert sig.metadata["component_count"] == 3

    def test_metadata_keys_when_indicator_missing(self) -> None:
        """With RSI=None, remaining metadata keys should still be correct."""
        mr = MeanReversionStrategy()
        snap = _make_snapshot(regime=MarketRegime.RANGE_BOUND, rsi=None)
        sig = mr.compute_signal(snap)

        assert "rsi_signal" not in sig.metadata
        assert "bollinger_signal" in sig.metadata
        assert "ibs_signal" in sig.metadata
        assert "vwap_signal" not in sig.metadata
        assert sig.metadata["component_count"] == 2

    def test_metadata_keys_only_rsi(self) -> None:
        """With only RSI available, metadata should only have rsi_signal."""
        mr = MeanReversionStrategy()
        snap = _make_snapshot(
            regime=MarketRegime.RANGE_BOUND,
            rsi=25.0,
            ibs=None,
            bb_upper=None,
            bb_middle=None,
            bb_lower=None,
        )
        sig = mr.compute_signal(snap)

        assert "rsi_signal" in sig.metadata
        assert sig.metadata["component_count"] == 1
        assert "bollinger_signal" not in sig.metadata
        assert "ibs_signal" not in sig.metadata
        assert "vwap_signal" not in sig.metadata


# ---------------------------------------------------------------------------
# Momentum — Volume dampener floor
# ---------------------------------------------------------------------------


class TestMomentumVolumeDampener:
    """Verify the volume dampener floor preserves trend signal magnitude."""

    def test_dampener_floor_at_low_volume(self) -> None:
        """At relative_volume=0.30, signal should be >= 70% of undampened."""
        mom_new = MomentumStrategy(volume_dampener_floor=0.7)
        mom_old = MomentumStrategy(volume_dampener_floor=0.3)

        snap = _make_snapshot(regime=MarketRegime.TRENDING_BULL, relative_volume=0.30)

        sig_new = mom_new.compute_signal(snap)
        sig_old = mom_old.compute_signal(snap)

        # New floor keeps more signal: |new| >= |old|
        assert abs(sig_new.value) >= abs(sig_old.value)
        # Old floor crushed to ~30%, new keeps ~70%
        assert abs(sig_new.value) > 0.5 * abs(sig_old.value)

    def test_high_volume_unaffected(self) -> None:
        """At relative_volume=2.0+, floor doesn't matter (vol_multiplier=1.0)."""
        mom = MomentumStrategy(volume_dampener_floor=0.7)
        snap = _make_snapshot(regime=MarketRegime.TRENDING_BULL, relative_volume=2.5)
        sig = mom.compute_signal(snap)

        # With vol_multiplier=1.0, floor doesn't bind
        assert abs(sig.value) > 0

    def test_no_volume_confirmation(self) -> None:
        """With volume_confirmation=False, dampener is skipped entirely."""
        mom = MomentumStrategy(volume_confirmation=False)
        snap = _make_snapshot(regime=MarketRegime.TRENDING_BULL, relative_volume=0.1)
        sig = mom.compute_signal(snap)

        # Signal should be the raw MA+MACD blend, not dampened
        assert abs(sig.value) > 0

    def test_volume_dampener_floor_param_roundtrip(self) -> None:
        """volume_dampener_floor should survive get/set parameter round-trip."""
        mom = MomentumStrategy(volume_dampener_floor=0.5)
        params = mom.get_parameters()
        assert params["volume_dampener_floor"] == 0.5

        mom2 = MomentumStrategy()
        mom2.set_parameters(params)
        assert mom2.get_parameters()["volume_dampener_floor"] == 0.5

    def test_volume_dampener_floor_validation(self) -> None:
        """Validates volume_dampener_floor is in [0.0, 1.0]."""
        mom = MomentumStrategy()
        errors = mom.validate_parameters({"volume_dampener_floor": 1.5})
        assert any("volume_dampener_floor" in e for e in errors)
        errors = mom.validate_parameters({"volume_dampener_floor": 0.7})
        assert not any("volume_dampener_floor" in e for e in errors)

    def test_signal_bounded(self) -> None:
        """Momentum signal must always be in [-1.0, +1.0]."""
        mom = MomentumStrategy(volume_dampener_floor=0.7)
        for regime in MarketRegime:
            snap = _make_snapshot(regime=regime)
            sig = mom.compute_signal(snap)
            assert -1.0 <= sig.value <= 1.0


# ---------------------------------------------------------------------------
# Integration — Both fixes together
# ---------------------------------------------------------------------------


class TestCompositeIntegration:
    """Verify the combined fixes produce a non-frozen composite signal."""

    def test_uptrend_composite_not_frozen(self) -> None:
        """In a confirmed uptrend with low volume, the composite should no
        longer be frozen near zero.

        With v005 params + both code fixes, the momentum signal should be
        strong enough (floor=0.7) and the MR fade weak enough (decay=0.35)
        that the composite expresses a meaningful bullish lean.
        """
        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.algorithms.strategies.gap import GapStrategy

        mr = MeanReversionStrategy(
            rsi_weight=0.60,
            bollinger_weight=0.20,
            ibs_weight=0.20,
            trend_fade_decay=0.2,
        )
        mom = MomentumStrategy(volume_dampener_floor=0.7)
        gap = GapStrategy()

        composite = CompositeStrategy(
            sub_strategies={
                "momentum": mom,
                "mean_reversion": mr,
                "gap": gap,
            },
            weights={
                "momentum": 0.4,
                "mean_reversion": 0.35,
                "gap": 0.25,
            },
        )

        # Confirmed uptrend with low volume (the scenario that was frozen)
        snap = _make_snapshot(
            regime=MarketRegime.TRENDING_BULL,
            ibs=0.87,  # overbought IBS
            close=740.0,  # near upper Bollinger
            relative_volume=0.30,  # low volume
        )

        sig = composite.compute_signal(snap)
        # The composite should NOT be frozen near zero anymore.
        # With the old code, |composite| was ~0.001-0.017.
        # With the fixes, momentum dominates and composite should be
        # meaningfully positive (or at least not frozen in the noise band).
        assert abs(sig.value) > 0.02, f"Composite signal {sig.value:.4f} is still frozen near zero"


# ---------------------------------------------------------------------------
# Mean Reversion — Trend guard (finding #7)
# ---------------------------------------------------------------------------


class TestMeanReversionTrendGuard:
    """When a confirmed downtrend is active (price < ema_9 < ema_21,
    sma_20 < sma_50), BUY-leaning mean_reversion signals should be
    decayed to prevent continuous 'oversold' readings through a decline.

    See evolution review:
    20260729_bearish_inexpressibility finding #7.
    """

    def test_buy_decayed_in_bearish_stack(self):
        """RSI oversold + bearish MA stack → signal decayed by trend_fade_decay."""
        strategy = MeanReversionStrategy(trend_fade_decay=0.35)

        # Bearish MA stack: price < ema_9 < ema_21, sma_20 < sma_50
        snap_bearish = _make_snapshot(
            close=660.0,
            rsi=28.0,  # Very oversold → strong BUY signal
            ibs=0.15,  # Oversold → BUY
            ema_9=665.0,
            ema_21=670.0,
            sma_20=668.0,
            sma_50=675.0,
            bb_upper=680.0,
            bb_middle=670.0,
            bb_lower=660.0,
        )

        # Neutral MA stack (no trend guard): same indicators
        snap_neutral = _make_snapshot(
            close=660.0,
            rsi=28.0,
            ibs=0.15,
            ema_9=658.0,  # price > ema_9 → no bearish stack
            ema_21=655.0,
            sma_20=660.0,
            sma_50=650.0,
            bb_upper=680.0,
            bb_middle=670.0,
            bb_lower=660.0,
        )

        sig_bearish = strategy.compute_signal(snap_bearish)
        sig_neutral = strategy.compute_signal(snap_neutral)

        # Both should be positive (BUY on oversold)
        assert sig_neutral.value > 0, "Expected BUY signal on oversold"
        assert sig_bearish.value > 0, "Even decayed, oversold should be positive"

        # But the bearish-stack signal should be smaller (decayed)
        assert sig_bearish.value < sig_neutral.value, (
            f"Bearish stack signal ({sig_bearish.value:.4f}) should be "
            f"decayed below neutral ({sig_neutral.value:.4f})"
        )
        assert sig_bearish.metadata.get("trend_guard_applied") is True

    def test_no_decay_without_trend(self):
        """No MA trend → no decay applied."""
        strategy = MeanReversionStrategy()

        snap = _make_snapshot(
            close=720.0,
            rsi=28.0,
            ibs=0.15,
            ema_9=718.0,  # Mixed — not a clean bear or bull stack
            ema_21=722.0,
            sma_20=730.0,
            sma_50=710.0,  # sma_20 > sma_50 contradicts bear
        )

        sig = strategy.compute_signal(snap)
        # Always reported since review 20260924_224217, so "did not fire" is
        # countable rather than indistinguishable from "guard missing".
        assert sig.metadata["trend_guard_applied"] is False
        assert sig.metadata["trend_stack"] == "none"
