"""Regression tests: Range-break continuation silent non-fire.

Guards against the bug where range_break_continuation emitted 0.0 on a
confirmed bearish break day because ``snapshot.daily_change_pct`` was None
and the ``or 0.0`` fallback silently disabled magnitude confirmation.

See: data/evolution/reviews/20260723_210605_range_break_silence_and_regime_downday_blindspot.md
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.strategies.range_break_continuation import (
    RangeBreakContinuationStrategy,
)
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)


def _make_snapshot(
    close: float = 688.535,
    bollinger_lower: float = 690.104,
    bollinger_upper: float = 710.0,
    bollinger_middle: float = 700.0,
    macd_histogram: float = -3.033,
    atr_14: float = 5.0,
    daily_change_pct: float | None = None,
    quote_previous_close: float | None = None,
    ibs: float = 0.15,
) -> MarketSnapshot:
    """Build a MarketSnapshot matching the 7/23 15:30 incident conditions.

    All _pct values use PERCENT units: -2.38 means -2.38%.
    """
    quote_kwargs: dict = {
        "ticker": "QQQ",
        "bid": close - 0.01,
        "ask": close + 0.01,
        "last": close,
        "volume": 50_000_000.0,
        "timestamp": datetime(2026, 7, 23, 15, 30, 0),
    }
    quote = Quote(**quote_kwargs)

    # Dynamically set previous_close if provided (Quote model may not have it)
    if quote_previous_close is not None:
        try:
            quote.previous_close = quote_previous_close  # type: ignore[attr-defined]
        except Exception:
            object.__setattr__(quote, "previous_close", quote_previous_close)

    indicators = TechnicalIndicators(
        rsi_14=25.0,
        macd_line=-2.0,
        macd_signal=-1.0,
        macd_histogram=macd_histogram,
        bollinger_upper=bollinger_upper,
        bollinger_middle=bollinger_middle,
        bollinger_lower=bollinger_lower,
        bollinger_width=0.03,
        ema_9=692.0,
        ema_21=695.0,
        sma_20=700.0,
        sma_50=705.0,
        vwap=695.0,
        # The break is confirmed INTRADAY since 2026-09-18 (review 20260918_195356):
        # price on the break side of a current-session VWAP, IBS at the extreme.
        vwap_anchor="current_session",
        ibs=ibs,
        atr_14=atr_14,
        volume_sma_20=40_000_000.0,
        relative_volume=1.5,
    )

    regime = RegimeClassification(
        regime=MarketRegime.RANGE_BOUND,
        confidence=0.6,
        reasoning="ADX 20.5 below threshold 25.",
        adx=20.5,
        trend_direction=-0.5,
        volatility_percentile=50.0,
    )

    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 7, 23, 15, 30, 0),
        quote=quote,
        indicators=indicators,
        regime=regime,
        daily_change_pct=daily_change_pct,
    )


class TestRangeBreakSilenceFix:
    """Regression tests for the 7/23 silent non-fire incident."""

    def test_bearish_break_fires_with_valid_daily_change(self):
        """Replay 7/23 15:30: close=688.535 < boll_lower=690.104,
        macd_hist=-3.033, daily_change_pct=-2.38 → should fire bearish."""
        strategy = RangeBreakContinuationStrategy(
            min_break_pct=1.2,
            macd_confirm_floor=0.5,  # ATR units since 2026-09-18 (was $1.00)
            base_strength=0.45,
            penetration_scale=0.35,
        )
        snapshot = _make_snapshot(daily_change_pct=-2.38)  # -2.38%
        signal = strategy.compute_signal(snapshot)

        assert signal.value < 0, (
            f"Expected negative signal for bearish break, got {signal.value}. "
            f"Metadata: {signal.metadata}"
        )
        assert signal.value == pytest.approx(-0.56, abs=0.1), (
            f"Expected signal ~-0.56, got {signal.value}"
        )
        assert signal.metadata["break_side"] == "lower"
        assert signal.metadata["active_break"] == 1
        assert signal.metadata["day_chg_source"] == "snapshot"

    def test_silent_none_daily_change_with_fallback(self):
        """When daily_change_pct is None but quote has previous_close,
        the strategy should compute day_chg from the fallback and fire."""
        strategy = RangeBreakContinuationStrategy(
            min_break_pct=1.2,
            macd_confirm_floor=0.5,  # ATR units since 2026-09-18 (was $1.00)
            base_strength=0.45,
            penetration_scale=0.35,
        )
        # daily_change_pct=None, but previous_close=705.35 →
        # fallback: ((688.535 - 705.35) / 705.35) * 100 ≈ -2.38%
        snapshot = _make_snapshot(
            daily_change_pct=None,
            quote_previous_close=705.35,
        )
        signal = strategy.compute_signal(snapshot)

        assert signal.value < 0, (
            f"Expected bearish signal via fallback, got {signal.value}. Metadata: {signal.metadata}"
        )
        assert signal.metadata["day_chg_source"] == "fallback_prev_close"
        assert signal.metadata["active_break"] == 1

    def test_missing_daily_change_no_fallback_emits_zero(self):
        """When daily_change_pct is None and no previous_close is available,
        strategy should emit applicable=False so the composite renormalizes
        around the missing voice rather than diluting by the strategy's
        full weight (dead-weight dilution fix, see review
        20260729_bearish_inexpressibility)."""
        strategy = RangeBreakContinuationStrategy(
            min_break_pct=1.2,
            macd_confirm_floor=0.5,  # ATR units since 2026-09-18 (was $1.00)
        )
        snapshot = _make_snapshot(daily_change_pct=None, quote_previous_close=None)
        signal = strategy.compute_signal(snapshot)

        assert signal.value == 0.0, f"Expected 0.0 when day_chg is missing, got {signal.value}"
        assert signal.metadata["day_chg_source"] == "missing"
        assert signal.metadata["applicable"] is False
        assert signal.metadata["reason"] == "day_change_unavailable"

    def test_metadata_always_emitted_on_nonfire(self):
        """Even when the strategy doesn't fire, diagnostic metadata
        must be present for auditability."""
        strategy = RangeBreakContinuationStrategy()
        # Price within bands → no break
        snapshot = _make_snapshot(
            close=700.0,
            bollinger_lower=690.0,
            bollinger_upper=710.0,
            daily_change_pct=-0.5,  # -0.5%
        )
        signal = strategy.compute_signal(snapshot)

        assert signal.value == 0.0
        # All diagnostic keys must be present
        assert "day_chg" in signal.metadata
        assert "macd_histogram" in signal.metadata
        assert "close_vs_lower" in signal.metadata
        assert "close_vs_upper" in signal.metadata
        assert "active_break" in signal.metadata
        assert signal.metadata["active_break"] == 0

    def test_bullish_break_fires(self):
        """Symmetric test: bullish break above upper band."""
        strategy = RangeBreakContinuationStrategy(
            min_break_pct=1.2,
            macd_confirm_floor=0.5,  # ATR units since 2026-09-18 (was $1.00)
            base_strength=0.45,
            penetration_scale=0.35,
        )
        snapshot = _make_snapshot(
            close=712.0,
            ibs=0.9,
            bollinger_upper=710.0,
            bollinger_lower=690.0,
            macd_histogram=3.5,
            daily_change_pct=2.5,  # +2.5%
        )
        signal = strategy.compute_signal(snapshot)

        assert signal.value > 0, f"Expected positive signal for bullish break, got {signal.value}"
        assert signal.metadata["break_side"] == "upper"
        assert signal.metadata["active_break"] == 1

    def test_magnitude_below_threshold_does_not_fire(self):
        """Small daily move below min_break_pct should not trigger break
        even with band penetration + MACD confirmation."""
        strategy = RangeBreakContinuationStrategy(
            min_break_pct=1.2,
            macd_confirm_floor=0.5,  # ATR units since 2026-09-18 (was $1.00)
        )
        snapshot = _make_snapshot(
            daily_change_pct=-0.5,  # -0.5% — below 1.2% threshold
        )
        signal = strategy.compute_signal(snapshot)

        assert signal.value == 0.0, f"Expected no fire for small daily move, got {signal.value}"
        assert signal.metadata["active_break"] == 0


class TestRangeBreakValidation:
    """Verify validators catch unit confusion."""

    def test_validator_rejects_out_of_range_value(self):
        """If someone passes 120 (thinking 120%), validator should reject."""
        strategy = RangeBreakContinuationStrategy()
        errors = strategy.validate_parameters({"min_break_pct": 120})
        assert len(errors) > 0
        assert "percent" in errors[0].lower()

    def test_validator_accepts_percent_value(self):
        """1.2 (= 1.2%) should be accepted."""
        strategy = RangeBreakContinuationStrategy()
        errors = strategy.validate_parameters({"min_break_pct": 1.2})
        assert len(errors) == 0
