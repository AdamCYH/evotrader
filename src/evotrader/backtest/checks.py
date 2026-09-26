"""Run the statistical checks in :mod:`validation` on a finished backtest.

``validation`` holds the checks; this module feeds them a real run. It is what
``runner --validate`` calls, so a result is judged by the same code whether a
person or an agent asks.

Beyond the per-trade checks it runs two controls on the same bars and engine:

- **Placebo** — the version's own signal, circularly shifted against prices,
  replayed many times. That is the spread of results this measurement produces
  when the signal predicts nothing. The real result has to stand clear of it.
- **Positive control** — a signal that cheats by reading the next bar. It must
  stand clear of the same placebo spread; if it cannot, the harness cannot see
  an edge on this instrument and window (a price too high to buy one whole
  unit, say), and every other number in the report is meaningless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from evotrader.backtest.engine import BacktestEngine
from evotrader.backtest.validation import (
    ValidationReport,
    ValidationResult,
    circular_shift_indices,
    minimum_detectable_effect,
    placebo_test,
    validate,
)
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal

if TYPE_CHECKING:
    from evotrader.backtest.runner import RiskParams

logger = logging.getLogger(__name__)

DEFAULT_PLACEBOS = 40
PLACEBO_MARGIN_BARS = 50


@dataclass
class BacktestVerdict:
    """The validation report for one version, plus the numbers behind the controls."""

    version: str
    report: ValidationReport
    real_return_pct: float
    placebo_returns: list[float]
    control_return_pct: float | None
    n_variants_tested: int
    split: datetime

    @property
    def passed(self) -> bool:
        return self.report.passed


def replay(
    snapshots: list[MarketSnapshot],
    signals: list[AlgoSignal],
    risk: RiskParams,
    ticker: str = "",
) -> BacktestEngine:
    """Step a fresh engine through ``snapshots`` with the given per-bar signals."""
    engine = risk.to_engine(ticker=ticker)
    for snapshot, signal in zip(snapshots, signals, strict=True):
        engine.step(snapshot, signal)
    return engine


def total_return_pct(engine: BacktestEngine) -> float:
    """Mark-to-market return over the run, as the tear sheet computes it."""
    if not engine.equity_curve:
        return 0.0
    final = float(engine.equity_curve[-1]["equity"])
    return (final - engine.initial_cash) / engine.initial_cash * 100.0


def shift(signals: list[AlgoSignal], offset: int) -> list[AlgoSignal]:
    """The signal series rotated by ``offset`` bars, wrapping at the end."""
    n = len(signals)
    return [signals[(i + offset) % n] for i in range(n)]


def cheating_signals(snapshots: list[MarketSnapshot], next_bar_open_fill: bool) -> list[AlgoSignal]:
    """A signal that knows the next move: the positive control.

    With next-bar fills, a decision on bar i is filled at bar i+1's open and a
    reversal decided on bar i+1 fills at bar i+2's open, so the move that
    decision earns is open[i+2] - open[i+1]. With same-bar fills it is
    close[i+1] - close[i].
    """
    ohlc = [BacktestEngine._bar_ohlc(s) for s in snapshots]
    if next_bar_open_fill:
        px = [bar[0] for bar in ohlc]
        lead = 1
    else:
        px = [bar[3] for bar in ohlc]
        lead = 0
    out = []
    n = len(snapshots)
    for i in range(n):
        a, b = i + lead, i + lead + 1
        move = px[b] - px[a] if b < n else 0.0
        value = 1.0 if move > 0 else -1.0 if move < 0 else 0.0
        out.append(
            AlgoSignal(
                name="positive_control",
                value=value,
                weight=1.0,
                metadata={"authoring_signal": "positive_control"},
            )
        )
    return out


def _daily_returns(timestamps: list[datetime], values: list[float]) -> pd.Series:
    index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    s = pd.Series(values, index=index)
    return s.groupby(index.normalize()).last().pct_change().dropna()


def check_version(
    version: str,
    snapshots: list[MarketSnapshot],
    signals: list[AlgoSignal],
    engine: BacktestEngine,
    risk: RiskParams,
    *,
    ticker: str = "",
    n_variants_tested: int = 1,
    n_placebos: int = DEFAULT_PLACEBOS,
    split: datetime | None = None,
    seed: int = 0,
) -> BacktestVerdict:
    """Every check for one finished run: its trades, its benchmark, and both controls."""
    if split is None:
        first, last = snapshots[0].timestamp, snapshots[-1].timestamp
        split = first + (last - first) / 2

    # Per-trade return as a fraction: the t-tests don't care about scale, and
    # concentration reports the median trade as a percentage.
    pnl: list[float] = []
    dates: list[datetime] = []
    for t in engine.closed_trades:
        if t.exit_time is not None:
            pnl.append(t.return_pct / 100.0)
            dates.append(t.exit_time)

    strategy_daily = _daily_returns(
        [datetime.fromisoformat(str(r["timestamp"])) for r in engine.equity_curve],
        [float(r["equity"]) for r in engine.equity_curve],
    )
    benchmark_daily = _daily_returns(
        [s.timestamp for s in snapshots], [s.quote.last for s in snapshots]
    )

    report = validate(
        version,
        pnl,
        dates,
        split=split,
        n_variants_tested=n_variants_tested,
        strategy_returns=strategy_daily,
        benchmark_returns=benchmark_daily,
        log_failures=False,
    )

    real = total_return_pct(engine)
    placebos: list[float] = []
    control: float | None = None
    n = len(snapshots)
    if n <= 2 * PLACEBO_MARGIN_BARS + 1:
        report.checks.insert(
            0,
            ValidationResult(
                "positive control",
                False,
                f"{n} bars is too short to build placebos; nothing here can be judged",
            ),
        )
        report.checks.append(
            ValidationResult("placebo", False, f"{n} bars is too short to build placebos")
        )
    else:
        offsets = circular_shift_indices(n, n_placebos, seed=seed, margin=PLACEBO_MARGIN_BARS)
        placebos = [
            total_return_pct(replay(snapshots, shift(signals, off), risk, ticker))
            for off in offsets
        ]
        control = total_return_pct(
            replay(snapshots, cheating_signals(snapshots, risk.next_bar_open_fill), risk, ticker)
        )
        sd = float(np.std(placebos, ddof=1)) if len(placebos) > 1 else 0.0
        floor = minimum_detectable_effect(sd)

        ctrl = placebo_test(control, placebos)
        ctrl.name = "positive control"
        if ctrl.passed:
            ctrl.detail = (
                f"a signal that cheats by reading the next bar returns {control:+.2f}%, "
                f"well clear of the placebo spread, so this harness can see an edge here"
            )
        else:
            ctrl.detail = (
                f"even a signal that cheats by reading the next bar returns only "
                f"{control:+.2f}% (placebo mean {np.mean(placebos):+.2f}%, sd {sd:.2f}%). "
                f"The harness cannot see an edge on this instrument and window, so no "
                f"other check here means anything. Common causes: too few bars, or a "
                f"price too high to buy one whole unit (try --fractional)"
            )
        report.checks.insert(0, ctrl)

        plac = placebo_test(real, placebos)
        plac.detail += f". Smallest return this run can tell apart from luck: ±{floor:.2f}%"
        report.checks.append(plac)

    if not report.passed:
        logger.warning(
            "Version '%s' failed %d validation check(s): %s",
            version,
            len(report.failures),
            ", ".join(c.name for c in report.failures),
        )
    return BacktestVerdict(
        version=version,
        report=report,
        real_return_pct=real,
        placebo_returns=placebos,
        control_return_pct=control,
        n_variants_tested=n_variants_tested,
        split=split,
    )


_PLAIN = {
    "positive control": "Can this harness see an edge at all?",
    "clustered t-stat": "Is the average trade profit more than luck, counting each day once?",
    "out-of-sample": "Does it still work in the second half of the window?",
    "period stability": "Is the profit spread across years, not one lucky year?",
    "concentration": "Is the profit spread across trades, not a few big wins?",
    "multiple testing": "Does it clear the higher bar for having tried several versions?",
    "vs benchmark": "Is it a better risk-adjusted holding than the instrument itself?",
    "placebo": "Does it beat the same signal shifted so it predicts nothing?",
}


def format_verdict_markdown(verdict: BacktestVerdict) -> str:
    """The verdict as a report section: one row per check, then the overall answer."""
    lines = [
        f"## Validation: `{verdict.version}`",
        "",
        "Whether this result is evidence of an edge or could have come from luck. "
        "Treat any FAIL as disqualifying, not as a starting point for debate.",
        "",
        f"Split for the out-of-sample check: {verdict.split:%Y-%m-%d}. "
        f"Versions counted as tried: {verdict.n_variants_tested}.",
        "",
        "| Check | Question | Result | Detail |",
        "|---|---|---|---|",
    ]
    for c in verdict.report.checks:
        mark = "PASS" if c.passed else "**FAIL**"
        detail = c.detail.replace("|", "/")
        lines.append(f"| {c.name} | {_PLAIN.get(c.name, '')} | {mark} | {detail} |")
    lines.append("")
    if verdict.passed:
        lines.append(
            "**Verdict: survives every check.** Before believing it, price what it "
            "costs to trade live, and run it through a practice cycle."
        )
    else:
        failed = ", ".join(c.name for c in verdict.report.failures)
        lines.append(
            f"**Verdict: NOT trustworthy** ({len(verdict.report.failures)} failed: {failed}). "
            "The return above is not evidence of an edge."
        )
    return "\n".join(lines)
