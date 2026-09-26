"""Unit tests for the CronTrigger parser and scheduler."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from evotrader.cron import CronTrigger

ET = ZoneInfo("America/New_York")


def test_cron_trigger_parsing() -> None:
    # Test valid 5-field cron parsing
    trigger = CronTrigger("30 8-15 * * 1-5")
    assert 30 in trigger.minutes
    assert len(trigger.minutes) == 1
    assert all(h in trigger.hours for h in range(8, 16))
    assert len(trigger.hours) == 8
    assert all(d in trigger.days for d in range(1, 32))
    assert all(m in trigger.months for m in range(1, 13))
    assert all(dow in trigger.dows for dow in range(1, 6))  # Mon-Fri
    assert 0 not in trigger.dows  # Sunday is not in it


def test_cron_trigger_sunday_normalization() -> None:
    # Test Sunday is 0 or 7
    trigger_7 = CronTrigger("* * * * 7")
    assert 0 in trigger_7.dows

    trigger_0 = CronTrigger("* * * * 0")
    assert 0 in trigger_0.dows


def test_cron_trigger_matching() -> None:
    trigger = CronTrigger("30 8-15 * * 1-5")

    # Matches: Monday (weekday=0, cron_dow=1), 8:30 AM
    dt1 = datetime(2026, 6, 22, 8, 30, tzinfo=ET)  # June 22, 2026 is Monday
    assert trigger.matches(dt1) is True

    # Fails minute: 8:31 AM
    dt2 = datetime(2026, 6, 22, 8, 31, tzinfo=ET)
    assert trigger.matches(dt2) is False

    # Fails hour: 7:30 AM
    dt3 = datetime(2026, 6, 22, 7, 30, tzinfo=ET)
    assert trigger.matches(dt3) is False

    # Fails day of week: Saturday (2026-06-27 is Saturday)
    dt4 = datetime(2026, 6, 27, 8, 30, tzinfo=ET)
    assert trigger.matches(dt4) is False


def test_cron_trigger_next_run() -> None:
    trigger = CronTrigger("30 8-15 * * 1-5")

    # Starting Sunday night: June 21, 2026, 10:00 PM
    start_dt = datetime(2026, 6, 21, 22, 0, tzinfo=ET)

    # Next run should be Monday morning: June 22, 2026, 8:30 AM
    expected = datetime(2026, 6, 22, 8, 30, tzinfo=ET)
    assert trigger.next_run(start_dt) == expected

    # Starting Monday morning: June 22, 2026, 8:30 AM
    start_dt2 = datetime(2026, 6, 22, 8, 30, tzinfo=ET)
    # Next run should be Monday morning 9:30 AM
    expected2 = datetime(2026, 6, 22, 9, 30, tzinfo=ET)
    assert trigger.next_run(start_dt2) == expected2


def test_invalid_cron_expression() -> None:
    with pytest.raises(ValueError):
        CronTrigger("* * * *")  # 4 fields instead of 5
