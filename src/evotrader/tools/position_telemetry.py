"""How far a held lot has come, in the instrument's own units.

Every cycle the strategy agent judged its target by hand: where is price
against the entry, against the first target (T1), and how far did the lot ever
get. Those numbers moved by several tenths of an ATR from one cycle's arithmetic
to the next, and the question "how close do positions come to their target
before they are stopped or trimmed" (the maximum favourable excursion) could
only be answered from notes.

``lot_excursion`` computes them from the bars the market-data tool already has,
so they read the same every cycle and accumulate in the snapshots:

* ``gain_atr``: the mark against the lot's entry, in daily ATRs (negative is a
  loss).
* ``mfe_atr``: the best price since the entry against the entry, never below 0.
  From the daily bars of the sessions AFTER the entry's session, plus, for a
  lot bought today, today's 5-minute bars that started at or after the entry,
  plus the mark. The entry session's own high is left out for a lot bought
  before today: it may have come before the entry, and counting it would claim
  an excursion the lot never had (``mfe_basis`` says what was read).
* ``t1_distance_atr``: when a target multiple is configured
  (``position_sizing.target_atr_multiplier``), how many ATRs the mark still is
  from entry + that many ATRs (``t1_price``). Negative means past it.

ATR is today's daily ATR, the one the stops are sized with. Long and short lots
are both handled; a short's favourable direction is down.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from evotrader.indicators.daily_series import daily_bar_date
from evotrader.tools.market_hours import ET


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def lot_excursion(
    *,
    direction: str | None,
    entry_price: float,
    entry_time: Any,
    daily_bars: Sequence[dict[str, Any]],
    session_bars: Sequence[dict[str, Any]],
    mark: float | None,
    atr: float | None,
    now: datetime,
    target_atr: float | None = None,
) -> dict[str, Any]:
    """The lot's gain, best excursion and distance to T1, in daily ATRs.

    Returns an empty dict when there is no ATR, entry or mark to measure with.
    """
    if not atr or atr <= 0 or not entry_price or entry_price <= 0 or not mark or mark <= 0:
        return {}
    short = str(direction or "LONG").upper() == "SHORT"
    sign = -1.0 if short else 1.0
    opened = _when(entry_time)
    entry_day = opened.astimezone(ET).date() if opened and opened.tzinfo else None
    today = now.astimezone(ET).date()

    prices: list[float] = [float(mark)]
    basis = "mark"
    if entry_day is not None:
        later = [b for b in daily_bars if (d := daily_bar_date(b)) is not None and d > entry_day]
        if later:
            prices += [float(b["low"] if short else b["high"]) for b in later]
            basis = "daily_bars_after_entry_session"
        if entry_day == today and opened is not None:
            since = [
                b
                for b in session_bars
                if (start := _when(b.get("timestamp"))) is not None and start >= opened
            ]
            if since:
                prices += [float(b["low"] if short else b["high"]) for b in since]
                basis = "session_bars_after_entry"
    best = min(prices) if short else max(prices)

    out: dict[str, Any] = {
        "gain_atr": round(sign * (float(mark) - entry_price) / atr, 4),
        "mfe_atr": round(max(0.0, sign * (best - entry_price) / atr), 4),
        "mfe_price": round(best, 4),
        "mfe_basis": basis,
    }
    if target_atr:
        t1 = entry_price + sign * target_atr * atr
        out["t1_price"] = round(t1, 4)
        out["t1_distance_atr"] = round(sign * (t1 - float(mark)) / atr, 4)
    return out
