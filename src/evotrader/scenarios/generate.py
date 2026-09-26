"""Generate scenarios from real historical data, with known outcomes.

A hand-written scenario tests whether the agent follows its instructions. It
cannot tell you whether following them makes money. These scenarios are built
from real market snapshots produced by the live pipeline, paired with the
forward return that actually followed — so a decision can be scored in dollars
rather than only against rules.

Selection is by forward-return quantile, not by hand. Picking interesting-looking
setups is how a test suite comes to measure the author's hindsight instead of the
agent's judgement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import yaml

from evotrader.models.market import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass
class HistoricalCase:
    """One real market moment plus what happened next."""

    index: int
    snapshot: MarketSnapshot
    composite_value: float
    sub_signals: list[dict[str, Any]]
    forward_return_pct: float
    horizon_bars: int
    label: str


def select_cases(
    snapshots: list[MarketSnapshot],
    composites: list[Any],
    horizon_bars: int = 35,
    per_bucket: int = 1,
    seed: int = 7,
) -> list[HistoricalCase]:
    """Pick cases spanning the forward-return distribution.

    Buckets are quantiles of the realised forward return, so the set always
    contains large up moves, large down moves and chop — including setups where
    the signal pointed the wrong way. Sampling within a bucket is seeded, so the
    same inputs always yield the same cases.
    """
    n = len(snapshots)
    if n <= horizon_bars + 1:
        raise ValueError(f"need more than {horizon_bars + 1} snapshots, got {n}")
    if len(composites) != n:
        raise ValueError(f"composites ({len(composites)}) must match snapshots ({n})")

    px = np.array([s.quote.last for s in snapshots], dtype=float)
    fwd = (px[horizon_bars:] / px[:-horizon_bars] - 1.0) * 100.0

    edges = np.quantile(fwd, [0.0, 0.10, 0.35, 0.65, 0.90, 1.0])
    labels = ["large_decline", "mild_decline", "chop", "mild_advance", "large_advance"]

    rng = np.random.default_rng(seed)
    cases: list[HistoricalCase] = []
    for i, label in enumerate(labels):
        lo, hi = edges[i], edges[i + 1]
        idx = np.flatnonzero((fwd >= lo) & (fwd <= hi))
        # Keep away from the very start, where indicator warmup is incomplete.
        idx = idx[idx > 200]
        if len(idx) == 0:
            logger.warning("No cases in bucket %s", label)
            continue
        for pick in rng.choice(idx, size=min(per_bucket, len(idx)), replace=False):
            j = int(pick)
            detailed = composites[j]
            cases.append(
                HistoricalCase(
                    index=j,
                    snapshot=snapshots[j],
                    composite_value=float(detailed.composite_value),
                    sub_signals=[
                        {
                            "name": s.name,
                            "value": s.value,
                            "weight": s.weight,
                            "metadata": s.metadata,
                        }
                        for s in detailed.signals
                    ],
                    forward_return_pct=float(fwd[j]),
                    horizon_bars=horizon_bars,
                    label=label,
                )
            )
    logger.info("Selected %d cases across %d buckets", len(cases), len(labels))
    return sorted(cases, key=lambda c: c.index)


def render_market(case: HistoricalCase, cash: float = 5000.0) -> str:
    """Format a case exactly as the live agent receives a cycle."""
    s = case.snapshot
    ind = s.indicators
    lines = [
        f"## CYCLE INPUT — {s.ticker} @ {s.timestamp:%Y-%m-%d %H:%M} UTC",
        "",
        f"price: ${s.quote.last:.2f}    regime: {s.regime.regime.value} "
        f"(confidence {s.regime.confidence:.4f})",
        f"portfolio: ${cash:,.0f} cash, no open positions",
        f"composite_signal: {case.composite_value:+.4f}",
        "",
        "### Indicators",
    ]
    for key in (
        "rsi_14",
        "macd_histogram",
        "atr_14",
        "bollinger_upper",
        "bollinger_middle",
        "bollinger_lower",
        "vwap",
        "relative_volume",
        "ema_9",
        "ema_21",
        "sma_20",
        "sma_50",
    ):
        v = getattr(ind, key, None)
        if v is not None:
            lines.append(f"  {key}: {v:.4f}" if isinstance(v, float) else f"  {key}: {v}")
    lines.append(f"  daily_change_pct: {s.daily_change_pct}")
    lines.append(f"  gap_pct: {s.gap_pct}")

    lines += ["", "### Sub-signals"]
    for sub in case.sub_signals:
        meta = sub.get("metadata", {}) or {}
        flat = ", ".join(
            f"{k}={v}" for k, v in list(meta.items())[:5] if not isinstance(v, (dict, list))
        )
        lines.append(
            f"  {sub['name']:<26} value={sub['value']:+.6f}  "
            f"weight={sub['weight']:.2f}  {flat[:140]}"
        )

    lines += [
        "",
        "### News & Sentiment Agent report",
        "  aggregate_score: 0.0",
        "  confidence: 0.0",
        "  items: []",
        "  key_events: []",
        '  reasoning: "No news retrieved for this historical replay."',
        "",
        "### Tool results",
        "  compute_risk_budget -> see below",
    ]
    return "\n".join(lines)


def to_yaml(case: HistoricalCase, risk_budget: float, name: str) -> str:
    """Serialise a case as a scenario file.

    ``outcome`` records what actually happened. It is written to the file for
    scoring but must never be included in the prompt handed to an agent.
    """
    doc = {
        "name": name,
        "description": (
            f"Real {case.snapshot.ticker} snapshot, {case.snapshot.timestamp:%Y-%m-%d %H:%M}. "
            f"Forward-return bucket: {case.label}."
        ),
        "facts": {
            "historical": True,
            "has_open_position": False,
            "price": round(case.snapshot.quote.last, 2),
            "atr": round(case.snapshot.indicators.atr_14 or 0.0, 2),
        },
        "market": render_market(case).replace(
            "compute_risk_budget -> see below",
            f'compute_risk_budget -> {{"risk_budget": {risk_budget:.2f}, '
            f'"target_volatility": 0.20, "max_exposure": 1.0}}',
        ),
        "outcome": {
            "forward_return_pct": round(case.forward_return_pct, 4),
            "horizon_bars": case.horizon_bars,
            "bucket": case.label,
            "note": "HIDDEN FROM THE AGENT. Used only to score the decision.",
        },
        "rules": [
            {
                "name": "logs_attribution",
                "description": "Attribution must be logged every cycle, traded or not.",
                "require": [r"log_signal_attribution"],
                "severity": "error",
            }
        ],
    }
    return yaml.safe_dump(doc, sort_keys=False, width=100)
