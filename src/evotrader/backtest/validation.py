"""Statistical validation for backtest results.

Every false positive found during research shared a structure: a result that
looked significant until one specific check was applied. This module applies
those checks automatically.

Real examples this would have caught:

- Pairs trading: 153 pairs tested, best showed Sharpe 0.46 in-sample and
  -0.03 out-of-sample. Caught by :func:`out_of_sample_split` and
  :func:`multiple_testing_penalty`.
- Covered calls: simulation said +17.3%/yr, the live CBOE BuyWrite index
  returned +6.3%. Caught by :func:`compare_to_benchmark`.
- Mega-cap dip buying: naive t-stat 7.05 across 1,840 trades that were really
  848 market days. Caught by :func:`clustered_tstat`.
- "Quiet periods only" showing t=6.58 after excluding 2008 and 2020 — which
  removed the strategy's entire left tail. Caught by :func:`period_stability`.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SIGNIFICANCE_T = 2.0


@dataclass
class ValidationResult:
    """Outcome of one check. ``passed=False`` means the result is not trustworthy."""

    name: str
    passed: bool
    detail: str
    value: float | None = None

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        v = f"  ({self.value:+.2f})" if self.value is not None else ""
        return f"[{mark}] {self.name}{v}: {self.detail}"


@dataclass
class ValidationReport:
    """All checks for one strategy, plus a single trustworthy/not verdict."""

    strategy: str
    checks: list[ValidationResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[ValidationResult]:
        return [c for c in self.checks if not c.passed]

    def __str__(self) -> str:
        head = f"Validation: {self.strategy}"
        body = "\n".join(f"  {c}" for c in self.checks)
        verdict = (
            "VERDICT: result survives every check"
            if self.passed
            else f"VERDICT: NOT trustworthy — {len(self.failures)} check(s) failed"
        )
        return f"{head}\n{'=' * len(head)}\n{body}\n\n{verdict}"


def _t(a: np.ndarray) -> float:
    """Ordinary one-sample t-statistic against zero."""
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) < 3 or a.std(ddof=1) == 0:
        return float("nan")
    return float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a))))


def clustered_tstat(
    pnl: Sequence[float],
    dates: Sequence[datetime],
    min_ratio: float = 1.5,
) -> ValidationResult:
    """Collapse same-day trades into one observation before testing.

    Thirty stocks falling on the same market day is one event, not thirty.
    Treating it as thirty overstates the t-statistic by roughly sqrt(30).
    """
    df = pd.DataFrame({"pnl": list(pnl), "date": pd.to_datetime(list(dates))})
    if df.empty:
        return ValidationResult("clustered t-stat", False, "no trades")

    naive = _t(df["pnl"].to_numpy())
    by_day = df.groupby(df["date"].dt.date)["pnl"].mean()
    clustered = _t(by_day.to_numpy())
    per_day = len(df) / max(len(by_day), 1)

    passed = bool(np.isfinite(clustered) and clustered > SIGNIFICANCE_T)
    detail = (
        f"{len(df)} trades on {len(by_day)} distinct days ({per_day:.1f}/day). "
        f"naive t={naive:.2f} -> clustered t={clustered:.2f}"
    )
    if per_day > min_ratio and np.isfinite(naive) and naive > clustered * 1.3:
        detail += " — naive figure was materially inflated by same-day clustering"
    return ValidationResult("clustered t-stat", passed, detail, clustered)


def out_of_sample_split(
    pnl: Sequence[float],
    dates: Sequence[datetime],
    split: str | datetime,
    max_decay: float = 0.5,
) -> ValidationResult:
    """Compare the first half of history to the second.

    A strategy selected on backtest performance typically shows a strong first
    half and nothing afterwards. That is the signature of fitting noise.
    """
    df = pd.DataFrame({"pnl": list(pnl), "date": pd.to_datetime(list(dates))})
    cut = pd.Timestamp(split)
    ins, oos = df[df["date"] < cut]["pnl"], df[df["date"] >= cut]["pnl"]
    if len(ins) < 20 or len(oos) < 20:
        return ValidationResult(
            "out-of-sample",
            False,
            f"insufficient data either side of {cut.date()} ({len(ins)}/{len(oos)})",
        )

    t_in, t_out = _t(ins.to_numpy()), _t(oos.to_numpy())
    decayed = np.isfinite(t_in) and t_in > 0 and t_out < t_in * max_decay
    passed = bool(np.isfinite(t_out) and t_out > SIGNIFICANCE_T and not decayed)
    detail = f"in-sample t={t_in:.2f} (n={len(ins)}), out-of-sample t={t_out:.2f} (n={len(oos)})"
    if decayed:
        detail += " — edge decayed out of sample, consistent with overfitting"
    return ValidationResult("out-of-sample", passed, detail, t_out)


def multiple_testing_penalty(n_tested: int, best_t: float) -> ValidationResult:
    """Adjust the bar for how many variants were tried.

    Testing 153 pairs and reporting the best is not the same as testing one.
    Uses a Bonferroni-style threshold on the implied normal quantile.
    """
    if n_tested < 1:
        return ValidationResult("multiple testing", False, "n_tested must be >= 1")
    # Two-sided Bonferroni: required |t| ~ quantile of 1 - 0.05/(2n)
    p = 0.05 / (2 * n_tested)
    # Acklam-style rational approximation of the normal inverse CDF, adequate here.
    q = 1 - p
    t_req = math.sqrt(2) * _erfinv(2 * q - 1)
    passed = bool(best_t > t_req)
    detail = (
        f"{n_tested} variants tested; best t={best_t:.2f} "
        f"but the threshold for that many tries is t>{t_req:.2f}"
    )
    if passed:
        detail = (
            f"{n_tested} variants tested; best t={best_t:.2f} clears the adjusted bar t>{t_req:.2f}"
        )
    return ValidationResult("multiple testing", passed, detail, t_req)


def _erfinv(y: float) -> float:
    """Inverse error function (Winitzki approximation), no SciPy dependency."""
    y = max(min(y, 1 - 1e-12), -1 + 1e-12)
    a = 0.147
    ln1 = math.log(1 - y * y)
    term = 2 / (math.pi * a) + ln1 / 2
    return math.copysign(math.sqrt(math.sqrt(term * term - ln1 / a) - term), y)


def period_stability(
    pnl: Sequence[float],
    dates: Sequence[datetime],
    min_positive_fraction: float = 0.6,
) -> ValidationResult:
    """Check the result is not carried by one or two exceptional years.

    Also guards against the reverse error: excluding "unusual" periods to
    improve a result removes the left tail and inflates significance.
    """
    df = pd.DataFrame({"pnl": list(pnl), "date": pd.to_datetime(list(dates))})
    if df.empty:
        return ValidationResult("period stability", False, "no trades")
    by_year = df.groupby(df["date"].dt.year)["pnl"].sum()
    if len(by_year) < 3:
        return ValidationResult("period stability", False, f"only {len(by_year)} years")

    frac = float((by_year > 0).mean())
    total = by_year.sum()
    top_share = float(by_year.max() / total) if total > 0 else float("inf")
    passed = bool(frac >= min_positive_fraction and top_share < 0.5)
    detail = (
        f"{int((by_year > 0).sum())}/{len(by_year)} years positive; "
        f"best year is {top_share:.0%} of total P&L"
    )
    if top_share >= 0.5:
        detail += " — result depends on a single year"
    return ValidationResult("period stability", passed, detail, frac)


def concentration(pnl: Sequence[float], max_top_share: float = 0.5) -> ValidationResult:
    """How much of the profit comes from a handful of trades."""
    a = np.sort(np.asarray(list(pnl), dtype=float))[::-1]
    a = a[np.isfinite(a)]
    if len(a) < 20:
        return ValidationResult("concentration", False, f"only {len(a)} trades")
    total = a.sum()
    if total <= 0:
        return ValidationResult("concentration", False, "strategy is not profitable in aggregate")
    k = max(1, len(a) // 20)  # top 5%
    share = float(a[:k].sum() / total)
    passed = bool(share < max_top_share)
    detail = (
        f"top 5% of trades ({k}) contribute {share:.0%} of profit; median trade {np.median(a):+.3%}"
    )
    return ValidationResult("concentration", passed, detail, share)


def placebo_test(
    real: float,
    placebos: Sequence[float],
    higher_is_better: bool = True,
) -> ValidationResult:
    """Score a real result against the same strategy run on shuffled signals.

    Every other check in this module asks whether a result is significant given
    its own trades. This one asks a blunter question: **could the measurement
    have produced this result with no signal at all?**

    Build ``placebos`` by re-running the identical backtest on circularly
    shifted copies of the signal (see :func:`circular_shift_indices`). A
    circular shift preserves the signal's marginal distribution and its full
    autocorrelation — so trade count, holding period and costs are unchanged —
    while destroying any true relationship with forward returns. Each run is
    therefore one draw from "this signal predicts nothing".

    Measured on this system: the shipped composite returned -1.61% against a
    placebo distribution of mean -0.65%, sd 1.86%, spanning -5.12% to +3.65%.
    The real result sat at the 35th percentile of its own placebos. Every
    parameter setting anyone had tried fell inside that spread, which is why
    4,000 weightings and 25 algorithm versions all failed to hold up: those
    searches were sampling noise and reporting the maximum.

    Always pair this with a positive control — run a cheating signal through
    the same harness and confirm it scores well — otherwise a broken engine
    also produces a null result.
    """
    a = np.asarray(list(placebos), dtype=float)
    a = a[np.isfinite(a)]
    if len(a) < 20:
        return ValidationResult(
            "placebo", False, f"only {len(a)} placebo runs; need 20+ to form a distribution"
        )
    sd = float(a.std(ddof=1))
    mean = float(a.mean())
    pct = float((a < real).mean() * 100.0)
    if not higher_is_better:
        pct = 100.0 - pct
    z = (real - mean) / sd if sd > 0 else math.inf if real > mean else -math.inf
    if not higher_is_better:
        z = -z
    passed = bool(np.isfinite(z) and z > SIGNIFICANCE_T)
    detail = (
        f"real {real:+.2f} vs {len(a)} placebos (mean {mean:+.2f}, sd {sd:.2f}, "
        f"range {a.min():+.2f}..{a.max():+.2f}); {pct:.0f}th percentile, z={z:+.2f}"
    )
    if not passed:
        detail += " — indistinguishable from the same strategy with no signal"
    return ValidationResult("placebo", passed, detail, z)


def circular_shift_indices(n: int, n_shifts: int, seed: int = 0, margin: int = 50) -> list[int]:
    """Offsets for building placebo signals, avoiding near-zero shifts.

    A shift of a few bars leaves the signal still nearly aligned with returns,
    which biases the placebo distribution toward the real result and makes the
    test too easy to pass.
    """
    if n <= 2 * margin + 1:
        raise ValueError(f"series of {n} too short for margin {margin}")
    rng = np.random.default_rng(seed)
    pool = np.arange(margin, n - margin)
    size = min(n_shifts, len(pool))
    return sorted(int(x) for x in rng.choice(pool, size=size, replace=False))


def minimum_detectable_effect(placebo_sd: float, n_sigma: float = SIGNIFICANCE_T) -> float:
    """Smallest effect the measurement can resolve, in the placebo's units.

    On this system placebo sd was 1.86% over two years, so nothing below about
    3.7 pp / 2y — roughly 56% directional accuracy — is distinguishable from
    luck, however good the in-sample statistics look.
    """
    return float(n_sigma * placebo_sd)


def compare_to_benchmark(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    periods_per_year: int = 252,
) -> ValidationResult:
    """Beat the benchmark on risk-adjusted terms, not just on return.

    A strategy returning more with far more volatility has found leverage,
    not edge.
    """
    idx = strategy_returns.index.intersection(benchmark_returns.index)
    s, b = strategy_returns.reindex(idx).fillna(0.0), benchmark_returns.reindex(idx).fillna(0.0)
    if len(s) < 60:
        return ValidationResult("vs benchmark", False, f"only {len(s)} overlapping periods")

    def sharpe(x: pd.Series) -> float:
        return float(x.mean() / x.std() * math.sqrt(periods_per_year)) if x.std() > 0 else 0.0

    ss, bs = sharpe(s), sharpe(b)
    passed = bool(ss > bs)
    detail = f"strategy Sharpe {ss:.2f} vs benchmark {bs:.2f}"
    if not passed:
        detail += " — the benchmark is the better risk-adjusted holding"
    return ValidationResult("vs benchmark", passed, detail, ss - bs)


def survivorship_warning(universe: Sequence[str], as_of: str | None = None) -> ValidationResult:
    """Flag universes built from instruments that still trade today.

    A universe of current tickers excludes everything that failed. For any
    strategy that buys weakness, that bias runs in the favourable direction.
    """
    n = len(universe)
    detail = (
        f"universe of {n} instruments selected as of "
        f"{as_of or 'today'}; delisted names are absent. For dip-buying or "
        f"mean-reversion strategies this biases results upward — validate on "
        f"a point-in-time universe or on instruments that cannot delist"
    )
    return ValidationResult("survivorship", False, detail)


def validate(
    strategy: str,
    pnl: Sequence[float],
    dates: Sequence[datetime],
    *,
    split: str | datetime,
    n_variants_tested: int = 1,
    strategy_returns: pd.Series | None = None,
    benchmark_returns: pd.Series | None = None,
    universe: Sequence[str] | None = None,
) -> ValidationReport:
    """Run every applicable check and return a single verdict."""
    report = ValidationReport(strategy=strategy)
    clustered = clustered_tstat(pnl, dates)
    report.checks.append(clustered)
    report.checks.append(out_of_sample_split(pnl, dates, split))
    report.checks.append(period_stability(pnl, dates))
    report.checks.append(concentration(pnl))
    if n_variants_tested > 1:
        best_t = clustered.value if clustered.value is not None else 0.0
        report.checks.append(multiple_testing_penalty(n_variants_tested, best_t))
    if strategy_returns is not None and benchmark_returns is not None:
        report.checks.append(compare_to_benchmark(strategy_returns, benchmark_returns))
    if universe is not None:
        report.checks.append(survivorship_warning(universe))

    if not report.passed:
        logger.warning(
            "Strategy '%s' failed %d validation check(s): %s",
            strategy,
            len(report.failures),
            ", ".join(c.name for c in report.failures),
        )
    return report
