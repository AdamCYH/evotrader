"""The cycle schedule must express more than one time-of-day pattern.

Operator request, 2026-09-21: run an extra trading cycle one hour after the
close, so the agent can act on after-hours moves (MSTR spiked then dropped after
the 09-21 close while the system was idle from 15:30 ET).

`cycle_cron` held a single 5-field expression. "every hour 8:30-15:30" and
"17:00" differ in BOTH the minute and hour fields, so no single cron line covers
them — standard cron has no OR across fields. The field now accepts a list, and
a cycle fires when ANY expression matches.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.cron import CronTrigger, iter_cron_expressions
from evotrader.tools.market_hours import ET


def _et(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


class TestAScheduleMayHoldSeveralExpressions:
    def test_a_bare_string_still_works(self) -> None:
        assert iter_cron_expressions("30 8-15 * * 1-5") == ["30 8-15 * * 1-5"]

    def test_a_list_is_returned_in_order(self) -> None:
        assert iter_cron_expressions(["30 8-15 * * 1-5", "0 17 * * 1-5"]) == [
            "30 8-15 * * 1-5",
            "0 17 * * 1-5",
        ]

    def test_none_and_blanks_yield_nothing(self) -> None:
        assert iter_cron_expressions(None) == []
        assert iter_cron_expressions("") == []
        assert iter_cron_expressions(["  ", ""]) == []

    def test_blanks_inside_a_list_are_dropped_not_parsed(self) -> None:
        assert iter_cron_expressions(["0 17 * * 1-5", ""]) == ["0 17 * * 1-5"]


class TestTheDeployedScheduleFiresWhenIntended:
    """The regression case: 17:00 ET on a weekday must trigger a cycle."""

    _SCHEDULE = ["30 8-15 * * 1-5", "0 17 * * 1-5"]

    def _fires(self, when: datetime) -> bool:
        return any(CronTrigger(e).matches(when) for e in iter_cron_expressions(self._SCHEDULE))

    def test_the_after_hours_cycle_fires_at_1700_et(self) -> None:
        assert self._fires(_et(9, 21, 17, 0)), "17:00 ET Monday — one hour after the close"

    def test_the_regular_session_cycles_still_fire(self) -> None:
        for hour in range(8, 16):
            assert self._fires(_et(9, 21, hour, 30)), f"{hour}:30 ET"

    @pytest.mark.parametrize("hour,minute", [(16, 0), (17, 30), (18, 0), (20, 0), (7, 30)])
    def test_it_does_not_fire_at_other_times(self, hour: int, minute: int) -> None:
        assert not self._fires(_et(9, 21, hour, minute))

    def test_it_does_not_fire_at_the_weekend(self) -> None:
        assert not self._fires(_et(9, 19, 17, 0))  # Saturday
        assert not self._fires(_et(9, 20, 17, 0))  # Sunday

    def test_1700_et_is_inside_the_after_hours_session(self) -> None:
        """A cycle that fires when no session is live could only queue orders
        to the next open, which is worse than not running."""
        from evotrader.tools.market_hours import MarketSession, get_current_session

        assert get_current_session(_et(9, 21, 17, 0)) == MarketSession.AFTER_HOURS


class TestSettingsYamlMayHoldTheList:
    def test_the_loaded_config_accepts_the_list(self, update_data_yaml) -> None:
        from evotrader.config import AppConfig

        schedule = TestTheDeployedScheduleFiresWhenIntended._SCHEDULE
        update_data_yaml("settings.yaml", {"schedule": {"cycle_cron": schedule}})
        cron = AppConfig().settings.schedule.cycle_cron
        assert len(iter_cron_expressions(cron)) == 2
