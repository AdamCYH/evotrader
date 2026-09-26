"""RSI (Relative Strength Index) computation.

The RSI is a momentum oscillator that measures the speed and magnitude of
price changes. Values range from 0 to 100:
- Below 30: oversold → potential buy signal
- Above 70: overbought → potential sell signal

Thresholds are configurable via the Algorithm Registry.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Compute the Relative Strength Index.

    Uses the exponential weighted moving average (Wilder's smoothing)
    method, which is the industry standard for RSI calculation.

    Args:
        close: Series of closing prices.
        period: Lookback period (default 14).

    Returns:
        Series of RSI values (0-100). First ``period`` values will be NaN.
    """
    delta = close.diff()

    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder's smoothing (equivalent to EMA with alpha = 1/period)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.inf)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def rsi_signal(
    rsi_value: float,
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> float:
    """Convert an RSI value to a normalised signal in [-1, +1].

    Signal mapping:
    - RSI ≤ oversold  →  +1.0 (strong buy)
    - RSI ≥ overbought → -1.0 (strong sell)
    - RSI = 50         →  0.0 (neutral)
    - Linear interpolation between zones

    Args:
        rsi_value: Current RSI reading (0-100).
        oversold: Buy threshold.
        overbought: Sell threshold.

    Returns:
        Signal in [-1.0, +1.0].
    """
    if rsi_value <= oversold:
        # Scale from oversold..0 → 0..+1
        return min(1.0, (oversold - rsi_value) / oversold + 0.5)
    elif rsi_value >= overbought:
        # Scale from overbought..100 → 0..-1
        return max(-1.0, -(rsi_value - overbought) / (100 - overbought) - 0.5)
    else:
        # Between oversold and overbought: linear scale
        midpoint = (oversold + overbought) / 2
        half_range = (overbought - oversold) / 2
        return -(rsi_value - midpoint) / half_range * 0.4  # Damped in neutral zone
