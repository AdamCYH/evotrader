"""Three optional swing-failure gates for a capitulation day, all off by default.

Found by the evolution agent's code review. On a day that gaps down and
flushes, the reversal channel stayed silent through the rebound for reasons
of its gates, not of the market:

* the stretch is measured from the session VWAP, which on such a day sits near
  the low by construction, so it read shallow however far the day had fallen.
  ``min_stretch_ema9_atr`` adds a second measure from EMA9; the gate passes on
  either, and ``stretch_basis`` says which;
* ``lookback_bars`` was both the memory and the warm-up: with 5-minute bars a
  window long enough to keep a 2-3 hour reversal in view kept the channel
  silent until noon. ``min_window_bars`` reads from that many bars on;
* a close a hair over the flush bar's high read ``confirmed_reversal`` at half
  loudness. ``min_reclaim_quality`` keeps a token reclaim from saying so
  (``reclaim_too_shallow``).

Unset, every reading is what it was. Made-up bars and parameters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evotrader.algorithms.strategies.swing_failure_reversal import SwingFailureReversalStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

FLUSH_LOW, FLUSH_HIGH, ATR = 100.0, 100.6, 2.0
PARAMS = {
    "lookback_bars": 20,
    "min_confirm_bars": 2,
    "max_confirm_bars": 8,
    "min_stretch_atr": 0.5,
    "stretch_scale": 0.4,
    "confirm_decay": 0.95,
    "reclaim_scale": 0.5,
    "base_strength": 0.8,
}


def snapshot(
    stretch_atr: float,
    *,
    n: int = 24,
    bars_since_flush: int = 4,
    ema9_atr: float | None = None,
    reclaim_by: float = 0.05,
) -> MarketSnapshot:
    """``n`` five-minute bars: a flush, higher lows, then closes over the flush bar's high.

    The session VWAP sits ``stretch_atr`` ATRs above the flush low, EMA9
    ``ema9_atr`` ATRs above it, and the bars after the flush close
    ``reclaim_by`` over the flush bar's high.
    """
    now = datetime(2026, 3, 3, 17, 30, tzinfo=UTC)
    flush_idx = n - 1 - bars_since_flush
    bars = []
    for i in range(n):
        if i == flush_idx:
            low, close, high = FLUSH_LOW, FLUSH_LOW + 0.3, FLUSH_HIGH
        elif i < flush_idx:
            low, close = FLUSH_LOW + 1.0, FLUSH_LOW + 1.2
            high = close + 0.1
        else:
            low, close = FLUSH_LOW + 0.2, FLUSH_HIGH + reclaim_by
            high = close + 0.1
        bars.append(
            OHLCV(
                timestamp=now - timedelta(minutes=5 * (n - i)),
                open=close,
                high=high,
                low=low,
                close=close,
                volume=1e4,
            )
        )
    last = bars[-1].close
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=now,
        quote=Quote(
            ticker="XYZ", bid=last - 0.01, ask=last + 0.01, last=last, volume=1e5, timestamp=now
        ),
        indicators=TechnicalIndicators(
            vwap=FLUSH_LOW + stretch_atr * ATR,
            vwap_anchor="current_session",
            atr_14=ATR,
            ema_9=FLUSH_LOW + ema9_atr * ATR if ema9_atr is not None else None,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="t"
        ),
        recent_candles=bars,
    )


def read(snap: MarketSnapshot, **overrides):
    return SwingFailureReversalStrategy(**{**PARAMS, **overrides}).compute_signal(snap)


class TestOffByDefault:
    def test_the_defaults(self) -> None:
        params = SwingFailureReversalStrategy().get_parameters()
        assert params["min_stretch_ema9_atr"] is None
        assert params["min_window_bars"] is None
        assert params["min_reclaim_quality"] == 0.0

    @pytest.mark.parametrize("stretch", [0.2, 0.9])
    def test_unset_reads_as_before(self, stretch) -> None:
        snap = snapshot(stretch, ema9_atr=3.0)
        before = read(snap)
        explicit = read(
            snap, min_stretch_ema9_atr=None, min_window_bars=None, min_reclaim_quality=0.0
        )
        assert (explicit.value, explicit.metadata) == (before.value, before.metadata)
        assert "stretch_basis" not in before.metadata


class TestTheEma9Stretch:
    def test_a_day_whose_vwap_hugs_the_low_passes_on_ema9(self) -> None:
        snap = snapshot(0.2, ema9_atr=2.0)
        assert read(snap).metadata["reason"] == "stretch_too_shallow"
        sig = read(snap, min_stretch_ema9_atr=1.5)
        assert sig.metadata["reason"] == "confirmed_reversal"
        assert sig.metadata["stretch_basis"] == "ema9"
        assert sig.metadata["stretch_ema9_atr"] == pytest.approx(2.0)
        assert sig.metadata["stretch_atr"] == pytest.approx(0.2)

    def test_its_loudness_is_still_measured_from_the_vwap_stretch(self) -> None:
        snap = snapshot(0.2, ema9_atr=2.0)
        # The anchor defaults to the gate (0.5): a 0.2 stretch says nothing.
        assert read(snap, min_stretch_ema9_atr=1.5).value == 0.0
        assert read(snap, min_stretch_ema9_atr=1.5, stretch_anchor_atr=0.0).value > 0.0

    def test_neither_measure_deep_enough(self) -> None:
        sig = read(snapshot(0.2, ema9_atr=1.0), min_stretch_ema9_atr=1.5)
        assert sig.metadata["reason"] == "stretch_too_shallow"
        assert sig.metadata["stretch_basis"] is None
        assert sig.metadata["stretch_ema9_atr"] == pytest.approx(1.0)
        assert sig.metadata["min_stretch_ema9_atr"] == 1.5

    def test_the_vwap_measure_comes_first(self) -> None:
        sig = read(snapshot(0.9, ema9_atr=2.0), min_stretch_ema9_atr=1.5)
        assert sig.metadata["stretch_basis"] == "vwap"

    def test_no_ema9_means_the_vwap_measure_alone(self) -> None:
        sig = read(snapshot(0.2), min_stretch_ema9_atr=1.5)
        assert sig.metadata["reason"] == "stretch_too_shallow"
        assert sig.metadata["stretch_ema9_atr"] is None


class TestTheFirstRead:
    def test_a_long_window_reads_from_min_window_bars(self) -> None:
        snap = snapshot(0.9, n=14)
        waiting = read(snap, lookback_bars=36)
        assert waiting.metadata["reason"] == "insufficient_data"
        assert waiting.metadata["bars_needed"] == 36
        sig = read(snap, lookback_bars=36, min_window_bars=12)
        assert sig.metadata["reason"] == "confirmed_reversal"
        assert sig.value == pytest.approx(read(snap, lookback_bars=14).value)

    def test_below_it_the_channel_is_warming_up(self) -> None:
        sig = read(snapshot(0.9, n=10, bars_since_flush=3), lookback_bars=36, min_window_bars=12)
        assert sig.metadata["reason"] == "insufficient_data"
        assert sig.metadata["warming_up"] is True
        assert sig.metadata["bars_needed"] == 12

    def test_the_window_is_still_the_lookback(self) -> None:
        # 40 bars, the flush 4 bars back: a 20-bar window and a 36-bar one find it.
        snap = snapshot(0.9, n=40)
        assert read(snap, min_window_bars=12).value == pytest.approx(read(snap).value)


class TestTheReclaimFloor:
    def test_a_token_reclaim_is_not_a_reversal(self) -> None:
        snap = snapshot(0.9, reclaim_by=0.05)  # 0.05 / ATR 2 / scale 0.5 = 0.05
        assert read(snap).metadata["reason"] == "confirmed_reversal"
        sig = read(snap, min_reclaim_quality=0.10)
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "reclaim_too_shallow"
        assert sig.metadata["reclaim_quality"] == pytest.approx(0.05)
        assert sig.metadata["min_reclaim_quality"] == 0.10

    def test_a_clean_reclaim_passes_unchanged(self) -> None:
        snap = snapshot(0.9, reclaim_by=0.6)
        assert read(snap, min_reclaim_quality=0.10).value == pytest.approx(read(snap).value)


class TestValidation:
    @pytest.mark.parametrize(
        "params, error",
        [
            ({"min_stretch_ema9_atr": 0.0}, "min_stretch_ema9_atr must be positive"),
            ({"min_window_bars": 2}, "min_window_bars must be in [3"),
            ({"min_window_bars": 30, "lookback_bars": 20}, "min_window_bars must be in [3"),
            ({"min_reclaim_quality": 1.5}, "min_reclaim_quality must be in [0, 1]"),
        ],
    )
    def test_bad_values(self, params, error) -> None:
        errors = SwingFailureReversalStrategy().validate_parameters(params)
        assert any(error in e for e in errors), errors

    def test_good_values(self) -> None:
        assert (
            SwingFailureReversalStrategy().validate_parameters(
                {
                    "lookback_bars": 36,
                    "min_window_bars": 12,
                    "min_stretch_ema9_atr": 1.5,
                    "min_reclaim_quality": 0.1,
                }
            )
            == []
        )

    def test_set_parameters_takes_them(self) -> None:
        s = SwingFailureReversalStrategy()
        s.set_parameters({"min_stretch_ema9_atr": 1.5, "min_window_bars": 12})
        assert s.get_parameters()["min_stretch_ema9_atr"] == 1.5
        assert s.get_parameters()["min_window_bars"] == 12
