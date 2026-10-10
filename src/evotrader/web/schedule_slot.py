"""The console's cron schedules: a due trigger waits until it can start.

The scheduler polls every ten seconds. It used to fire a job only when the
job's cron expression matched the CURRENT minute, and it marked the minute done
before checking whether anything else was running. Two ways that lost cycles:

* A trigger due while another task ran was logged "skipped" and never looked
  at again. And when a cycle and the weekly evolution run shared a minute,
  both started in the same poll and ran side by side: the cycle's task had
  not marked itself running yet when the evolution run was checked.
* A minute the loop never saw (a stalled event loop, a host asleep across the
  whole minute) never matched, so its cycle silently never fired.

A ``ScheduleSlot`` keeps a trigger due until the job actually starts, or until
its grace window lapses (then it is reported as skipped). It looks at every
minute since the previous poll, so a late poll starts the job late rather than
never; after a long gap only the latest due minute is kept, so a backlog never
fires as a burst, and every due minute that can no longer start is reported as
skipped, with why. ``CronScheduler`` holds the three slots and the rule between
them: the trading cycle goes first, the evolution run waits behind it, the
metrics job runs whatever else is running. Minutes before the app started are
not caught up: whether the previous process ran them is unknown.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from evotrader.cron import CronTrigger

logger = logging.getLogger(__name__)

_MINUTE = timedelta(minutes=1)

#: How late a due trigger may still start. A cycle 15 minutes late still reads
#: the same hour; one 50 minutes late would run into the next.
CYCLE_GRACE = timedelta(minutes=20)
EVOLUTION_GRACE = timedelta(hours=2)
METRICS_GRACE = timedelta(hours=6)
#: How far back a poll after a gap looks for the minutes it did not see.
LOOKBACK = timedelta(hours=24)
#: Polls come every ten seconds; a longer gap than this is a stall or a sleep.
_GAP = timedelta(minutes=2)


@dataclass
class ScheduleSlot:
    """One schedule's due trigger, kept until it starts or its grace lapses."""

    name: str
    grace: timedelta
    due_at: datetime | None = None
    fired_for: datetime | None = None
    #: The due trigger has had to wait for another task at least once.
    waited: bool = False

    def observe(
        self, triggers: Sequence[CronTrigger], last_poll: datetime | None, now_minute: datetime
    ) -> list[tuple[datetime, str]]:
        """Mark the latest matching minute in (last_poll, now_minute] as due.

        The latest one inside the grace window becomes due. Returns the minutes
        that will now never start, each with why: older matching minutes the
        poll did not see in time, and a still-due minute the new one replaces.
        The first poll looks at its own minute only: whether the previous
        process ran the minutes before it is unknown.
        """
        if not triggers:
            return []
        start = now_minute if last_poll is None else max(last_poll + _MINUTE, now_minute - LOOKBACK)
        newest: datetime | None = None
        unseen: list[datetime] = []
        probe = now_minute
        while probe >= start:  # newest first
            if any(trigger.matches(probe) for trigger in triggers):
                if probe == self.fired_for or (self.due_at is not None and probe <= self.due_at):
                    break  # known already, and so is every minute before it
                if newest is None and now_minute - probe <= self.grace:
                    newest = probe
                else:
                    unseen.append(probe)
            probe -= _MINUTE
        lost = [
            (
                minute,
                f"the scheduler did not see its minute until {now_minute:%H:%M} "
                "(the app was stalled or asleep)",
            )
            for minute in unseen
        ]
        if newest is not None:
            if self.due_at is not None:
                lost.append(
                    (self.due_at, f"still waiting when the next run came due at {newest:%H:%M}")
                )
            self.due_at, self.waited = newest, False
        return lost

    def expire(self, now_minute: datetime) -> datetime | None:
        """Clear and return the due minute whose grace has lapsed, if any."""
        if self.due_at is not None and now_minute - self.due_at > self.grace:
            dropped, self.due_at = self.due_at, None
            return dropped
        return None

    def take(self, now_minute: datetime) -> datetime | None:
        """Pop the due minute to start now (inside its grace), else None."""
        if self.due_at is None or now_minute - self.due_at > self.grace:
            return None
        due, self.due_at = self.due_at, None
        self.fired_for = due
        return due

    def clear(self) -> None:
        """Forget a pending trigger (the schedule was switched off)."""
        self.due_at = None


@dataclass
class Start:
    """A job to start now. ``late_reason`` is set when it starts after its minute."""

    job: str
    due_at: datetime
    late_reason: str | None = None


@dataclass
class Skip:
    job: str
    due_at: datetime
    reason: str


@dataclass
class Tick:
    start: list[Start] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    #: Jobs still due that wait for a running task, with their due minute.
    deferred: list[tuple[str, datetime]] = field(default_factory=list)


class CronScheduler:
    """The cycle, evolution and metrics schedules, polled once per tick."""

    JOBS = ("cycle", "evolution", "metrics")

    def __init__(
        self,
        cycle_grace: timedelta = CYCLE_GRACE,
        evolution_grace: timedelta = EVOLUTION_GRACE,
        metrics_grace: timedelta = METRICS_GRACE,
    ) -> None:
        self.slots = {
            "cycle": ScheduleSlot("cycle", cycle_grace),
            "evolution": ScheduleSlot("evolution", evolution_grace),
            "metrics": ScheduleSlot("metrics", metrics_grace),
        }
        self.last_poll: datetime | None = None
        self._parsed: dict[str, CronTrigger | None] = {}

    def _triggers(self, expressions: Iterable[str]) -> list[CronTrigger]:
        out = []
        for expr in expressions:
            if expr not in self._parsed:
                try:
                    self._parsed[expr] = CronTrigger(expr)
                except ValueError as e:
                    logger.error("Cron expression %r ignored: %s", expr, e)
                    self._parsed[expr] = None
            trigger = self._parsed[expr]
            if trigger is not None:
                out.append(trigger)
        return out

    def tick(self, now: datetime, schedules: dict[str, Sequence[str]], *, busy: bool) -> Tick:
        """One poll. ``schedules`` maps a job to its enabled cron expressions
        (empty when switched off); ``busy`` says an agent task is running."""
        now_minute = now.replace(second=0, microsecond=0)
        if self.last_poll is not None and now_minute - self.last_poll > _GAP:
            logger.warning(
                "The scheduler did not poll from %s to %s: the app was stalled or asleep.",
                self.last_poll.strftime("%H:%M"),
                now_minute.strftime("%H:%M"),
            )
        result = Tick()
        for job in self.JOBS:
            slot = self.slots[job]
            triggers = self._triggers(schedules.get(job) or ())
            if not triggers:
                slot.clear()
                continue
            for minute, reason in slot.observe(triggers, self.last_poll, now_minute):
                result.skipped.append(Skip(job, minute, reason))
            waited = slot.waited
            dropped = slot.expire(now_minute)
            if dropped is not None:
                grace = f"{int(slot.grace.total_seconds() // 60)}-minute grace"
                result.skipped.append(
                    Skip(
                        job,
                        dropped,
                        f"another task was still running when its {grace} ran out"
                        if waited
                        else f"the app was stalled or asleep past its {grace}",
                    )
                )
        self.last_poll = now_minute

        # The agents run one at a time; the trading cycle goes first.
        for job in ("cycle", "evolution"):
            slot = self.slots[job]
            if slot.due_at is None:
                continue
            if busy:
                slot.waited = True
                result.deferred.append((job, slot.due_at))
                continue
            waited = slot.waited
            due = slot.take(now_minute)
            if due is None:
                continue
            reason = None
            if due < now_minute:
                reason = (
                    "waited for another task to finish"
                    if waited
                    else "the scheduler did not see its minute (the app was stalled or asleep)"
                )
            result.start.append(Start(job, due, reason))
            busy = True

        # The daily metrics job reads the broker; it never waits for the agents.
        due = self.slots["metrics"].take(now_minute)
        if due is not None:
            result.start.append(Start("metrics", due, None))
        return result
