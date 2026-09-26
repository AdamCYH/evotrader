"""Performance tear sheet and regime-stratified backtest metrics."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from evotrader.backtest.engine import BacktestTrade


def infer_bars_per_day(equity_curve: list[dict[str, Any]]) -> float:
    """Median bars per session, from the curve's own timestamps.

    Annualization was hardcoded to 7 (regular-hours 1h bars). That silently
    understates Sharpe by sqrt(78/7) ~ 3.3x on 5-minute data, so derive it
    rather than assume it.
    """
    stamps = [r.get("timestamp") for r in equity_curve if r.get("timestamp")]
    if not stamps:
        return 7.0
    try:
        idx = pd.to_datetime(pd.Series(stamps), utc=True, format="mixed")
    except (ValueError, TypeError):
        return 7.0
    per_day = idx.dt.date.value_counts()
    if per_day.empty:
        return 7.0
    return max(float(per_day.median()), 1.0)


def calculate_tear_sheet(
    initial_cash: float,
    closed_trades: list[BacktestTrade],
    equity_curve: list[dict[str, Any]],
    bars_per_day: float | None = None,
    execution_assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate institutional-grade tear sheet metrics from backtest results.

    ``bars_per_day`` defaults to the median session bar count inferred from
    the equity curve. ``execution_assumptions`` is recorded verbatim so a
    stored report always says which fill model produced its numbers.
    """
    if not equity_curve:
        return {"error": "Empty equity curve"}

    if bars_per_day is None:
        bars_per_day = infer_bars_per_day(equity_curve)

    eq_df = pd.DataFrame(equity_curve)
    equities = eq_df["equity"].values
    final_equity = float(equities[-1])
    total_pnl = round(final_equity - initial_cash, 2)
    total_return_pct = round(((final_equity - initial_cash) / initial_cash) * 100.0, 2)

    # 1. Period & CAGR
    num_bars = len(equities)
    years = max(num_bars / (252.0 * bars_per_day), 0.01)
    if final_equity > 0:
        cagr = round(((final_equity / initial_cash) ** (1.0 / years) - 1.0) * 100.0, 2)
    else:
        cagr = -100.0

    # 2. Risk Metrics (Sharpe, Sortino, Drawdown)
    returns = np.diff(equities) / equities[:-1]
    annual_factor = math.sqrt(252.0 * bars_per_day)

    ret_mean = np.mean(returns) if len(returns) > 0 else 0.0
    ret_std = np.std(returns) if len(returns) > 0 else 0.0

    sharpe = round(float((ret_mean / ret_std) * annual_factor), 2) if ret_std > 0 else 0.0

    # Downside deviation for Sortino
    neg_returns = returns[returns < 0]
    downside_std = np.std(neg_returns) if len(neg_returns) > 0 else 0.0
    sortino = (
        round(float((ret_mean / downside_std) * annual_factor), 2) if downside_std > 0 else 0.0
    )

    # Drawdown series
    peak = np.maximum.accumulate(equities)
    drawdowns = (equities - peak) / peak
    max_drawdown_pct = round(float(np.min(drawdowns)) * 100.0, 2) if len(drawdowns) > 0 else 0.0

    # Drawdown duration
    dd_duration = 0
    max_dd_duration = 0
    for eq, pk in zip(equities, peak, strict=False):
        if eq < pk:
            dd_duration += 1
            max_dd_duration = max(max_dd_duration, dd_duration)
        else:
            dd_duration = 0

    # 3. Trade Metrics
    pnls = [t.realized_pnl for t in closed_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = abs(sum(losses))

    win_rate = round(len(wins) / len(pnls), 4) if pnls else 0.0
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    profit_factor = (
        round(sum(wins) / gross_loss, 4) if gross_loss > 0 else (None if not losses else 0.0)
    )
    win_loss_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0.0 else 0.0
    avg_bars_held = (
        round(sum(t.bars_held for t in closed_trades) / len(closed_trades), 1)
        if closed_trades
        else 0.0
    )

    # 4. Regime Breakdown
    regime_groups: dict[str, list[BacktestTrade]] = {}
    for t in closed_trades:
        regime_groups.setdefault(t.entry_regime, []).append(t)

    regime_breakdown = {}
    for r_name, r_trades in regime_groups.items():
        r_pnls = [t.realized_pnl for t in r_trades]
        r_wins = [p for p in r_pnls if p > 0]
        regime_breakdown[r_name] = {
            "trade_count": len(r_trades),
            "win_rate": round(len(r_wins) / len(r_trades), 3),
            "total_pnl": round(sum(r_pnls), 2),
            "avg_pnl": round(sum(r_pnls) / len(r_trades), 2),
        }

    # 5. Authoring Signal Attribution
    author_groups: dict[str, list[BacktestTrade]] = {}
    for t in closed_trades:
        author_groups.setdefault(t.authoring_signal, []).append(t)

    authoring_attribution = {}
    for a_name, a_trades in author_groups.items():
        a_pnls = [t.realized_pnl for t in a_trades]
        a_wins = [p for p in a_pnls if p > 0]
        authoring_attribution[a_name] = {
            "trade_count": len(a_trades),
            "win_rate": round(len(a_wins) / len(a_trades), 3),
            "total_pnl": round(sum(a_pnls), 2),
            "avg_pnl": round(sum(a_pnls) / len(a_trades), 2),
        }

    # 5b. Directional Breakdown — the long and short books have behaved
    # very differently, and an aggregate number hides that entirely.
    side_breakdown = {}
    for side_name in ("LONG", "SHORT"):
        s_trades = [t for t in closed_trades if t.side.value == side_name]
        if not s_trades:
            continue
        s_pnls = [t.realized_pnl for t in s_trades]
        s_wins = [p for p in s_pnls if p > 0]
        side_breakdown[side_name] = {
            "trade_count": len(s_trades),
            "win_rate": round(len(s_wins) / len(s_trades), 3),
            "total_pnl": round(sum(s_pnls), 2),
            "avg_pnl": round(sum(s_pnls) / len(s_trades), 2),
        }

    # 6. Exit Reason Breakdown
    exit_reasons: dict[str, int] = {}
    for t in closed_trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return {
        "initial_cash": initial_cash,
        "bars_per_day": round(float(bars_per_day), 2),
        "execution_assumptions": execution_assumptions or {},
        "final_equity": final_equity,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "cagr": cagr,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown_duration_bars": max_dd_duration,
        "trade_count": len(closed_trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "avg_bars_held": avg_bars_held,
        "regime_breakdown": regime_breakdown,
        "side_breakdown": side_breakdown,
        "authoring_attribution": authoring_attribution,
        "exit_reasons": exit_reasons,
    }
