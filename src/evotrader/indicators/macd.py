"""MACD (Moving Average Convergence Divergence) computation.

The MACD tracks the relationship between two exponential moving averages.
It generates signals from:
- Signal line crossovers (MACD line crossing signal line)
- Histogram direction changes
- Divergence from price
"""

from __future__ import annotations

import math
from typing import NamedTuple

import pandas as pd


class MACDResult(NamedTuple):
    """Structured MACD computation result."""

    macd_line: pd.Series
    signal_line: pd.Series
    histogram: pd.Series


def compute_macd(
    close: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """Compute MACD line, signal line, and histogram.

    Args:
        close: Series of closing prices.
        fast_period: Fast EMA period (default 12).
        slow_period: Slow EMA period (default 26).
        signal_period: Signal line EMA period (default 9).

    Returns:
        ``MACDResult`` with macd_line, signal_line, and histogram.
    """
    fast_ema = close.ewm(span=fast_period, adjust=False).mean()
    slow_ema = close.ewm(span=slow_period, adjust=False).mean()

    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    return MACDResult(
        macd_line=macd_line,
        signal_line=signal_line,
        histogram=histogram,
    )


def macd_signal(
    macd_line_value: float,
    signal_line_value: float,
    histogram_value: float,
    histogram_prev: float | None = None,
    atr: float | None = None,
) -> float:
    """Convert MACD values to a normalised signal in [-1, +1].

    Signal sources:
    1. MACD line position relative to signal line (crossover)
    2. Histogram magnitude and direction

    When *atr* is provided the crossover is scaled by 0.25 × ATR rather
    than the legacy hard-coded 2.0 (calibrated for SPY).  On QQQ (~2×
    SPY's price) crossovers routinely exceed 2.0 and clamp; ATR-relative
    scaling adapts to the instrument automatically.

    Args:
        macd_line_value: Current MACD line value.
        signal_line_value: Current signal line value.
        histogram_value: Current histogram value.
        histogram_prev: Previous histogram value (for acceleration detection).
        atr: 14-period ATR for volatility-relative scaling. None falls
            back to the legacy SPY-calibrated divisor of 2.0.

    Returns:
        Signal in [-1.0, +1.0].
    """
    # Base signal from crossover direction.  The legacy divisor of 2.0
    # was calibrated for SPY; on QQQ crossovers routinely exceed it and
    # clamp.  Scale by ATR when available.
    crossover = macd_line_value - signal_line_value
    scale = (0.25 * atr) if (atr is not None and atr > 0) else 2.0
    crossover_signal = math.tanh(crossover / scale)

    # Histogram acceleration signal
    accel_signal = 0.0
    if histogram_prev is not None:
        accel = histogram_value - histogram_prev
        accel_signal = max(-0.5, min(0.5, accel / 1.0))

    # Combine: 70% crossover, 30% acceleration
    combined = 0.7 * crossover_signal + 0.3 * accel_signal

    return max(-1.0, min(1.0, combined))
