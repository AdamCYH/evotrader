"""ATR (Average True Range) computation.

ATR measures market volatility. It is the exponential moving average of
the True Range, which accounts for gaps between sessions.

Used extensively in EvoTrader for:
- Position sizing (volatility targeting)
- Stop-loss distance (e.g., 2× ATR from entry)
- Regime detection (high ATR → volatile regime)
"""

from __future__ import annotations

import pandas as pd


def compute_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Compute Average True Range.

    True Range = max(H-L, |H-prevC|, |L-prevC|)
    ATR = EWM of True Range (Wilder's smoothing)

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of closing prices.
        period: Smoothing period (default 14).

    Returns:
        Series of ATR values. First value will be NaN.
    """
    prev_close = close.shift(1)

    # True Range components
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothing (alpha = 1/period)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    return atr


def atr_percentile(
    current_atr: float,
    atr_series: pd.Series,
    lookback: int = 60,
) -> float:
    """Compute where the current ATR falls in the recent distribution.

    Used for regime detection: high percentile = volatile market.

    Args:
        current_atr: Current ATR value.
        atr_series: Historical ATR series.
        lookback: Number of periods for the distribution.

    Returns:
        Percentile (0-100) of the current ATR within the lookback window.
    """
    recent = atr_series.tail(lookback).dropna()
    if len(recent) < 5:
        return 50.0  # Not enough data, assume median

    rank = (recent < current_atr).sum()
    return float(rank / len(recent) * 100)
