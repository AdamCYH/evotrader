"""Per-source signal attribution.

The Evolution Agent can only improve what it can measure. Scoring a blended
composite tells you the blend was wrong; it cannot tell you which ingredient
was wrong, so every proposed fix is a guess.

This module scores each witness — algorithm, news, LLM reasoning — separately
against realised forward returns, and measures whether their *agreement* adds
anything beyond the individual sources. That last number is the one that
decides whether a multi-source design is worth running at all: if agreement
scores no better than the best single source, the sources are not independent
and the ensemble is decoration.

Statistics here are deliberately conservative. Same-cycle observations across
correlated instruments are one event, not many, so :func:`score_source`
clusters by timestamp the way :mod:`evotrader.backtest.validation` does.
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
class SourceScore:
    """How well one witness predicted forward returns."""

    source: str
    n_calls: int
    n_events: int
    hit_rate: float
    mean_forward_return: float
    information_coefficient: float
    t_stat: float
    #: Always calling the majority direction of THIS sample. For a channel that
    #: only calls one side, that majority is its own call whenever it is right
    #: more than half the time, so ``edge_over_baseline`` reads 0.0 by
    #: construction for exactly the channels that choose their moments; see
    #: ``expected_hit_unconditional``.
    baseline_hit_rate: float
    #: Calls by side: a one-sided record should read as one.
    n_long: int = 0
    n_short: int = 0
    #: The hit rate the same calls would score if each cycle rose with the
    #: window's unconditional up-share: that share for a long call, one minus it
    #: for a short call. NaN without a window up-share.
    expected_hit_unconditional: float = float("nan")
    #: Two-sided exact probability of a hit count no more likely than this
    #: one, against that expectation. Unclustered: read it next to
    #: ``n_events``, never alone.
    binomial_p_unconditional: float = float("nan")

    @property
    def is_informative(self) -> bool:
        """True when the record clears significance after clustering, either way.

        ``direction`` says which way. A witness that is reliably wrong
        (t <= -2) is information too, the candidate for inversion, not chance;
        under |t| 2 the honest reading is "not distinguishable from chance",
        never "slightly better".
        """
        return bool(np.isfinite(self.t_stat) and abs(self.t_stat) >= SIGNIFICANCE_T)

    @property
    def direction(self) -> str | None:
        """``predicts`` when the calls' t is above 0, ``anti_predicts`` below, else None."""
        if not np.isfinite(self.t_stat) or self.t_stat == 0:
            return None
        return "predicts" if self.t_stat > 0 else "anti_predicts"

    @property
    def edge_over_baseline(self) -> float:
        """Hit rate above what always guessing the majority direction would give."""
        return self.hit_rate - self.baseline_hit_rate

    def __str__(self) -> str:
        mark = (
            f"informative ({self.direction})"
            if self.is_informative
            else "not distinguishable from chance"
        )
        return (
            f"{self.source:<24} n={self.n_calls:<5} events={self.n_events:<5} "
            f"hit={self.hit_rate:.1%} (base {self.baseline_hit_rate:.1%})  "
            f"IC={self.information_coefficient:+.3f}  t={self.t_stat:+.2f}  → {mark}"
        )


@dataclass
class AttributionReport:
    """Per-source scores plus the verdict on whether combining them helps."""

    sources: list[SourceScore] = field(default_factory=list)
    agreement: SourceScore | None = None
    best_single_t: float = 0.0

    @property
    def agreement_adds_value(self) -> bool:
        """True when agreement beats every individual source on significance.

        If this is False the witnesses are correlated — they are re-reading the
        same information — and the ensemble is not doing what it was built for.
        """
        if self.agreement is None:
            return False
        return bool(self.agreement.t_stat > self.best_single_t)

    def __str__(self) -> str:
        lines = ["Signal attribution", "=" * 18]
        lines += [f"  {s}" for s in self.sources]
        if self.agreement is not None:
            lines.append("")
            lines.append(f"  {self.agreement}")
            verdict = (
                "agreement beats every single source — the witnesses are adding "
                "independent information"
                if self.agreement_adds_value
                else "agreement does NOT beat the best single source — the witnesses "
                "are correlated and the ensemble is not earning its complexity"
            )
            lines.append(f"\n  VERDICT: {verdict}")
        return "\n".join(lines)


def _cluster_t(values: np.ndarray, stamps: Sequence[datetime]) -> tuple[float, int]:
    """t-statistic on per-timestamp means, plus the number of distinct events."""
    df = pd.DataFrame({"v": values, "ts": pd.to_datetime(list(stamps))})
    by_event = df.groupby(df["ts"].dt.floor("D"))["v"].mean()
    n = len(by_event)
    if n < 3 or by_event.std(ddof=1) == 0:
        return float("nan"), n
    t = by_event.mean() / (by_event.std(ddof=1) / math.sqrt(n))
    return float(t), n


def _two_sided_tail(hits: int, rates: np.ndarray) -> float:
    """Two-sided exact probability of a hit count no more likely than ``hits``.

    Call ``i`` is right with probability ``rates[i]``; the count's distribution
    (Poisson-binomial) is computed exactly, and every count whose probability
    is at most the observed one's is summed: the usual exact test, which for
    one-sided calls is the binomial test. Doubling the smaller tail instead
    overstates it on a skewed distribution (7 of 8 at 0.35: 0.007 against
    0.004).
    """
    mass = np.ones(1)
    for p in rates:
        mass = np.concatenate((mass * (1.0 - p), [0.0])) + np.concatenate(([0.0], mass * p))
    return float(min(1.0, mass[mass <= mass[hits] * (1 + 1e-7)].sum()))


def score_source(
    source: str,
    directions: Sequence[float],
    forward_returns: Sequence[float],
    timestamps: Sequence[datetime],
    window_up_rate: float | None = None,
) -> SourceScore:
    """Score one witness's directional calls against what actually happened.

    Args:
        source: Witness name, e.g. ``"algo"``, ``"news"``, ``"llm"``.
        directions: Signed calls. Sign is the direction; magnitude is
            conviction. Zeros are abstentions and are excluded.
        forward_returns: Realised return over the decision horizon, same order.
        timestamps: Decision times, used to cluster same-day calls.
        window_up_rate: Share of the window's cycles whose price rose, over
            every cycle, called or not. With it, the score says what the same
            calls would hit by chance (``expected_hit_unconditional``) and how
            unlikely the actual count is (``binomial_p_unconditional``).
    """
    d = np.asarray(list(directions), dtype=float)
    f = np.asarray(list(forward_returns), dtype=float)
    ts = list(timestamps)
    if not (len(d) == len(f) == len(ts)):
        raise ValueError(
            f"length mismatch: directions={len(d)}, returns={len(f)}, timestamps={len(ts)}"
        )

    live = np.isfinite(d) & np.isfinite(f) & (np.abs(d) > 1e-9)
    d, f = d[live], f[live]
    ts = [t for t, keep in zip(ts, live, strict=True) if keep]

    if len(d) < 3:
        return SourceScore(source, len(d), 0, 0.0, 0.0, float("nan"), float("nan"), 0.0)

    signed = np.sign(d) * f  # positive when the call was right
    hit = float((signed > 0).mean())
    # Baseline: always calling the majority direction of this sample.
    up_rate = float((f > 0).mean())
    baseline = max(up_rate, 1 - up_rate)

    ic = float(np.corrcoef(d, f)[0, 1]) if d.std() > 0 and f.std() > 0 else float("nan")
    t, n_events = _cluster_t(signed, ts)

    expected = p_value = float("nan")
    if window_up_rate is not None and 0.0 < window_up_rate < 1.0:
        rates = np.where(d > 0, window_up_rate, 1.0 - window_up_rate)
        expected = float(rates.mean())
        p_value = _two_sided_tail(int((signed > 0).sum()), rates)

    return SourceScore(
        source=source,
        n_calls=len(d),
        n_events=n_events,
        hit_rate=hit,
        mean_forward_return=float(signed.mean()),
        information_coefficient=ic,
        t_stat=t,
        baseline_hit_rate=baseline,
        n_long=int((d > 0).sum()),
        n_short=int((d < 0).sum()),
        expected_hit_unconditional=expected,
        binomial_p_unconditional=p_value,
    )


def score_agreement(
    source_directions: dict[str, Sequence[float]],
    forward_returns: Sequence[float],
    timestamps: Sequence[datetime],
    min_agreeing: int = 2,
) -> SourceScore:
    """Score the cycles where at least ``min_agreeing`` witnesses pointed the same way.

    This is the test of the ensemble premise. Weak independent signals should
    combine into a stronger one; weak *correlated* signals should not.
    """
    names = list(source_directions)
    if not names:
        raise ValueError("source_directions is empty")
    mat = np.array([np.asarray(list(source_directions[n]), dtype=float) for n in names])
    f = np.asarray(list(forward_returns), dtype=float)

    signs = np.sign(np.nan_to_num(mat))
    up = (signs > 0).sum(axis=0)
    down = (signs < 0).sum(axis=0)
    # Consensus only where enough witnesses agree AND none point the other way.
    consensus = np.where(
        (up >= min_agreeing) & (down == 0),
        1.0,
        np.where((down >= min_agreeing) & (up == 0), -1.0, 0.0),
    )
    return score_source(
        f"agreement (>={min_agreeing})",
        consensus.tolist(),
        f.tolist(),
        timestamps,
    )


def attribute(
    source_directions: dict[str, Sequence[float]],
    forward_returns: Sequence[float],
    timestamps: Sequence[datetime],
    min_agreeing: int = 2,
) -> AttributionReport:
    """Score every witness and their agreement. See :class:`AttributionReport`."""
    report = AttributionReport()
    for name, dirs in source_directions.items():
        report.sources.append(score_source(name, dirs, forward_returns, timestamps))

    finite = [s.t_stat for s in report.sources if np.isfinite(s.t_stat)]
    report.best_single_t = max(finite) if finite else 0.0

    if len(source_directions) >= 2:
        report.agreement = score_agreement(
            source_directions, forward_returns, timestamps, min_agreeing
        )

    if report.agreement is not None and not report.agreement_adds_value:
        logger.warning(
            "Agreement (t=%.2f) does not beat the best single source (t=%.2f) — "
            "witnesses appear correlated rather than independent.",
            report.agreement.t_stat,
            report.best_single_t,
        )
    return report


@dataclass
class CalibrationBucket:
    """Stated conviction versus realised hit rate, for one conviction band."""

    low: float
    high: float
    n: int
    stated: float
    realized: float

    @property
    def gap(self) -> float:
        """Positive means overconfident: claimed more certainty than delivered."""
        return self.stated - self.realized

    def __str__(self) -> str:
        if self.n == 0:
            return f"  {self.low:.1f}-{self.high:.1f}   (no calls)"
        verdict = (
            "overconfident"
            if self.gap > 0.10
            else "underconfident"
            if self.gap < -0.10
            else "calibrated"
        )
        return (
            f"  {self.low:.1f}-{self.high:.1f}   n={self.n:<5} "
            f"claimed {self.stated:.0%}  actual {self.realized:.0%}  "
            f"gap {self.gap:+.0%}  → {verdict}"
        )


def score_calibration(
    convictions: Sequence[float],
    directions: Sequence[float],
    forward_returns: Sequence[float],
    bands: Sequence[tuple[float, float]] = ((0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)),
) -> list[CalibrationBucket]:
    """Is stated conviction an honest probability?

    A conviction of 0.8 claims the call is right about 80% of the time. This
    bins calls by stated conviction and compares against what actually
    happened. Persistent overconfidence at high bands is the expensive failure,
    because conviction drives position size.
    """
    c = np.asarray(list(convictions), dtype=float)
    d = np.asarray(list(directions), dtype=float)
    f = np.asarray(list(forward_returns), dtype=float)
    if not (len(c) == len(d) == len(f)):
        raise ValueError(
            f"length mismatch: convictions={len(c)}, directions={len(d)}, returns={len(f)}"
        )

    live = np.isfinite(c) & np.isfinite(d) & np.isfinite(f) & (np.abs(d) > 1e-9)
    c, d, f = c[live], d[live], f[live]
    correct = (np.sign(d) * f) > 0

    out: list[CalibrationBucket] = []
    for lo, hi in bands:
        m = (c >= lo) & (c < hi)
        n = int(m.sum())
        out.append(
            CalibrationBucket(
                low=lo,
                high=min(hi, 1.0),
                n=n,
                stated=float(c[m].mean()) if n else 0.0,
                realized=float(correct[m].mean()) if n else 0.0,
            )
        )
    return out
