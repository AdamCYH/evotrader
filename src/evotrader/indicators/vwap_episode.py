"""The latest intraday dip through session VWAP, and its reclaim.

One measurement, shared by every channel that reads a VWAP reclaim
(``vwap_reclaim_continuation`` and ``vwap_reclaim_fade``), so that they can
never disagree about WHETHER a reclaim happened, only about what it means.
Pure arithmetic over the session's completed 5-minute bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evotrader.models.market import OHLCV


@dataclass(frozen=True)
class VwapDipEpisode:
    """The latest dip through session VWAP against the stack, and its reclaim."""

    lookback_bars_seen: int
    #: Price never closed on the dip side of VWAP inside the window.
    no_cross: bool
    dip_bars: int = 0
    depth_atr: float = 0.0
    bars_since_reclaim: int = 0
    age_min: float | None = None
    age_source: str = "no_reclaim_bar"
    #: The live price is back on the trend side of VWAP.
    reclaimed_now: bool = False

    def as_meta(self) -> dict[str, Any]:
        """The episode fields every reclaim channel records, in this order."""
        return {
            "dip_bars": self.dip_bars,
            "depth_atr": round(self.depth_atr, 4),
            "bars_since_reclaim": self.bars_since_reclaim,
            "age_min": round(self.age_min, 2) if self.age_min is not None else None,
            "age_source": self.age_source,
        }


def locate_vwap_dip_episode(
    candles: Sequence[OHLCV],
    *,
    vwap: float,
    atr: float,
    bull_stack: bool,
    price: float,
    lookback_bars: int,
    now: datetime | None,
) -> VwapDipEpisode:
    """Find the most recent dip through VWAP and measure its reclaim.

    Shared by every channel that reads a VWAP reclaim, so they can never
    disagree about WHETHER a reclaim happened, only about what it means.

    A dip is a run of closes on the wrong side of VWAP for the stack (below it
    in a bull stack, above it in a bear stack). Its depth is the furthest close
    from VWAP in daily ATRs; its age is wall-clock minutes from the first bar
    that closed back on the trend side.
    """
    window = candles[-lookback_bars:]
    closes = [c.close for c in window]
    n = len(closes)

    def _is_dip(close_value: float) -> bool:
        return close_value < vwap if bull_stack else close_value > vwap

    last_dip_idx = -1
    for i in range(n - 1, -1, -1):
        if _is_dip(closes[i]):
            last_dip_idx = i
            break

    if last_dip_idx < 0:
        return VwapDipEpisode(lookback_bars_seen=n, no_cross=True)

    start_idx = last_dip_idx
    while start_idx - 1 >= 0 and _is_dip(closes[start_idx - 1]):
        start_idx -= 1

    episode = closes[start_idx : last_dip_idx + 1]
    extreme = min(episode) if bull_stack else max(episode)
    bars_since_reclaim = (n - 1) - last_dip_idx

    # Age of the reclaim in WALL-CLOCK minutes, from the first bar that
    # closed back on the trend side of VWAP. Falls back to bars x 5 only
    # when timestamps are absent, and says so.
    age_min: float | None = None
    age_source = "bars_assumed_5min"
    reclaim_bar = window[last_dip_idx + 1] if last_dip_idx + 1 < n else None
    if reclaim_bar is None:
        # No bar has closed back on the trend side yet, so there is no age
        # to measure. This used to read 'bars_assumed_5min' — the label for
        # a MISSING TIMESTAMP — on every not-reclaimed cycle, which made a
        # healthy clock look broken in the census.
        age_source = "no_reclaim_bar"
    reclaim_ts = getattr(reclaim_bar, "timestamp", None) if reclaim_bar else None
    now_ts = now
    if reclaim_ts is not None and now_ts is not None:
        try:
            if reclaim_ts.tzinfo is None and now_ts.tzinfo is not None:
                reclaim_ts = reclaim_ts.replace(tzinfo=now_ts.tzinfo)
            elif now_ts.tzinfo is None and reclaim_ts.tzinfo is not None:
                now_ts = now_ts.replace(tzinfo=reclaim_ts.tzinfo)
            delta = (now_ts - reclaim_ts).total_seconds() / 60.0
            if delta >= 0:
                age_min = delta
                age_source = "candle_timestamps"
        except Exception:
            age_min = None
    if age_min is None and reclaim_bar is not None:
        age_min = float(bars_since_reclaim) * 5.0

    return VwapDipEpisode(
        lookback_bars_seen=n,
        no_cross=False,
        dip_bars=len(episode),
        depth_atr=abs(vwap - extreme) / atr,
        bars_since_reclaim=bars_since_reclaim,
        age_min=age_min,
        age_source=age_source,
        reclaimed_now=(price > vwap) if bull_stack else (price < vwap),
    )
