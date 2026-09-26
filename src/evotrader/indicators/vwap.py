"""VWAP (Volume-Weighted Average Price) computation.

VWAP is the benchmark for intraday trading — it represents the average
price weighted by volume. Price tends to revert to VWAP during range-bound
sessions. VWAP is calculated from the start of the trading day.
"""

from __future__ import annotations

import math

import pandas as pd


def compute_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    session: pd.Series | None = None,
) -> pd.Series:
    """Compute cumulative intraday VWAP with per-session reset.

    Uses the typical price (H+L+C)/3 weighted by volume.  Cumulative sums
    are reset at each session boundary so that VWAP reflects the *current*
    trading session only, avoiding the stale multi-day mean bug.

    If *session* is ``None`` and the index is a ``DatetimeIndex``, sessions
    are inferred automatically (one per calendar day).  For non-datetime
    indices the original whole-window cumsum is preserved as a fallback.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of closing prices.
        volume: Series of trade volumes.
        session: Optional Series whose values group bars into sessions.
            Bars with the same session value share a cumulative window.

    Returns:
        Series of VWAP values.
    """
    typical_price = (high + low + close) / 3.0
    tp_volume = typical_price * volume

    if session is None and isinstance(close.index, pd.DatetimeIndex):
        # Default: reset at each calendar trading day from the index.
        session = pd.Series(close.index, index=close.index).dt.normalize()

    if session is not None:
        cumulative_tp_volume = tp_volume.groupby(session).cumsum()
        cumulative_volume = volume.groupby(session).cumsum()
    else:
        cumulative_tp_volume = tp_volume.cumsum()
        cumulative_volume = volume.cumsum()

    vwap = cumulative_tp_volume / cumulative_volume.replace(0, float("nan"))
    return vwap


def vwap_signal(
    close: float,
    vwap_value: float,
    atr: float | None = None,
) -> float:
    """Convert price-to-VWAP relationship to a normalised signal in [-1, +1].

    Signal mapping (mean-reversion):
    - Price far below VWAP → +1.0 (buy — expect reversion up to VWAP)
    - Price far above VWAP → -1.0 (sell — expect reversion down to VWAP)
    - Price at VWAP        →  0.0 (neutral)

    Three-layer normalisation (merged from evolution reviews):
    1. Wider normaliser (~2.5× ATR) so the signal retains a usable gradient
       across the realistic intraday range.
    2. Saturation guard: when |distance| > 3 ATR, attenuate toward 0 to
       prevent a single extreme-distance reading from pinning the signal.
       NOTE: Mis-anchoring (stale prior-session VWAP) is now handled by
       callers via the ``vwap_anchor`` label — callers abstain outright
       unless ``anchor == 'current_session'``.  The 3-ATR decay remains
       here only as a saturation guard for genuine *intra-session* extremes
       (e.g. a large gap-up on a fresh session VWAP).
    3. Smooth ``tanh`` saturation instead of a hard clamp so extreme-but-
       finite distances still vary (eliminates the dead -1.0 signal bug).

    Args:
        close: Current closing price.
        vwap_value: Current VWAP value.
        atr: Optional ATR for normalisation (more robust than % of price).

    Returns:
        Signal in [-1.0, +1.0].
    """
    if vwap_value == 0:
        return 0.0

    distance = close - vwap_value

    # Normalise by ~2.5× ATR (retain a usable gradient across the realistic
    # intraday range) if available, otherwise by 1.25% of VWAP.
    normaliser = (atr * 2.5) if atr and atr > 0 else vwap_value * 0.0125

    # Stale-anchor guard: if price is an extreme distance from VWAP
    # (>3 ATR, or >5% when ATR is unavailable), the daily VWAP is likely
    # mis-anchored after a multi-day move and carries no mean-reversion
    # edge.  Attenuate toward 0 rather than reporting a confident saturated
    # reading that would freeze the composite ensemble.
    extreme_threshold = (atr * 3.0) if atr and atr > 0 else vwap_value * 0.05
    if abs(distance) > extreme_threshold and extreme_threshold > 0:
        decay = extreme_threshold / abs(distance)  # in (0, 1)
        raw_signal = (-distance / normaliser) * decay
    else:
        raw_signal = -distance / normaliser

    # Smooth saturation so large-but-finite distances still vary instead
    # of pinning at the clamp boundary (avoids the dead -1.0 signal bug).
    return math.tanh(raw_signal)


def compute_session_vwap(
    intraday_high: pd.Series,
    intraday_low: pd.Series,
    intraday_close: pd.Series,
    intraday_volume: pd.Series,
    session_start: pd.Timestamp | None = None,
) -> tuple[float | None, str]:
    """Compute cumulative VWAP for the CURRENT session only from intraday bars.

    Used during regular hours so the VWAP anchor reflects the *developing*
    session rather than the prior day's completed session.  Callers should
    fall back to the daily ``compute_vwap`` result (prior session) only
    when no intraday bars exist (e.g. pre-market open).

    Args:
        intraday_high: Series of intraday high prices.
        intraday_low: Series of intraday low prices.
        intraday_close: Series of intraday close prices.
        intraday_volume: Series of intraday volumes.
        session_start: Optional timestamp to filter bars from.  If ``None``,
            all bars are used.

    Returns:
        Tuple of ``(vwap_value, anchor_label)`` where *anchor_label* is
        ``"current_session"`` on success or ``"prior_session"`` on fallback.
    """
    if intraday_close is None or intraday_close.empty:
        return None, "prior_session"

    if session_start is not None and isinstance(intraday_close.index, pd.DatetimeIndex):
        mask = intraday_close.index >= session_start
        intraday_high = intraday_high[mask]
        intraday_low = intraday_low[mask]
        intraday_close = intraday_close[mask]
        intraday_volume = intraday_volume[mask]

    if intraday_close.empty:
        return None, "prior_session"

    typical_price = (intraday_high + intraday_low + intraday_close) / 3.0
    cum_tpv = (typical_price * intraday_volume).cumsum()
    cum_vol = intraday_volume.cumsum().replace(0, float("nan"))
    vwap_series = cum_tpv / cum_vol

    vwap_value = vwap_series.iloc[-1]
    if pd.isna(vwap_value):
        return None, "prior_session"

    return float(vwap_value), "current_session"
