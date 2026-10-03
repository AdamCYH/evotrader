"""The constitution's account-level limits, applied in code.

Until 2026-10-03 the weekly-loss limit and the drawdown "full stop" were words
in constitution.yaml with no code behind them, and the daily-loss limit, the
loss-streak pause and the order count lived only in a tool the risk-manager
agent is asked to call. ``callbacks.account_rails`` is now the one verdict all
three callers apply: the two risk tools, the pre-order gate and the positions
report. Made-up account: $5,000, peak $6,000.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evotrader.callbacks.account_rails import (
    AccountState,
    daily_loss_verdict,
    drawdown_verdict,
    evaluate_account_rails,
    weekly_loss_verdict,
)
from evotrader.models.config import Constitution

NOW = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)


def _constitution(rules: dict | None = None, **limits) -> Constitution:
    """Built the way the app builds it, from plain values (8 means 8%)."""
    values = {
        "max_daily_loss_pct": 8,
        "max_weekly_loss_pct": 15,
        "max_drawdown_pct": 25,
        "max_order_value_usd": 100_000.0,
    }
    values.update(limits)
    trading = {"allowed_tickers": ["T"], "max_trades_per_day": 16, "max_option_premium_pct": 30}
    trading.update(rules or {})
    return Constitution.model_validate(
        {
            "risk_limits": values,
            "trading_rules": trading,
            "circuit_breakers": {"consecutive_losses_pause": 5, "pause_duration_minutes": 60},
        }
    )


def _state(**kw) -> AccountState:
    base = {"account_value": 5000.0, "value_source": "broker", "peak_value": 6000.0}
    base.update(kw)
    return AccountState(**base)


class TestDrawdown:
    def test_inside_the_limit_nothing_happens(self) -> None:
        assert drawdown_verdict(5000.0, 6000.0, 0.25, is_exit=False) == (None, None)

    def test_at_the_limit_new_entries_stop(self) -> None:
        violation, warning = drawdown_verdict(4500.0, 6000.0, 0.25, is_exit=False)
        assert violation is not None and warning is None
        assert "DRAWDOWN HALT" in violation and "25.0%" in violation
        assert "$4500.00" in violation, "the floor the account must get back above"

    def test_exits_are_never_blocked(self) -> None:
        violation, warning = drawdown_verdict(4000.0, 6000.0, 0.25, is_exit=True)
        assert violation is None
        assert warning is not None and "exit" in warning.lower()

    def test_an_unknown_peak_is_said_not_guessed(self) -> None:
        violation, warning = drawdown_verdict(4000.0, None, 0.25, is_exit=False)
        assert violation is None
        assert warning is not None and "not evaluated" in warning and "peak" in warning

    def test_a_new_high_is_no_drawdown(self) -> None:
        assert _state(account_value=6500.0, peak_value=6000.0).drawdown_pct == 0.0


class TestWeeklyLoss:
    def test_past_the_limit_new_entries_stop(self) -> None:
        violation, _warning = weekly_loss_verdict(-800.0, 5000.0, 0.15, is_exit=False)
        assert violation is not None and "Weekly loss $800.00" in violation
        assert "$750.00" in violation

    def test_inside_the_limit_or_in_profit_nothing_happens(self) -> None:
        assert weekly_loss_verdict(-700.0, 5000.0, 0.15, is_exit=False) == (None, None)
        assert weekly_loss_verdict(300.0, 5000.0, 0.15, is_exit=False) == (None, None)

    def test_exits_pass_with_a_warning(self) -> None:
        violation, warning = weekly_loss_verdict(-800.0, 5000.0, 0.15, is_exit=True)
        assert violation is None and warning is not None


class TestDailyLossIsUnchanged:
    """The 2026-09-25 rule, now shared: same words, same numbers."""

    def test_a_loss_past_the_limit_blocks_new_entries(self) -> None:
        violation, warning = daily_loss_verdict(-260.0, 5000.0, 0.05, is_exit=False)
        assert violation is not None and warning is None
        assert "$260.00" in violation and "$250.00" in violation

    def test_the_tools_module_still_exposes_it(self) -> None:
        from evotrader.agents.tools import _daily_loss_verdict

        assert _daily_loss_verdict(-260.0, 5000.0, 0.05, is_exit=False) == daily_loss_verdict(
            -260.0, 5000.0, 0.05, is_exit=False
        )


class TestTheOneVerdict:
    def test_a_healthy_account_passes_with_a_full_report(self) -> None:
        verdict = evaluate_account_rails(_state(), _constitution(), is_exit=False, now=NOW)
        assert verdict.violations == [] and verdict.warnings == []
        report = verdict.report
        assert report["drawdown_pct"] == pytest.approx(16.67, abs=0.01)
        assert report["drawdown_halt"] is False
        assert report["daily_loss_limit_usd"] == pytest.approx(400.0)
        assert report["weekly_loss_limit_usd"] == pytest.approx(750.0)
        assert report["entries_blocked"] is False

    def test_every_rail_can_speak_at_once(self) -> None:
        state = _state(
            account_value=4400.0,
            today_pnl=-400.0,
            week_pnl=-700.0,
            consecutive_losses=5,
            last_loss_at=NOW - timedelta(minutes=10),
            trades_today=16,
        )
        verdict = evaluate_account_rails(state, _constitution(), is_exit=False, now=NOW)
        joined = "\n".join(verdict.violations)
        for text in (
            "Daily loss",
            "Weekly loss",
            "DRAWDOWN HALT",
            "Circuit breaker",
            "trade limit",
        ):
            assert text in joined, joined
        assert verdict.report["entries_blocked"] is True
        assert verdict.report["loss_streak_pause_active"] is True

    def test_an_exit_is_never_blocked_by_any_of_them(self) -> None:
        state = _state(
            account_value=4400.0,
            today_pnl=-400.0,
            week_pnl=-700.0,
            consecutive_losses=5,
            last_loss_at=NOW - timedelta(minutes=10),
            trades_today=16,
        )
        verdict = evaluate_account_rails(state, _constitution(), is_exit=True, now=NOW)
        assert verdict.violations == []
        assert len(verdict.warnings) == 3, verdict.warnings  # the three loss limits explain
        assert verdict.report["entries_blocked"] is None

    def test_the_loss_streak_pause_lifts_after_the_pause(self) -> None:
        state = _state(consecutive_losses=5, last_loss_at=NOW - timedelta(minutes=61))
        verdict = evaluate_account_rails(state, _constitution(), is_exit=False, now=NOW)
        assert verdict.violations == []
        assert verdict.report["loss_streak_pause_active"] is False

    def test_a_streak_with_no_timestamp_pauses(self) -> None:
        state = _state(consecutive_losses=5, last_loss_at=None)
        verdict = evaluate_account_rails(state, _constitution(), is_exit=False, now=NOW)
        assert any("Circuit breaker" in v for v in verdict.violations)

    def test_the_option_premium_cap_is_a_share_of_the_account(self) -> None:
        constitution = _constitution()
        too_big = evaluate_account_rails(
            _state(), constitution, is_exit=False, now=NOW, order_value=1600.0, is_option=True
        )
        assert any("Option premium $1600.00 exceeds 30%" in v for v in too_big.violations)
        assert too_big.report["option_premium_limit_usd"] == pytest.approx(1500.0)
        fits = evaluate_account_rails(
            _state(), constitution, is_exit=False, now=NOW, order_value=1400.0, is_option=True
        )
        assert fits.violations == []
        shares = evaluate_account_rails(
            _state(), constitution, is_exit=False, now=NOW, order_value=1600.0, is_option=False
        )
        assert shares.violations == [], "the premium cap is for options"

    def test_a_cap_of_one_hundred_percent_is_off(self) -> None:
        constitution = _constitution(rules={"max_option_premium_pct": 100})
        verdict = evaluate_account_rails(
            _state(), constitution, is_exit=False, now=NOW, order_value=4900.0, is_option=True
        )
        assert verdict.violations == []
        assert verdict.report["option_premium_limit_usd"] is None

    def test_what_cannot_be_measured_is_reported_not_passed_silently(self) -> None:
        state = AccountState(today_pnl=-900.0, week_pnl=-900.0, gaps=("Journal unavailable: x",))
        verdict = evaluate_account_rails(state, _constitution(), is_exit=False, now=NOW)
        assert verdict.violations == []
        joined = "\n".join(verdict.warnings)
        assert "Daily-loss limit not evaluated" in joined
        assert "Weekly-loss limit not evaluated" in joined
        assert "Drawdown limit not evaluated" in joined
        assert "Journal unavailable: x" in joined
        assert verdict.report["account_value"] is None
