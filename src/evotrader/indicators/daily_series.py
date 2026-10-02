"""The live daily series: the broker's completed sessions, then today's bar.

The broker's daily list ends at the PREVIOUS session all day long. Until
2026-10-02 the live price was written over that last bar, which erased
yesterday's close, high and low: every daily indicator ran with the most
recent completed session missing (found 2026-10-01, when ATR read the same at
every cycle of a wide-range day). Today's bar is now appended instead.

This lives with the indicators, not in the market-data tool, so a change to
how the series is built moves the signal engine's fingerprint
(``algorithms.composite._ENGINE_SOURCES``) like a change to any indicator.
"""

from __future__ import annotations

from datetime import date, datetime

from evotrader.tools.market_hours import ET, is_trading_day, regular_close


def daily_bar_date(bar: dict) -> date | None:
    """The session date a daily bar describes, read from its stamp as written.

    The broker stamps a daily bar at midnight UTC of its session date, so
    converting the stamp to US Eastern time would move it back a day.
    """
    stamp = bar.get("timestamp")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def session_daily_bar(
    candles: list[dict],
    live_price: float,
    now: datetime,
    session_bars: list[dict] | None = None,
) -> str:
    """End the daily series with TODAY's bar, never by overwriting yesterday's.

    By the session date in US Eastern time:

    * the last bar is today's: update it with the live price (``patched_today``);
    * it is an earlier session's: append today's bar, flagged ``forming``, built
      from today's 5-minute bars when there are any and otherwise from the live
      price alone (``forming_bar_appended``). After the close it takes the
      session's own close, not an after-hours print;
    * there is no session today, a weekend or a holiday: leave the series as it
      is (``no_session_today``).

    Returns the mode, for the snapshot to record.
    """
    if not candles or live_price <= 0:
        return "unchanged"
    now_et = now.astimezone(ET)
    today = now_et.date()
    if not is_trading_day(today):
        return "no_session_today"
    last = candles[-1]
    if daily_bar_date(last) == today:
        last["close"] = live_price
        last["high"] = max(float(last["high"]), live_price)
        last["low"] = min(float(last["low"]), live_price)
        return "patched_today"
    if session_bars:
        closed = now_et.time() >= regular_close(today)
        high = max(float(b["high"]) for b in session_bars)
        low = min(float(b["low"]) for b in session_bars)
        if closed:
            close = float(session_bars[-1]["close"])
        else:
            close, high, low = live_price, max(high, live_price), min(low, live_price)
        bar = {
            "open": float(session_bars[0]["open"]),
            "high": high,
            "low": low,
            "close": close,
            "volume": sum(float(b.get("volume") or 0.0) for b in session_bars),
        }
        bar["forming"] = "session_bars"
    else:
        bar = {"open": live_price, "high": live_price, "low": live_price, "close": live_price}
        bar["volume"] = 0.0
        bar["forming"] = "quote"  # no bar yet: its "open" is not the session's open
    candles.append({"timestamp": f"{today.isoformat()}T00:00:00+00:00", **bar})
    return "forming_bar_appended"


def last_completed_date(candles: list[dict], now: datetime) -> str | None:
    """The date of the last finished session in the daily series."""
    today = now.astimezone(ET).date()
    for bar in reversed(candles):
        day = daily_bar_date(bar)
        if day is not None and day < today:
            return day.isoformat()
    return None
