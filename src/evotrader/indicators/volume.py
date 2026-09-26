"""Volume analysis computations.

Volume confirms price moves — a move on high volume is more significant
than one on low volume. Relative volume (vs. 20-day average) helps filter
out noise from low-liquidity periods.
"""

from __future__ import annotations

import pandas as pd


def compute_volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """Compute volume simple moving average.

    Args:
        volume: Series of trade volumes.
        period: SMA period (default 20).

    Returns:
        Series of volume SMA values.
    """
    return volume.rolling(window=period).mean()


def compute_relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Compute relative volume (current volume / average volume).

    Args:
        volume: Series of trade volumes.
        period: Average lookback period.

    Returns:
        Series of relative volume ratios (1.0 = average, >1.5 = elevated).
    """
    avg = volume.rolling(window=period).mean()
    return volume / avg.replace(0, float("nan"))


def volume_signal(relative_volume: float) -> float:
    """Convert relative volume to a confirmation multiplier in [0, +1].

    This is not a directional signal — it multiplies the strength of
    other signals. High volume confirms, low volume dampens.

    Signal mapping (piecewise-linear, anchored so the documented midpoint is
    the real one):
    - RVOL ≥ 2.0 → 1.0 (strong confirmation)
    - RVOL = 1.0 → 0.5 (average, neutral)
    - RVOL ≤ 0.5 → 0.0 (low volume, dampen signals)

    A single linear map over 0.5→2.0 was used previously. It contradicted the
    mapping above: average volume returned 0.333, not 0.5, and the true neutral
    midpoint sat at RVOL 1.25. Two segments are needed because 1.0 is not the
    midpoint of [0.5, 2.0].

    Note for callers that apply a floor: ``momentum`` clamps this with
    ``max(volume_dampener_floor, raw)``. At the configured floor of 0.7 the
    multiplier is pinned constant for all RVOL below 1.4 (it was 1.55 under the
    old map), so volume confirmation only discriminates above that level.

    Args:
        relative_volume: Current relative volume ratio.

    Returns:
        Multiplier in [0.0, 1.0].
    """
    if relative_volume >= 2.0:
        return 1.0
    elif relative_volume <= 0.5:
        return 0.0
    elif relative_volume <= 1.0:
        # 0.5 → 1.0 maps to 0.0 → 0.5
        return (relative_volume - 0.5) / 1.0
    else:
        # 1.0 → 2.0 maps to 0.5 → 1.0
        return 0.5 + (relative_volume - 1.0) / 2.0


RVOL_SOURCE_DAILY = "prior_session_daily"
RVOL_SOURCE_SESSION = "session_profile"
RVOL_SOURCE_DAILY_FALLBACK = "prior_session_daily_fallback"


def select_relative_volume(indicators: object, mode: str = "daily") -> tuple[float | None, str]:
    """The relative-volume reading a channel should use, and what it measured.

    ONE place decides, so the three channels that read volume cannot drift apart:

    * ``"daily"`` — the prior session's daily ratio (``relative_volume``). This
      is every existing version's behaviour, kept as the default so nothing
      changes until a version opts in.
    * ``"session"`` — today's time-of-day-matched ratio
      (``session_relative_volume``) when the profile is warm; otherwise the
      daily value, labelled ``prior_session_daily_fallback`` so a warming
      profile is visible rather than silently indistinguishable.

    Callers keep their own handling of a missing value, so choosing a source
    never changes what "no data" means to a channel.
    """
    daily = getattr(indicators, "relative_volume", None)
    daily_source = getattr(indicators, "relative_volume_source", None) or RVOL_SOURCE_DAILY
    if mode == "session":
        session = getattr(indicators, "session_relative_volume", None)
        if session is not None:
            return session, RVOL_SOURCE_SESSION
        return daily, RVOL_SOURCE_DAILY_FALLBACK
    return daily, daily_source


def validate_rvol_source(value: object) -> str | None:
    """Error text for a bad ``rvol_source`` parameter, else None."""
    if value not in ("daily", "session"):
        return f"rvol_source must be 'daily' or 'session', got {value!r}"
    return None
