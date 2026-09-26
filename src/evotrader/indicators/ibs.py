"""IBS (Internal Bar Strength) computation.

IBS measures where the closing price falls within the day's range:
    IBS = (Close - Low) / (High - Low)

It is one of the most reliable mean-reversion indicators for SPY:
- IBS < 0.2 → oversold → next day tends to be positive
- IBS > 0.8 → overbought → next day tends to be negative

Academic studies show IBS has been a consistent predictor of next-day
returns for S&P 500 ETFs since the 1990s.
"""

from __future__ import annotations

import pandas as pd


def compute_ibs(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> pd.Series:
    """Compute Internal Bar Strength.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of closing prices.

    Returns:
        Series of IBS values in [0, 1]. NaN where High == Low.
    """
    bar_range = high - low
    ibs = (close - low) / bar_range.replace(0, float("nan"))
    return ibs


def ibs_signal(
    ibs_value: float,
    oversold: float = 0.2,
    overbought: float = 0.8,
) -> float:
    """Convert IBS to a normalised signal in [-1, +1].

    Signal mapping (mean-reversion):
    - IBS ≤ oversold threshold  → +1.0 (strong buy)
    - IBS ≥ overbought threshold → -1.0 (strong sell)
    - IBS = 0.5                  →  0.0 (neutral)

    Args:
        ibs_value: Current IBS reading (0-1).
        oversold: Buy threshold (default 0.2).
        overbought: Sell threshold (default 0.8).

    Returns:
        Signal in [-1.0, +1.0].
    """
    if ibs_value <= oversold:
        # Scale from oversold→0 to [+0.5, +1.0]
        intensity = (oversold - ibs_value) / oversold
        return 0.5 + 0.5 * intensity
    elif ibs_value >= overbought:
        # Scale from overbought→1.0 to [-0.5, -1.0]
        intensity = (ibs_value - overbought) / (1.0 - overbought)
        return -(0.5 + 0.5 * intensity)
    else:
        # Between oversold and overbought: linear scale, damped
        midpoint = (oversold + overbought) / 2.0
        half_range = (overbought - oversold) / 2.0
        return -(ibs_value - midpoint) / half_range * 0.3


def compute_live_ibs(
    session_high: float,
    session_low: float,
    last_price: float,
) -> float | None:
    """Compute IBS from the current session's developing bar.

    During regular hours, the session high/low/last should come from the
    intraday running extremes (not yesterday's completed bar) so that IBS
    reflects the *current* session's price position.

    Args:
        session_high: Current session's running high price.
        session_low: Current session's running low price.
        last_price: Most recent trade price (live quote).

    Returns:
        IBS value in [0, 1], or ``None`` if high == low (no range).
    """
    bar_range = session_high - session_low
    if bar_range <= 0:
        return None
    return (last_price - session_low) / bar_range
