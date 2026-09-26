"""Regression tests: the market calendar follows NYSE's standing rules, not a typed-in list.

The calendar was a hand-kept set of 2026 dates marked "extend annually". From
2027-01-01 every market holiday would have read as a trading day, and even the
2026 list missed Juneteenth: NYSE has closed on June 19 since 2022. Early-close
days (1:00 pm ET) were not known at all, so on the day after Thanksgiving the
app would have believed the regular session ran until 4:00 pm, and practice
stops could have triggered in what is really the after-hours session.

The expected dates below are NYSE's published calendar, written out by hand,
not re-derived from the rules under test.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from evotrader.tools import market_hours
from evotrader.tools.market_hours import (
    ET,
    MarketSession,
    get_current_session,
    is_any_session_active,
    is_market_open,
    seconds_until_next_session,
)

HOLIDAYS_2026 = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth (missing from the old list)",
    date(2026, 7, 3): "Independence Day, observed: July 4 is a Saturday",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving",
    date(2026, 12, 25): "Christmas",
}

HOLIDAYS_2027 = {
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth, observed: June 19 is a Saturday",
    date(2027, 7, 5): "Independence Day, observed: July 4 is a Sunday",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving",
    date(2027, 12, 24): "Christmas, observed: December 25 is a Saturday",
}

EARLY_CLOSES_2026 = {
    date(2026, 11, 27): "the day after Thanksgiving",
    date(2026, 12, 24): "Christmas Eve",
}


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def _ids(table: dict[date, str]) -> list[str]:
    return [f"{d.isoformat()} {name}" for d, name in table.items()]


class TestHolidays:
    @pytest.mark.parametrize(
        "day",
        list(HOLIDAYS_2026) + list(HOLIDAYS_2027),
        ids=_ids(HOLIDAYS_2026) + _ids(HOLIDAYS_2027),
    )
    def test_the_market_is_closed_all_day(self, day: date) -> None:
        for hour in (5, 12, 17):
            assert get_current_session(_at(day, hour)) == MarketSession.CLOSED, hour
        assert is_market_open(_at(day, 12)) is False
        assert is_any_session_active(_at(day, 12)) is False

    def test_the_2026_holidays(self) -> None:
        assert market_hours.market_holidays(2026) == set(HOLIDAYS_2026)

    def test_the_2027_holidays(self) -> None:
        assert market_hours.market_holidays(2027) == set(HOLIDAYS_2027)

    def test_new_years_day_on_a_saturday_leaves_the_friday_before_open(self) -> None:
        """1 Jan 2028 is a Saturday. NYSE does not close on Friday 31 Dec 2027
        instead: its rule makes an exception for the end of the year."""
        new_years_eve = date(2027, 12, 31)
        assert get_current_session(_at(new_years_eve, 12)) == MarketSession.REGULAR
        assert market_hours.is_market_holiday(new_years_eve) is False
        assert new_years_eve not in market_hours.market_holidays(2027)
        assert not any(d.month == 1 and d.day <= 3 for d in market_hours.market_holidays(2028))

    def test_good_friday_2028(self) -> None:
        good_friday = date(2028, 4, 14)
        assert get_current_session(_at(good_friday, 12)) == MarketSession.CLOSED
        assert good_friday in market_hours.market_holidays(2028)

    @pytest.mark.parametrize("year", range(2022, 2041))
    def test_every_year_has_its_ten_holidays_on_weekdays(self, year: int) -> None:
        """Nine when New Year's Day is a Saturday (it is not moved to the Friday)."""
        holidays = market_hours.market_holidays(year)
        assert all(d.year == year and d.weekday() < 5 for d in holidays), sorted(holidays)
        expected = 9 if date(year, 1, 1).weekday() == 5 else 10
        assert len(holidays) == expected, sorted(holidays)

    def test_the_wake_up_timer_skips_a_holiday(self) -> None:
        """Thursday evening before Juneteenth: the next session is Monday's pre-market."""
        thursday_night = _at(date(2026, 6, 18), 21)
        monday_pre_market = _at(date(2026, 6, 22), 4)
        expected = int((monday_pre_market - thursday_night).total_seconds())
        assert seconds_until_next_session(thursday_night) == expected


class TestEarlyCloses:
    @pytest.mark.parametrize("day", list(EARLY_CLOSES_2026), ids=_ids(EARLY_CLOSES_2026))
    def test_the_regular_session_ends_at_one(self, day: date) -> None:
        assert get_current_session(_at(day, 9, 30)) == MarketSession.REGULAR
        assert get_current_session(_at(day, 12, 59)) == MarketSession.REGULAR
        assert get_current_session(_at(day, 13, 0)) == MarketSession.AFTER_HOURS
        assert is_market_open(_at(day, 13, 30)) is False
        assert get_current_session(_at(day, 16, 59)) == MarketSession.AFTER_HOURS
        assert get_current_session(_at(day, 17, 0)) == MarketSession.CLOSED
        assert market_hours.regular_close(day) == time(13, 0)
        assert market_hours.after_hours_close(day) == time(17, 0)

    def test_the_app_clock_override_sees_the_early_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Practice cycles run on EVOTRADER_MOCK_TIME; stops trigger only while
        is_market_open() says the regular session is on."""
        monkeypatch.setenv("EVOTRADER_MOCK_TIME", "2026-11-27T13:30:00-05:00")
        assert is_market_open() is False
        assert get_current_session() == MarketSession.AFTER_HOURS

    @pytest.mark.parametrize(
        ("now", "label"),
        [
            ("2026-11-27T12:00:00-05:00", "Regular hours (9:30 AM – 1:00 PM ET, early close)"),
            ("2026-11-27T13:30:00-05:00", "After-hours (1:00 PM – 5:00 PM ET, early close)"),
            ("2026-11-25T12:00:00-05:00", "Regular hours (9:30 AM – 4:00 PM ET)"),
            ("2026-11-25T17:00:00-05:00", "After-hours (4:00 PM – 8:00 PM ET)"),
        ],
    )
    def test_the_agents_are_told_the_days_real_hours(
        self, monkeypatch: pytest.MonkeyPatch, now: str, label: str
    ) -> None:
        """The agents' temporal context names the session with its hours; after a
        1:00 pm close it must not say the after-hours session starts at 4:00 pm."""
        from evotrader.agents.temporal_context import build_temporal_context
        from evotrader.models.config import ScheduleConfig

        monkeypatch.setenv("EVOTRADER_MOCK_TIME", now)
        assert f"- **Market session**: {label}\n" in build_temporal_context(ScheduleConfig())

    def test_july_3_on_an_ordinary_weekday_closes_early(self) -> None:
        assert market_hours.regular_close(date(2028, 7, 3)) == time(13, 0)  # a Monday

    def test_when_the_eve_is_the_holiday_the_day_before_is_a_full_day(self) -> None:
        """July 3, 2026 and December 24, 2027 are the holidays themselves, so no
        early close moves to the day before."""
        for day in (date(2026, 7, 2), date(2027, 12, 23)):
            assert get_current_session(_at(day, 15, 30)) == MarketSession.REGULAR
            assert market_hours.regular_close(day) == time(16, 0)


class TestAnOrdinaryDay:
    """Nothing changes on a normal trading day."""

    DAY = date(2026, 9, 24)  # a Thursday

    def test_a_normal_weekday_is_open(self) -> None:
        assert is_market_open(_at(self.DAY, 12)) is True
        assert market_hours.is_market_holiday(self.DAY) is False
        assert market_hours.is_trading_day(self.DAY) is True
        assert market_hours.regular_close(self.DAY) == time(16, 0)
        assert market_hours.after_hours_close(self.DAY) == time(20, 0)

    def test_the_days_around_a_holiday_are_open(self) -> None:
        assert is_market_open(_at(date(2026, 6, 18), 12)) is True  # the day before Juneteenth
        assert is_market_open(_at(date(2026, 7, 6), 12)) is True  # after the observed July 4

    def test_weekends_are_not_trading_days(self) -> None:
        assert market_hours.is_trading_day(date(2026, 9, 26)) is False  # Saturday
        assert market_hours.is_trading_day(date(2026, 6, 19)) is False  # Juneteenth, a Friday

    @pytest.mark.parametrize(
        ("hour", "minute", "session"),
        [
            (3, 59, MarketSession.CLOSED),
            (4, 0, MarketSession.PRE_MARKET),
            (9, 29, MarketSession.PRE_MARKET),
            (9, 30, MarketSession.REGULAR),
            (15, 59, MarketSession.REGULAR),
            (16, 0, MarketSession.AFTER_HOURS),
            (19, 59, MarketSession.AFTER_HOURS),
            (20, 0, MarketSession.CLOSED),
        ],
    )
    def test_the_session_boundaries_are_unchanged(
        self, hour: int, minute: int, session: MarketSession
    ) -> None:
        assert get_current_session(_at(self.DAY, hour, minute)) == session

    def test_the_twenty_four_hour_market_is_unchanged(self) -> None:
        assert (
            get_current_session(_at(self.DAY, 20), twenty_four_hour=True) == MarketSession.OVERNIGHT
        )
        assert (
            get_current_session(_at(self.DAY, 2), twenty_four_hour=True) == MarketSession.OVERNIGHT
        )


class TestPracticeDayOrdersFollowTheCalendar:
    """The practice broker ends a day order at the real close of its session."""

    def test_a_day_order_placed_on_an_early_close_day_ends_at_one(self) -> None:
        from evotrader.sim.sim_broker import _session_close_after

        day = date(2026, 11, 27)
        assert _session_close_after(_at(day, 10)) == _at(day, 13)

    def test_placed_after_an_early_close_it_lasts_until_the_next_close(self) -> None:
        from evotrader.sim.sim_broker import _session_close_after

        placed = _at(date(2026, 11, 27), 14)
        assert _session_close_after(placed) == _at(date(2026, 11, 30), 16)

    def test_a_holiday_is_skipped(self) -> None:
        """Placed after Thursday's close, it is for the next session. Friday is
        Juneteenth, so that is Monday's."""
        from evotrader.sim.sim_broker import _session_close_after

        placed = _at(date(2026, 6, 18), 17)
        assert _session_close_after(placed) == _at(date(2026, 6, 22), 16)

    def test_the_stale_order_check_uses_the_early_close(self) -> None:
        from evotrader.sim.sim_broker import _day_order_expired

        # Placed 10:00 ET, stored in UTC as the sim stores it.
        order = {"time_in_force": "gfd", "timestamp": "2026-11-27 15:00:00"}
        assert _day_order_expired(order, _at(date(2026, 11, 27), 12, 59)) is False
        assert _day_order_expired(order, _at(date(2026, 11, 27), 13)) is True
