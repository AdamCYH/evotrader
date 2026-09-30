"""A swing-failure vote ages from when the reversal was confirmed, not from the low.

Found 2026-09-29 by the evolution agent's code review. Freshness was counted
from the flush bar, so a low that had held longer read as staler: a structure
that chopped under the flush bar's high before reclaiming it was already
discounted by the time it was confirmed. It now counts from the confirmation:
the first close above the flush bar's high, and never from before
min_confirm_bars, the earliest the structure can be confirmed at all. So a
reversal that reclaims at once ages exactly as before. The stale-flush guard
still retires an old low.

Made-up bars and parameters.
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

FLUSH_LOW, FLUSH_HIGH = 100.0, 100.6
DECAY = 0.8
PARAMS = {
    "lookback_bars": 20,
    "min_confirm_bars": 2,
    "max_confirm_bars": 12,
    "min_stretch_atr": 0.3,
    "stretch_scale": 0.5,
    "confirm_decay": DECAY,
    "reclaim_scale": 0.5,
    "base_strength": 0.8,
}


def snapshot(bars_since_flush: int, reclaim_after: int) -> MarketSnapshot:
    """24 five-minute bars: a flush, then higher lows, first closing above the
    flush bar's high ``reclaim_after`` bars after it."""
    now = datetime(2026, 3, 3, 17, 0, tzinfo=UTC)
    n = 24
    flush_idx = n - 1 - bars_since_flush
    bars = []
    for i in range(n):
        if i == flush_idx:
            low, close, high = FLUSH_LOW, FLUSH_LOW + 0.3, FLUSH_HIGH
        elif i < flush_idx:
            low, close = FLUSH_LOW + 1.0, FLUSH_LOW + 1.2
            high = close + 0.1
        else:
            low = FLUSH_LOW + 0.2
            close = FLUSH_HIGH + (0.2 if i - flush_idx >= reclaim_after else -0.2)
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
        ticker="T",
        timestamp=now,
        quote=Quote(
            ticker="T", bid=last - 0.01, ask=last + 0.01, last=last, volume=1e5, timestamp=now
        ),
        indicators=TechnicalIndicators(
            vwap=FLUSH_LOW + 1.0, vwap_anchor="current_session", atr_14=2.0
        ),
        regime=RegimeClassification(regime=MarketRegime.RANGE_BOUND, confidence=0.6, reasoning="t"),
        recent_candles=bars,
    )


def vote(bars_since_flush: int, reclaim_after: int):
    return SwingFailureReversalStrategy(**PARAMS).compute_signal(
        snapshot(bars_since_flush, reclaim_after)
    )


class TestFreshness:
    def test_a_reversal_confirmed_just_now_is_fresh_however_old_its_low(self) -> None:
        signal = vote(bars_since_flush=6, reclaim_after=6)

        assert signal.metadata["reason"] == "confirmed_reversal"
        assert signal.metadata["freshness"] == 1.0, "counting from the low gave 0.8 ** 4"
        assert signal.metadata["bars_since_reclaim"] == 0
        assert signal.metadata["bars_since_confirmed"] == 0

    def test_it_ages_from_the_reclaim(self) -> None:
        signal = vote(bars_since_flush=6, reclaim_after=4)

        assert signal.metadata["freshness"] == pytest.approx(DECAY**2)
        assert signal.metadata["bars_since_reclaim"] == 2

    def test_a_reversal_that_reclaimed_at_once_ages_exactly_as_before(self) -> None:
        """Confirmed no sooner than min_confirm_bars: never below the old value."""
        signal = vote(bars_since_flush=6, reclaim_after=1)

        assert signal.metadata["freshness"] == pytest.approx(DECAY ** (6 - 2))
        assert signal.metadata["bars_since_reclaim"] == 5
        assert signal.metadata["bars_since_confirmed"] == 4

    @pytest.mark.parametrize("reclaim_after", [1, 3, 6])
    def test_is_never_below_counting_from_the_low(self, reclaim_after: int) -> None:
        signal = vote(bars_since_flush=6, reclaim_after=reclaim_after)
        assert signal.metadata["freshness"] >= DECAY ** (6 - 2) - 1e-12

    def test_an_old_low_is_still_retired(self) -> None:
        signal = vote(bars_since_flush=13, reclaim_after=13)

        assert signal.metadata["reason"] == "stale_flush"
        assert signal.value == 0.0
