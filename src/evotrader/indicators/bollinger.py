"""Bollinger Bands computation.

Bollinger Bands measure volatility by placing bands 2 standard deviations
above and below a simple moving average. Price touching/crossing the bands
generates mean-reversion signals.
"""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd


class BollingerResult(NamedTuple):
    """Structured Bollinger Bands computation result."""

    upper: pd.Series
    middle: pd.Series
    lower: pd.Series
    width: pd.Series
    percent_b: pd.Series  # %B: position within the bands (0-1)


def compute_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerResult:
    """Compute Bollinger Bands.

    Args:
        close: Series of closing prices.
        period: SMA period (default 20).
        num_std: Number of standard deviations (default 2.0).

    Returns:
        ``BollingerResult`` with upper, middle, lower bands, width, and %B.
    """
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()

    upper = middle + (std * num_std)
    lower = middle - (std * num_std)

    # Band width: normalised by middle band
    width = (upper - lower) / middle

    # %B: position within bands (0 = at lower band, 1 = at upper band)
    band_range = upper - lower
    percent_b = (close - lower) / band_range.replace(0, float("nan"))

    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        width=width,
        percent_b=percent_b,
    )


def bollinger_signal(
    close: float,
    upper: float,
    middle: float,
    lower: float,
) -> float:
    """Convert Bollinger Band position to a normalised signal in [-1, +1].

    Signal mapping (mean-reversion):
    - Price at/below lower band → +1.0 (buy — expect reversion to mean)
    - Price at/above upper band → -1.0 (sell — expect reversion to mean)
    - Price at middle band     →  0.0 (neutral)

    Args:
        close: Current closing price.
        upper: Upper Bollinger Band value.
        middle: Middle band (SMA) value.
        lower: Lower Bollinger Band value.

    Returns:
        Signal in [-1.0, +1.0].
    """
    if upper == lower:
        return 0.0

    # Normalise price position: -1 at upper, +1 at lower, 0 at middle
    band_half = (upper - lower) / 2.0
    distance_from_middle = close - middle

    # Invert: below middle = positive signal (buy), above = negative (sell)
    raw_signal = -distance_from_middle / band_half

    return max(-1.0, min(1.0, raw_signal))
