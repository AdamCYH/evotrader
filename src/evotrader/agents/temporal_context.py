"""Dynamic temporal context for agent system instructions.

Builds a short markdown block with current Eastern time, market session,
remaining trading cycles, and the cron expression.  Designed to be appended
as the **last** section of an agent's system instruction so that the large
static prefix remains stable for Gemini prefix caching.

Usage in factory.py::

    LlmAgent(
        static_instruction=_load_instructions("orchestrator", config),
        instruction=lambda ctx: build_temporal_context(config.settings.schedule),
        ...
    )
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from evotrader.cron import CronTrigger
from evotrader.models.config import ScheduleConfig
from evotrader.tools.market_hours import (
    ET,
    MarketSession,
    _resolve_now,
    after_hours_close,
    get_current_session,
    is_early_close,
    regular_close,
)

logger = logging.getLogger(__name__)


def build_temporal_context(schedule: ScheduleConfig) -> str:
    """Build a dynamic temporal context block for injection into agent instructions.

    Returns a markdown string with:
    - Current Eastern time (with explicit timezone marker)
    - Current market session
    - Cron expression (raw, for model reasoning)
    - Remaining cycles today (pre-computed with times)
    - Current cycle number of total

    Args:
        schedule: The schedule configuration from app settings.

    Returns:
        Markdown string to append to the system instruction.
    """
    now_et = _resolve_now().astimezone(ET)

    # Current time — human-readable with explicit ET marker
    time_str = now_et.strftime("%A, %B %d, %Y, %I:%M %p ET")

    # Market session
    session_info = get_current_session(now_et)
    session_label = _session_label(session_info, now_et.date())

    # Cron schedule and remaining cycles
    cron_expr = schedule.cycle_cron
    cron_line = ""
    cycles_line = ""
    progress_line = ""

    if cron_expr:
        cron_line = f"- **Cycle schedule (cron)**: `{cron_expr}`"

        try:
            remaining, total_today = _compute_remaining_cycles(cron_expr, now_et)
            current_cycle = total_today - len(remaining)

            if remaining:
                # Format the remaining times concisely
                time_list = ", ".join(t.strftime("%-I:%M %p") for t in remaining[:8])
                if len(remaining) > 8:
                    time_list += f", … ({len(remaining) - 8} more)"
                cycles_line = f"- **Remaining cycles today**: {len(remaining)} (next: {time_list})"
            else:
                cycles_line = (
                    "- **Remaining cycles today**: 0 (last cycle completed or market closed)"
                )

            if total_today > 0:
                progress_line = f"- **This is cycle {current_cycle} of {total_today} today**"
        except Exception as e:
            logger.debug("Could not compute remaining cycles: %s", e)
            cycles_line = "- **Remaining cycles today**: unable to compute"
    else:
        cron_line = "- **Cycle schedule**: Manual trigger only (on-demand)"

    lines = [
        "",
        "## Current Temporal Context",
        f"- **Current time**: {time_str}",
        f"- **Market session**: {session_label}",
        cron_line,
    ]
    if cycles_line:
        lines.append(cycles_line)
    if progress_line:
        lines.append(progress_line)

    return "\n".join(lines)


def _clock(t: dt_time) -> str:
    """4:00 PM style, as the labels write times."""
    return datetime.combine(date.min, t).strftime("%-I:%M %p")


def _session_label(session: MarketSession, day: date) -> str:
    """Convert a MarketSession enum to a human-readable label with that day's hours.

    The closes come from the market calendar, so on NYSE's 1:00 pm early-close
    days the agents are not told the session runs until 4:00 pm.
    """
    close, extended_close = _clock(regular_close(day)), _clock(after_hours_close(day))
    early = ", early close" if is_early_close(day) else ""
    labels = {
        MarketSession.PRE_MARKET: "Pre-market (4:00 AM – 9:30 AM ET)",
        MarketSession.REGULAR: f"Regular hours (9:30 AM – {close} ET{early})",
        MarketSession.AFTER_HOURS: f"After-hours ({close} – {extended_close} ET{early})",
        MarketSession.OVERNIGHT: "Overnight (market closed)",
        MarketSession.CLOSED: "Market closed",
    }
    return labels.get(session, str(session))


def _compute_remaining_cycles(
    cron_expr: str,
    now_et: datetime,
) -> tuple[list[datetime], int]:
    """Compute remaining cycle times for today and total cycles today.

    Walks the cron schedule forward from the start of today to find all
    matching times, then partitions them into past and future.

    Args:
        cron_expr: 5-field cron expression.
        now_et: Current datetime in Eastern time.

    Returns:
        Tuple of (remaining_times, total_today) where remaining_times
        are the cycle times still ahead today, and total_today is the
        total number of cycles scheduled for today.
    """
    trigger = CronTrigger(cron_expr)

    # Start of today in Eastern
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    # End of today
    today_end = today_start + timedelta(days=1)

    # Walk the cron schedule through today to find all matching times
    all_today: list[datetime] = []
    cursor = today_start - timedelta(minutes=1)  # so next_run starts from 00:00

    for _ in range(1440):  # max minutes in a day
        next_time = trigger.next_run(cursor)
        next_et = next_time.astimezone(ET)

        if next_et >= today_end:
            break

        all_today.append(next_et)
        cursor = next_time

    # Partition into remaining (strictly after now)
    remaining = [t for t in all_today if t > now_et]

    return remaining, len(all_today)
