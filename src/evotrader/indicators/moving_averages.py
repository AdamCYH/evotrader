"""Moving average computations (EMA and SMA).

Moving averages are the foundation of trend-following strategies:
- Short-term MAs (9/21 EMA) capture immediate momentum
- Intermediate MAs (20/50 SMA) define the prevailing trend
- Crossovers generate trend-change signals
"""

from __future__ import annotations

import math
from typing import NamedTuple

import pandas as pd


class MovingAverages(NamedTuple):
    """Computed moving average values."""

    ema_9: pd.Series
    ema_21: pd.Series
    sma_20: pd.Series
    sma_50: pd.Series


def compute_moving_averages(close: pd.Series) -> MovingAverages:
    """Compute standard moving averages for trend analysis.

    Args:
        close: Series of closing prices.

    Returns:
        ``MovingAverages`` with EMA(9), EMA(21), SMA(20), and SMA(50).
    """
    return MovingAverages(
        ema_9=close.ewm(span=9, adjust=False).mean(),
        ema_21=close.ewm(span=21, adjust=False).mean(),
        sma_20=close.rolling(window=20).mean(),
        sma_50=close.rolling(window=50).mean(),
    )


def moving_average_signal(
    close: float,
    ema_9: float,
    ema_21: float,
    sma_20: float,
    sma_50: float,
    atr: float | None = None,
) -> float:
    """Convert moving average relationships to a normalised signal in [-1, +1].

    Signal components:
    1. Short-term EMA crossover (EMA9 vs EMA21) — immediate momentum
    2. Price position vs SMA20 — current trend bias
    3. Intermediate trend (SMA20 vs SMA50) — prevailing trend

    When *atr* is provided, dispersion is scaled relative to volatility
    (ATR-relative) and compressed via ``tanh`` so the signal remains
    monotonic and informative far past the old hard-clamp rail.  Without
    ATR the legacy fixed-percent-of-price normaliser is used as fallback.

    Args:
        close: Current closing price.
        ema_9: 9-period EMA value.
        ema_21: 21-period EMA value.
        sma_20: 20-period SMA value.
        sma_50: 50-period SMA value.
        atr: 14-period ATR (volatility-relative scaling). None falls back
            to the legacy percent-of-price normaliser.

    Returns:
        Signal in (-1.0, +1.0).
    """
    if sma_50 == 0:
        return 0.0

    # Volatility-relative scaling.  A fixed percent-of-price normaliser
    # saturates on any instrument whose ATR exceeds it (QQQ ATR ~2.25%
    # of price vs a 0.5% normaliser), which pins all three components
    # at the clamp rail and destroys the signal's information content.
    # Scale by ATR so 'significant dispersion' adapts to the regime.
    if atr is not None and atr > 0:
        ema_scale = 0.75 * atr  # ~0.75 ATR = significant EMA spread
        price_scale = 0.75 * atr
        trend_scale = 1.50 * atr  # slower channel, wider scale
    else:  # legacy fallback
        ema_scale = close * 0.005
        price_scale = close * 0.005
        trend_scale = close * 0.010

    # tanh compression keeps the output in (-1, +1) while remaining
    # strictly monotonic — a -3 ATR dislocation now reads meaningfully
    # stronger than a -1 ATR one instead of both returning exactly -1.0.
    ema_signal = math.tanh((ema_9 - ema_21) / ema_scale)
    price_signal = math.tanh((close - sma_20) / price_scale)
    trend_signal = math.tanh((sma_20 - sma_50) / trend_scale)

    return 0.4 * ema_signal + 0.3 * price_signal + 0.3 * trend_signal
