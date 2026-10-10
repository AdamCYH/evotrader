"""The constitution's account-level limits, applied in code.

The constitution names limits on the whole account: how much may be lost in a
day, in a week, and from the account's peak, how many orders a day, how large
one option premium may be, and a pause after a run of losses. Until 2026-10-03
most of them were words only. The daily-loss limit, the loss-streak pause and
the order count were checked inside ``check_risk_limits``, a tool the Risk
Manager agent is asked to call; nothing checked the weekly loss, the drawdown
from the peak or the premium cap, and the deterministic gate that every order
passes through (``risk_gate``) checked none of them. A model that skipped the
tool skipped the rails.

This module is the one place those rules are evaluated. ``check_risk_limits``
and ``check_option_risk_limits`` report its verdict to the Risk Manager, the
pre-order gate refuses an entry on the same verdict, and ``get_open_positions``
shows the account's standing against each limit on every cycle, so a halt is
visible before an order is attempted.

Every rail here is ENTRY policy. An order that reduces a position, a close, a
trim, a protective stop, is never refused by an account limit: the sell that
stops a loss is the last order a loss limit should block. On an exit a rail
that would have fired is reported as a warning instead.

A limit the account cannot be measured against (no account value, no recorded
peak) is reported as "not evaluated", never silently passed as satisfied, and
never guessed. The caller decides what to do with that; today the order is
allowed and the gap is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from evotrader.models.config import Constitution


@dataclass(frozen=True)
class AccountState:
    """What the rails are measured against, gathered once per check."""

    account_value: float | None = None
    #: "broker" (asked this moment), "recorded" (the metrics job's last value,
    #: used when the broker cannot be asked) or "unavailable".
    value_source: str = "unavailable"
    #: The highest account value on record, including today's.
    peak_value: float | None = None
    #: Realised P&L of closed trades today, and over the trailing seven days.
    today_pnl: float = 0.0
    week_pnl: float = 0.0
    #: The losing streak the pause reads: closing decisions in today's session.
    consecutive_losses: int = 0
    #: The same streak without the session boundary, for reading, not for the pause.
    consecutive_losses_all_time: int | None = None
    last_loss_at: datetime | None = None
    trades_today: int = 0
    #: Problems met while gathering, reported as warnings.
    gaps: tuple[str, ...] = ()

    @property
    def drawdown_pct(self) -> float | None:
        """How far below its peak the account stands, as a fraction (0.12 = 12%)."""
        if not self.account_value or not self.peak_value or self.peak_value <= 0:
            return None
        return max(0.0, (self.peak_value - self.account_value) / self.peak_value)


@dataclass
class RailsVerdict:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: The account's standing against each limit, for agents and the console.
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def entries_blocked(self) -> bool:
        return bool(self.violations)


def daily_loss_verdict(
    today_pnl: float,
    account_value: float | None,
    max_daily_loss_pct: float,
    *,
    is_exit: bool,
) -> tuple[str | None, str | None]:
    """``(violation, warning)`` for the constitution's daily-loss limit.

    The limit is a share of the REAL account. Both risk checks used to test
    ``today_pnl < -(max_daily_loss_pct * max_order_value_usd * 10)``, 5% of
    $100,000, i.e. $5,000 whatever the real balance, so on a small account it
    could never fire.

    Counts closed-trade losses today. Past the limit, new entries are refused
    for the rest of the day; an exit or protective order never is.
    """
    if today_pnl >= 0:
        return None, None
    if not account_value or account_value <= 0:
        return None, (
            f"Daily-loss limit not evaluated: account value unavailable "
            f"(closed-trade P&L today ${today_pnl:.2f})."
        )
    limit = max_daily_loss_pct * account_value
    if -today_pnl < limit:
        return None, None
    detail = (
        f"Daily loss ${-today_pnl:.2f} exceeds the {max_daily_loss_pct:.0%} limit "
        f"(${limit:.2f} of the ${account_value:.2f} account)"
    )
    if is_exit:
        return None, f"{detail}, but this is an exit — permitted so risk can be reduced."
    return f"{detail}: no new entries until the next session. Exits stay allowed.", None


def weekly_loss_verdict(
    week_pnl: float,
    account_value: float | None,
    max_weekly_loss_pct: float,
    *,
    is_exit: bool,
) -> tuple[str | None, str | None]:
    """The same rule over the trailing seven days of closed trades."""
    if week_pnl >= 0:
        return None, None
    if not account_value or account_value <= 0:
        return None, (
            f"Weekly-loss limit not evaluated: account value unavailable "
            f"(closed-trade P&L this week ${week_pnl:.2f})."
        )
    limit = max_weekly_loss_pct * account_value
    if -week_pnl < limit:
        return None, None
    detail = (
        f"Weekly loss ${-week_pnl:.2f} exceeds the {max_weekly_loss_pct:.0%} limit "
        f"(${limit:.2f} of the ${account_value:.2f} account)"
    )
    if is_exit:
        return None, f"{detail}, but this is an exit — permitted so risk can be reduced."
    return (
        f"{detail}: no new entries until the week's losses fall out of the trailing "
        f"seven days. Exits stay allowed.",
        None,
    )


def drawdown_verdict(
    account_value: float | None,
    peak_value: float | None,
    max_drawdown_pct: float,
    *,
    is_exit: bool,
) -> tuple[str | None, str | None]:
    """The constitution's "full stop": the fall from the account's peak.

    A full stop on NEW risk. Nothing here sells a position; the agents keep
    managing exits, and the halt lifts by itself once the account is back
    inside the limit.
    """
    if not account_value or account_value <= 0 or not peak_value or peak_value <= 0:
        return None, (
            "Drawdown limit not evaluated: "
            + ("account value" if not account_value else "the account's peak")
            + " unavailable."
        )
    drawdown = max(0.0, (peak_value - account_value) / peak_value)
    if drawdown < max_drawdown_pct:
        return None, None
    floor = peak_value * (1.0 - max_drawdown_pct)
    detail = (
        f"DRAWDOWN HALT: the account (${account_value:.2f}) is {drawdown:.1%} below its "
        f"peak (${peak_value:.2f}); the constitution's limit is {max_drawdown_pct:.0%}"
    )
    if is_exit:
        return None, f"{detail}, but this is an exit — permitted so risk can be reduced."
    return (
        f"{detail}. No new entries until the account is back above ${floor:.2f}. "
        f"Exits and protective orders stay allowed.",
        None,
    )


def evaluate_account_rails(
    state: AccountState,
    constitution: Constitution,
    *,
    is_exit: bool,
    now: datetime | None = None,
    order_value: float | None = None,
    is_option: bool = False,
) -> RailsVerdict:
    """Every account-level rail, in one verdict.

    ``order_value`` is the order's dollar size (premium x 100 x contracts for
    an option), used for the option premium cap; the per-order dollar cap and
    buying power are checked by the callers, which have the order in hand.
    """
    limits = constitution.risk_limits
    rules = constitution.trading_rules
    breakers = constitution.circuit_breakers
    now = now or datetime.now(UTC)
    verdict = RailsVerdict()

    def take(pair: tuple[str | None, str | None]) -> None:
        violation, warning = pair
        if violation:
            verdict.violations.append(violation)
        if warning:
            verdict.warnings.append(warning)

    take(
        daily_loss_verdict(
            state.today_pnl, state.account_value, limits.max_daily_loss_pct, is_exit=is_exit
        )
    )
    take(
        weekly_loss_verdict(
            state.week_pnl, state.account_value, limits.max_weekly_loss_pct, is_exit=is_exit
        )
    )
    take(
        drawdown_verdict(
            state.account_value, state.peak_value, limits.max_drawdown_pct, is_exit=is_exit
        )
    )

    # The pause after a run of losses holds for `pause_duration_minutes` after
    # the last loss, then lifts even if the streak is unbroken. Session-scoped
    # by the journal: a Friday streak cannot block Monday.
    pause_active = False
    pause_expires_at = None
    if state.consecutive_losses >= breakers.consecutive_losses_pause:
        pause = timedelta(minutes=breakers.pause_duration_minutes)
        pause_active = state.last_loss_at is None or now - state.last_loss_at <= pause
        if state.last_loss_at is not None:
            pause_expires_at = state.last_loss_at + pause
        if pause_active and not is_exit:
            verdict.violations.append(
                f"Circuit breaker: {state.consecutive_losses} consecutive losses "
                f"(limit: {breakers.consecutive_losses_pause}), "
                f"pause expires {breakers.pause_duration_minutes}min after last loss"
            )

    if state.trades_today >= rules.max_trades_per_day and not is_exit:
        verdict.violations.append(
            f"Daily trade limit reached: {state.trades_today}/{rules.max_trades_per_day}"
        )

    premium_limit_usd = None
    if is_option and state.account_value and rules.max_option_premium_pct < 1.0:
        premium_limit_usd = rules.max_option_premium_pct * state.account_value
        if order_value is not None and order_value > premium_limit_usd and not is_exit:
            verdict.violations.append(
                f"Option premium ${order_value:.2f} exceeds {rules.max_option_premium_pct:.0%} of "
                f"the ${state.account_value:.2f} account (${premium_limit_usd:.2f})"
            )

    verdict.warnings.extend(state.gaps)

    value = state.account_value
    verdict.report = {
        "account_value": round(value, 2) if value else None,
        "value_source": state.value_source,
        "peak_value": round(state.peak_value, 2) if state.peak_value else None,
        "drawdown_pct": (
            round(state.drawdown_pct * 100, 2) if state.drawdown_pct is not None else None
        ),
        "max_drawdown_pct": round(limits.max_drawdown_pct * 100, 2),
        "drawdown_halt": (
            state.drawdown_pct is not None and state.drawdown_pct >= limits.max_drawdown_pct
        ),
        "today_pnl": round(state.today_pnl, 2),
        "daily_loss_limit_usd": round(limits.max_daily_loss_pct * value, 2) if value else None,
        "week_pnl": round(state.week_pnl, 2),
        "weekly_loss_limit_usd": (round(limits.max_weekly_loss_pct * value, 2) if value else None),
        # The pause reads today's session only, by design: a Friday streak does
        # not block Monday. The run without that boundary is beside it, so a
        # session streak of 0 after a losing day reads as what it is.
        "consecutive_losses": state.consecutive_losses,
        "streak_scope": "session",
        "consecutive_losses_all_time": state.consecutive_losses_all_time,
        "loss_streak_limit": breakers.consecutive_losses_pause,
        "loss_streak_pause_active": pause_active,
        # Whether a streak at the limit still binds, and until when: the pause
        # lifts this long after the last loss even if the streak stands.
        "last_loss_at": state.last_loss_at.isoformat() if state.last_loss_at else None,
        "pause_expires_at": pause_expires_at.isoformat() if pause_expires_at else None,
        "trades_today": state.trades_today,
        "max_trades_per_day": rules.max_trades_per_day,
        "option_premium_limit_usd": (
            round(premium_limit_usd, 2) if premium_limit_usd is not None else None
        ),
        # What an ENTRY would meet right now. Exits are never blocked.
        "entries_blocked": verdict.entries_blocked if not is_exit else None,
    }
    return verdict
