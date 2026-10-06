"""The combined signal per trading day, for the account chart's signal overlay.

The account value is one point per day and the combined signal one reading per
cycle, about nine a day. To draw them on the same dates, each day's readings
are summarised: their average (the bar under the curve), the lowest and highest
(the day's range), the last one, how many there were, and the regime most of
them were read in, which sets the thresholds the console's lean labels use.

A day is the US Eastern date, the calendar the equity curve uses. On a day the
system read more than one instrument, only the main one counts: the instrument
with the most readings that day, the later one on a tie.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from evotrader.utils import utc_timestamp_to_et_date


def daily_signal_summary(readings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per Eastern date that has readings, oldest first.

    Each reading needs ``timestamp`` (UTC ISO) and ``composite_signal``; ``ticker``
    and ``regime`` are used when present. A reading without a value or a
    readable timestamp is skipped.
    """
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for reading in readings:
        value, stamp = reading.get("composite_signal"), reading.get("timestamp")
        if value is None or not stamp:
            continue
        try:
            day = utc_timestamp_to_et_date(str(stamp))
        except ValueError:
            continue
        by_day.setdefault(day, []).append(reading)

    days = []
    for day in sorted(by_day):
        rows = sorted(by_day[day], key=lambda r: str(r["timestamp"]))
        counts = Counter(r.get("ticker") for r in rows)
        latest = {r.get("ticker"): i for i, r in enumerate(rows)}
        ticker = max(counts, key=lambda t: (counts[t], latest[t]))
        mine = [r for r in rows if r.get("ticker") == ticker]
        values = [float(r["composite_signal"]) for r in mine]
        regimes = Counter(r.get("regime") for r in mine if r.get("regime"))
        days.append(
            {
                "date": day,
                "ticker": ticker,
                "count": len(values),
                "mean": round(sum(values) / len(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                "last": round(values[-1], 4),
                "regime": regimes.most_common(1)[0][0] if regimes else None,
            }
        )
    return days
