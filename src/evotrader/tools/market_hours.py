"""Market hours and trading calendar utilities.

Provides functions to determine whether markets are open, what session
we're in (pre-market, regular, after-hours), and when the next
session boundary occurs. Uses US Eastern Time for all calculations.

The holiday calendar is worked out from NYSE's standing rules for any year
(see ``market_holidays``), not typed in by hand. A hand-kept list has to be
extended every year, and when it is not, every holiday of the new year quietly
reads as a trading day. The same goes for NYSE's 1:00 pm early closes
(``early_closes``).

Rules cannot foresee everything: NYSE can announce extra closures that no rule
predicts, such as a national day of mourning or a hurricane. On such a day this
calendar still says the market is open; the broker's own answer (see
``get_market_status`` in the agent tools) is the authority.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

import logging
from datetime import date, datetime, time, timedelta
from enum import Enum
from functools import cache
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# US Eastern timezone (handles EST/EDT automatically)
ET = ZoneInfo("America/New_York")


class MarketSession(str, Enum):
    """Current market session type."""

    PRE_MARKET = "pre_market"  # 4:00 AM - 9:30 AM ET
    REGULAR = "regular"  # 9:30 AM - 4:00 PM ET (to 1:00 PM on early-close days)
    AFTER_HOURS = "after_hours"  # 4:00 PM - 8:00 PM ET (1:00 PM - 5:00 PM on early-close days)
    OVERNIGHT = "overnight"  # 8:00 PM - 4:00 AM ET (Sun-Thu)
    CLOSED = "closed"  # Outside all sessions


# Session boundaries (Eastern Time). The regular and after-hours closes depend
# on the day: ask regular_close() and after_hours_close(), which know NYSE's
# early-close days.
_PRE_MARKET_OPEN = time(4, 0)
_REGULAR_OPEN = time(9, 30)
_FULL_DAY_REGULAR_CLOSE = time(16, 0)
_FULL_DAY_AFTER_HOURS_CLOSE = time(20, 0)
_EARLY_REGULAR_CLOSE = time(13, 0)
_EARLY_AFTER_HOURS_CLOSE = time(17, 0)

# Juneteenth became a federal holiday in 2021; NYSE first closed for it in 2022.
_FIRST_JUNETEENTH = 2022

_MONDAY, _THURSDAY, _SATURDAY, _SUNDAY = 0, 3, 5, 6


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Monday is 0) of a month, e.g. the 3rd Monday."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Monday is 0) of a month, e.g. the last Monday of May."""
    after = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = after - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Western Easter Sunday, by the anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday_shift = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_shift) // 451
    month, day = divmod(h + weekday_shift - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(holiday: date) -> date:
    """Where NYSE moves a fixed-date holiday that falls on a weekend.

    Saturday moves to the Friday before, Sunday to the Monday after.
    """
    if holiday.weekday() == _SATURDAY:
        return holiday - timedelta(days=1)
    if holiday.weekday() == _SUNDAY:
        return holiday + timedelta(days=1)
    return holiday


@cache
def market_holidays(year: int) -> frozenset[date]:
    """The weekdays NYSE is closed in ``year`` under its standing holiday rules.

    Worked out once per year and kept. Extra closures NYSE announces at short
    notice are not included (see the module docstring).
    """
    days = {
        _nth_weekday(year, 1, _MONDAY, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, _MONDAY, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, _MONDAY),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, _MONDAY, 1),  # Labor Day
        _nth_weekday(year, 11, _THURSDAY, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    # New Year's Day on a Sunday moves to Monday 2 January. On a Saturday it is
    # NOT moved to Friday 31 December: NYSE's rule makes an exception for the
    # end of the year, and the exchange stays open that Friday.
    new_years_day = date(year, 1, 1)
    if new_years_day.weekday() != _SATURDAY:
        days.add(_observed(new_years_day))
    if year >= _FIRST_JUNETEENTH:
        days.add(_observed(date(year, 6, 19)))
    return frozenset(days)


@cache
def early_closes(year: int) -> frozenset[date]:
    """The days in ``year`` NYSE's regular session ends at 1:00 pm ET.

    July 3 and December 24, when they are ordinary weekdays (in some years they
    are the observed holiday itself), and the day after Thanksgiving.
    """
    holidays = market_holidays(year)
    days = {_nth_weekday(year, 11, _THURSDAY, 4) + timedelta(days=1)}
    for eve in (date(year, 7, 3), date(year, 12, 24)):
        if eve.weekday() < _SATURDAY and eve not in holidays:
            days.add(eve)
    return frozenset(days)


def _as_et_date(day: date) -> date:
    """A date as given; a datetime becomes its date in US Eastern time."""
    if isinstance(day, datetime):
        return _resolve_now(day).date()
    return day


def is_market_holiday(day: date) -> bool:
    """Whether NYSE is closed for a holiday on ``day`` (weekends are not holidays)."""
    day = _as_et_date(day)
    return day in market_holidays(day.year)


def is_trading_day(day: date) -> bool:
    """Whether ``day`` is a weekday on which NYSE opens."""
    day = _as_et_date(day)
    return day.weekday() < _SATURDAY and not is_market_holiday(day)


def is_early_close(day: date) -> bool:
    """Whether ``day`` is one of NYSE's 1:00 pm early-close days."""
    day = _as_et_date(day)
    return day in early_closes(day.year)


def regular_close(day: date) -> time:
    """When the regular session ends on ``day`` (ET): 1:00 pm on early-close days, else 4:00 pm."""
    return _EARLY_REGULAR_CLOSE if is_early_close(day) else _FULL_DAY_REGULAR_CLOSE


def after_hours_close(day: date) -> time:
    """When the after-hours session ends on ``day`` (ET): 5:00 pm on early-close days, else 8:00 pm."""
    return _EARLY_AFTER_HOURS_CLOSE if is_early_close(day) else _FULL_DAY_AFTER_HOURS_CLOSE


def _resolve_now(now: datetime | None = None) -> datetime:
    """Resolve the current datetime, checking for environment overrides."""
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=ET)
        return now.astimezone(ET)

    import os

    mock_time_str = os.environ.get("EVOTRADER_MOCK_TIME")
    if mock_time_str:
        try:
            parsed = datetime.fromisoformat(mock_time_str)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ET)
            else:
                parsed = parsed.astimezone(ET)
            return parsed
        except ValueError:
            logger.error(
                "Invalid EVOTRADER_MOCK_TIME format: %s. Use ISO format (e.g. 2026-06-17T10:00:00-04:00)",
                mock_time_str,
            )

    return datetime.now(ET)


def minutes_since_open(timestamp: datetime) -> float | None:
    """Compute minutes elapsed since the regular-session open (9:30 AM ET).

    Useful for strategies whose edge decays over the trading session
    (e.g., gap-fill fades concentrate in the first 1-2 hours).

    Args:
        timestamp: The point-in-time to measure.  Timezone-naive values
                   are treated as Eastern Time.

    Returns:
        Minutes since 9:30 AM ET as a float, or ``None`` if *timestamp*
        is before the regular open (pre-market / overnight).
    """
    if timestamp.tzinfo is None:
        ts_et = timestamp.replace(tzinfo=ET)
    else:
        ts_et = timestamp.astimezone(ET)

    open_dt = ts_et.replace(
        hour=_REGULAR_OPEN.hour,
        minute=_REGULAR_OPEN.minute,
        second=0,
        microsecond=0,
    )

    if ts_et < open_dt:
        return None

    return (ts_et - open_dt).total_seconds() / 60.0


def get_current_session(
    now: datetime | None = None, twenty_four_hour: bool = False
) -> MarketSession:
    """Determine the current market session.

    Args:
        now: Override for the current time (for testing). Defaults to now.
        twenty_four_hour: Whether to check/allow overnight trading sessions.

    Returns:
        The active ``MarketSession``.
    """
    now = _resolve_now(now)
    date_today = now.date()

    # Check if it's a holiday
    if is_market_holiday(date_today):
        return MarketSession.CLOSED

    weekday = now.weekday()
    current_time = now.time()

    # Check weekend cases
    if weekday == 5:  # Saturday
        return MarketSession.CLOSED
    elif weekday == 6:  # Sunday
        if twenty_four_hour and current_time >= time(20, 0):
            return MarketSession.OVERNIGHT
        return MarketSession.CLOSED

    # Weekdays (Mon-Fri)
    # Mon-Fri standard sessions. On an early-close day the regular session ends
    # at 1:00 pm and after-hours at 5:00 pm; until the overnight session opens at
    # 8:00 pm the market is then closed (the checks below).
    close = regular_close(date_today)
    extended_close = after_hours_close(date_today)
    if _PRE_MARKET_OPEN <= current_time < _REGULAR_OPEN:
        return MarketSession.PRE_MARKET
    elif _REGULAR_OPEN <= current_time < close:
        return MarketSession.REGULAR
    elif close <= current_time < extended_close:
        return MarketSession.AFTER_HOURS

    # Outside standard sessions (8:00 PM to 4:00 AM)
    if twenty_four_hour:
        if weekday == 4:  # Friday night after 8 PM is closed
            if current_time >= time(20, 0):
                return MarketSession.CLOSED
            if current_time < time(4, 0):
                return MarketSession.OVERNIGHT
        else:
            # Mon-Thu nights
            if current_time >= time(20, 0) or current_time < time(4, 0):
                return MarketSession.OVERNIGHT

    return MarketSession.CLOSED


def is_market_open(now: datetime | None = None) -> bool:
    """Check if the market is in regular trading hours."""
    return get_current_session(now) == MarketSession.REGULAR


def is_any_session_active(now: datetime | None = None, twenty_four_hour: bool = False) -> bool:
    """Check if any trading session is active (including pre/post-market/overnight)."""
    return get_current_session(now, twenty_four_hour=twenty_four_hour) != MarketSession.CLOSED


# Robinhood's 24-hour market covers a limited, changing set of instruments.
# This list is a *fallback* only — the authoritative answer is the configured
# `constitution.trading_rules.extended_hours_tickers`, which an operator sets
# for the instrument actually being traded. Hardcoding it here silently told the
# system that any unlisted ticker (MSTR, for instance) was ineligible.
_FALLBACK_24H_TICKERS = frozenset(
    {"QQQ", "SPY", "IWM", "DIA", "AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "GOOGL", "META"}
)


def is_twenty_four_hour_eligible(ticker: str | None, eligible: Iterable[str] | None = None) -> bool:
    """Whether *ticker* can trade in Robinhood's 24-hour market.

    Args:
        ticker: Symbol to check.
        eligible: Explicit eligible set, normally
            ``constitution.trading_rules.extended_hours_tickers``. When omitted,
            the configured list is consulted, then a small built-in fallback.
    """
    if not ticker:
        return False
    symbol = ticker.upper()

    if eligible is not None:
        return symbol in {str(t).upper() for t in eligible}

    configured = configured_extended_hours_tickers()
    if configured:
        return symbol in configured

    return symbol in _FALLBACK_24H_TICKERS


_configured_24h: frozenset[str] = frozenset()


def bind_extended_hours_tickers(tickers: Iterable[str] | None) -> None:
    """Record the configured 24-hour-eligible symbols (called at startup)."""
    global _configured_24h
    _configured_24h = frozenset(str(t).upper() for t in (tickers or []))


def configured_extended_hours_tickers() -> frozenset[str]:
    return _configured_24h


def get_allowed_order_types(ticker: str, session: MarketSession) -> list[str]:
    """Get allowed order types for a ticker and market session."""
    if session == MarketSession.REGULAR:
        return ["market", "limit", "stop_loss", "stop_limit"]
    elif session in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS, MarketSession.OVERNIGHT):
        return ["limit"]
    else:
        return []


def get_trading_interval(
    now: datetime | None = None,
    regular_interval: int = 300,
    extended_interval: int = 1800,
    overnight_interval: int = 3600,
    twenty_four_hour: bool = False,
) -> int:
    """Get the appropriate polling interval based on the current session.

    Args:
        now: Override for current time.
        regular_interval: Interval in seconds during regular hours.
        extended_interval: Interval in seconds during pre/post-market.
        overnight_interval: Interval in seconds during overnight hours.
        twenty_four_hour: If True, support overnight session.

    Returns:
        Interval in seconds.
    """
    session = get_current_session(now, twenty_four_hour=twenty_four_hour)
    if session == MarketSession.REGULAR:
        return regular_interval
    elif session in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS):
        return extended_interval
    elif session == MarketSession.OVERNIGHT:
        return overnight_interval
    else:
        # Market closed — return a long interval
        return 3600


def seconds_until_next_session(now: datetime | None = None, twenty_four_hour: bool = False) -> int:
    """Calculate seconds until the next trading session begins.

    Useful for scheduling wake-up timers when the market is closed.
    """
    now = _resolve_now(now)

    # If we're in a session, return 0
    if is_any_session_active(now, twenty_four_hour=twenty_four_hour):
        return 0

    # Search day-by-day starting from now's date up to 7 days.
    # On each day, the possible transition times to active sessions are:
    # 1. 04:00 AM (Pre-market opens) - Mon-Fri
    # 2. 08:00 PM (Overnight opens) - Sun-Thu (if twenty_four_hour is True)
    candidate_times = []
    for i in range(8):
        check_date = (now + timedelta(days=i)).date()
        candidate_times.append(datetime.combine(check_date, time(4, 0), tzinfo=ET))
        if twenty_four_hour:
            candidate_times.append(datetime.combine(check_date, time(20, 0), tzinfo=ET))

    candidate_times.sort()
    for dt in candidate_times:
        if dt > now:
            if is_any_session_active(dt, twenty_four_hour=twenty_four_hour):
                delta = dt - now
                return max(0, int(delta.total_seconds()))

    return 3600
