"""Regression tests: intraday indicator staleness and VWAP session anchor.

Guards against the bug where all indicator values (RSI, MACD, VWAP, IBS,
Bollinger) were frozen for the entire trading day because they were computed
from completed daily candles and never refreshed intraday.

See: data/evolution/reviews/20260715_210358_intraday_indicator_staleness_and_vwap_session_anchor.md
"""

from __future__ import annotations

import pandas as pd

from evotrader.agents.tools import _patch_last_bar, compute_indicators
from evotrader.indicators.ibs import compute_live_ibs
from evotrader.indicators.vwap import compute_session_vwap

# ── _patch_last_bar ────────────────────────────────────────────────────


class TestPatchLastBar:
    """Verify that _patch_last_bar correctly updates H/L/C of the last candle."""

    def test_updates_close_to_live_price(self):
        candles = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        _patch_last_bar(candles, 103.0)
        assert candles[-1]["close"] == 103.0

    def test_extends_high_when_live_exceeds(self):
        candles = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        _patch_last_bar(candles, 108.0)
        assert candles[-1]["high"] == 108.0
        assert candles[-1]["close"] == 108.0

    def test_extends_low_when_live_below(self):
        candles = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        _patch_last_bar(candles, 92.0)
        assert candles[-1]["low"] == 92.0
        assert candles[-1]["close"] == 92.0

    def test_preserves_high_low_when_within_range(self):
        candles = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        _patch_last_bar(candles, 100.0)
        assert candles[-1]["high"] == 105  # Unchanged
        assert candles[-1]["low"] == 95  # Unchanged
        assert candles[-1]["close"] == 100.0

    def test_noop_on_empty_candles(self):
        candles: list[dict] = []
        _patch_last_bar(candles, 100.0)  # Should not raise
        assert candles == []

    def test_noop_on_zero_price(self):
        candles = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000},
        ]
        _patch_last_bar(candles, 0.0)
        assert candles[-1]["close"] == 102  # Unchanged

    def test_patched_candles_produce_different_indicators(self):
        """Core regression: indicators must differ after patching the last bar."""
        import json
        import math
        from datetime import datetime, timedelta

        # Build 60 daily candles with realistic up/down price movements
        candles = []
        base_date = datetime(2026, 5, 1)
        for i in range(60):
            # Sinusoidal price creates both gains and losses for valid RSI
            price = 700 + 10 * math.sin(i * 0.3) + i * 0.1
            dt = base_date + timedelta(days=i)
            candles.append(
                {
                    "timestamp": dt.strftime("%Y-%m-%dT00:00:00Z"),
                    "open": price - 0.5,
                    "high": price + 2,
                    "low": price - 2,
                    "close": price,
                    "volume": 10000 + i * 100,
                }
            )

        # Compute indicators WITHOUT patching
        original_indicators = compute_indicators(json.dumps(candles))
        original_macd_hist = original_indicators.get("macd_histogram")

        # Now patch the last bar with a significantly different live price
        import copy

        patched_candles = copy.deepcopy(candles)
        _patch_last_bar(patched_candles, 740.0)  # Big move up from ~706

        patched_indicators = compute_indicators(json.dumps(patched_candles))
        patched_macd_hist = patched_indicators.get("macd_histogram")

        # MACD histogram MUST change after patching (close drives EMA)
        assert patched_macd_hist != original_macd_hist, (
            "MACD histogram should change after patching last bar"
        )


# ── compute_session_vwap ───────────────────────────────────────────────


class TestComputeSessionVwap:
    """Verify session-aware VWAP computation from intraday bars."""

    def test_basic_session_vwap(self):
        """VWAP from simple intraday bars should equal the typical-price-weighted mean."""
        high = pd.Series([102.0, 104.0, 103.0])
        low = pd.Series([98.0, 100.0, 99.0])
        close = pd.Series([100.0, 102.0, 101.0])
        volume = pd.Series([1000.0, 2000.0, 1500.0])

        vwap_val, anchor = compute_session_vwap(high, low, close, volume)

        assert anchor == "current_session"
        assert vwap_val is not None
        # Verify manually: TP = (H+L+C)/3, cum(TP*V)/cum(V)
        tp = (high + low + close) / 3.0
        expected = (tp * volume).sum() / volume.sum()
        assert abs(vwap_val - expected) < 0.001

    def test_empty_series_returns_fallback(self):
        """Empty intraday data should return prior_session fallback."""
        vwap_val, anchor = compute_session_vwap(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
        )
        assert anchor == "prior_session"
        assert vwap_val is None

    def test_none_series_returns_fallback(self):
        vwap_val, anchor = compute_session_vwap(
            pd.Series([], dtype=float),
            pd.Series([], dtype=float),
            None,
            pd.Series([], dtype=float),
        )
        assert anchor == "prior_session"
        assert vwap_val is None

    def test_session_start_filter(self):
        """Only bars at or after session_start should be included."""
        timestamps = pd.to_datetime(
            [
                "2026-07-15 13:00:00+00:00",  # Before 09:30 ET (13:30 UTC)
                "2026-07-15 14:00:00+00:00",  # After session start
                "2026-07-15 15:00:00+00:00",
            ]
        )
        high = pd.Series([100.0, 104.0, 106.0], index=timestamps)
        low = pd.Series([98.0, 100.0, 102.0], index=timestamps)
        close = pd.Series([99.0, 103.0, 105.0], index=timestamps)
        volume = pd.Series([500.0, 1000.0, 1500.0], index=timestamps)

        session_start = pd.Timestamp("2026-07-15 13:30:00+00:00")
        vwap_val, anchor = compute_session_vwap(
            high,
            low,
            close,
            volume,
            session_start=session_start,
        )

        assert anchor == "current_session"
        assert vwap_val is not None
        # Only the last 2 bars should be used
        filtered_high = high.iloc[1:]
        filtered_low = low.iloc[1:]
        filtered_close = close.iloc[1:]
        filtered_volume = volume.iloc[1:]
        tp = (filtered_high + filtered_low + filtered_close) / 3.0
        expected = (tp * filtered_volume).sum() / filtered_volume.sum()
        assert abs(vwap_val - expected) < 0.001


# ── compute_live_ibs ───────────────────────────────────────────────────


class TestComputeLiveIbs:
    """Verify live IBS from session running high/low."""

    def test_price_at_high(self):
        assert compute_live_ibs(110.0, 100.0, 110.0) == 1.0

    def test_price_at_low(self):
        assert compute_live_ibs(110.0, 100.0, 100.0) == 0.0

    def test_price_at_midpoint(self):
        assert compute_live_ibs(110.0, 100.0, 105.0) == 0.5

    def test_zero_range_returns_none(self):
        assert compute_live_ibs(100.0, 100.0, 100.0) is None

    def test_normal_value(self):
        ibs = compute_live_ibs(724.33, 712.14, 720.0)
        assert ibs is not None
        assert 0 < ibs < 1
        expected = (720.0 - 712.14) / (724.33 - 712.14)
        assert abs(ibs - expected) < 0.0001
