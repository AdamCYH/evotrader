"""Cron parsing and scheduling utilities.

Parses standard 5-field cron expressions and calculates matching datetimes
as well as the next scheduled run time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from evotrader.tools.market_hours import ET

logger = logging.getLogger(__name__)


def parse_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field and return the set of valid integer values."""
    values: set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif "/" in part:
            left, right = part.split("/")
            step = int(right)
            if left == "*":
                start = min_val
                end = max_val
            elif "-" in left:
                start_str, end_str = left.split("-")
                start = int(start_str)
                end = int(end_str)
            else:
                start = int(left)
                end = max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            start_str, end_str = part.split("-")
            start = int(start_str)
            end = int(end_str)
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    return values


def iter_cron_expressions(value: object) -> list[str]:
    """Normalise a schedule setting to a list of cron expressions.

    A schedule needs more than one time-of-day pattern: hourly through the
    session PLUS a single run after the close. Those differ in both the minute
    and the hour field, and cron has no OR across fields, so one expression
    cannot say it. A caller fires when ANY returned expression matches.

    Accepts a bare string (the historical shape) or a list. Blank entries are
    dropped rather than parsed — an empty string matches nothing useful and
    would raise on parse.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def crons_can_coincide(first: str, second: str) -> bool:
    """Whether two cron expressions can fire on the same minute.

    Every field must share a value (this module ANDs the five fields).
    Unparseable expressions are reported as not coinciding.
    """
    try:
        a, b = CronTrigger(first), CronTrigger(second)
    except ValueError:
        return False
    return all(
        x & y
        for x, y in (
            (a.minutes, b.minutes),
            (a.hours, b.hours),
            (a.days, b.days),
            (a.months, b.months),
            (a.dows, b.dows),
        )
    )


class CronTrigger:
    """Parses a standard 5-field cron expression and calculates matching datetimes."""

    def __init__(self, cron_expr: str) -> None:
        self.cron_expr = cron_expr.strip()
        fields = self.cron_expr.split()
        if len(fields) != 5:
            raise ValueError(
                f"Cron expression must have exactly 5 fields, got {len(fields)}: '{cron_expr}'"
            )

        self.minutes = parse_field(fields[0], 0, 59)
        self.hours = parse_field(fields[1], 0, 23)
        self.days = parse_field(fields[2], 1, 31)
        self.months = parse_field(fields[3], 1, 12)

        # day of week field: normalize both 0 and 7 to Sunday (0)
        dows = parse_field(fields[4], 0, 7)
        self.dows = set()
        for d in dows:
            if d == 7:
                self.dows.add(0)
            else:
                self.dows.add(d)

    def matches(self, dt: datetime) -> bool:
        """Check if a datetime (naive or aware) matches the cron schedule.

        The datetime should represent local time in the target market timezone (US Eastern).
        """
        # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
        # Map to Cron weekday: Sun=0, Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6
        cron_dow = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.day in self.days
            and dt.month in self.months
            and cron_dow in self.dows
        )

    def next_run(self, start_dt: datetime) -> datetime:
        """Find the next scheduled run datetime strictly after start_dt.

        Searches minute-by-minute up to 10 days (14,400 minutes) into the future.
        The matching is evaluated in US/Eastern to align with market hours.
        """

        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)

        # Iterate in UTC to avoid daylight savings arithmetic bugs
        current_utc = start_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Check up to 10 days ahead
        for _ in range(14400):
            current_et = current_utc.astimezone(ET)
            if self.matches(current_et):
                return current_utc
            current_utc += timedelta(minutes=1)

        # Fallback if no match within 10 days
        return start_dt + timedelta(days=10)
