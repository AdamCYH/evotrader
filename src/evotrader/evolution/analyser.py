"""Performance analyser — deep-dives into trading results.

Provides structured analysis that the Evolution Agent uses to identify
what's working, what's not, and where to focus improvements.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt
        except (ValueError, TypeError):
            return None
    return None


class PerformanceAnalyser:
    """Analyses trading performance across multiple dimensions.

    Produces structured reports for the Evolution Agent to consume.
    """

    def __init__(self, journal: Any, metrics: Any) -> None:
        self._journal = journal
        self._metrics = metrics

    @staticmethod
    def _is_reconciliation(trade: dict) -> bool:
        """Check if a trade is a broker-sync reconciliation close.

        Reconciliation trades are forced position closes created by the
        system_sync process — they are NOT algorithmic or LLM decisions
        and must not pollute signal attribution or regime performance.
        """
        return trade.get("regime") == "reconciliation" or trade.get("algo_version") == "system_sync"

    @staticmethod
    def _is_unverified_protective_fill(trade: dict) -> bool:
        """A protective order marked FILLED on the executor's word alone.

        Such a row cannot be a real fill: a stop or take-profit rests until its
        trigger is touched, so it can never fill in the same breath as being
        placed. See migration 0023 and ``tools.record_trade``.
        """
        if (trade.get("order_status") or "FILLED") != "FILLED":
            return False
        if str(trade.get("action") or "").upper() not in ("STOP_LOSS", "TAKE_PROFIT"):
            return False
        return str(trade.get("fill_source") or "") == "executor_claim"

    @staticmethod
    def _summarise(trades: list[dict]) -> dict[str, Any]:
        """Summarise a trade population. Single source of truth."""
        pnls = [t["realized_pnl"] for t in trades if t.get("realized_pnl") is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_loss = abs(sum(losses))
        return {
            "trade_count": len(pnls),
            "win_rate": len(wins) / len(pnls) if pnls else 0.0,
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "total_pnl": sum(pnls),
            # None (not 0.0) when there are no losses: 0.0 is a real and
            # maximally-negative profit factor and must not denote 'undefined'.
            "profit_factor": (round(sum(wins) / gross_loss, 4) if gross_loss > 0 else None),
        }

    async def full_analysis(self, lookback_days: int = 30) -> dict[str, Any]:
        """Run a comprehensive performance analysis.

        Returns a structured report covering:
        - Overall performance metrics
        - Breakdown by regime
        - Breakdown by time of day
        - Breakdown by signal source (algo vs LLM dominant)
        - Streak analysis (win/loss streaks)
        - Strategy attribution (which sub-strategies contribute)
        - Reconciliation P&L (broker-sync closes, reported separately)
        - Order lifecycle diagnostics (rejected/cancelled with reasons)
        """
        all_trades = await self._journal.get_recent_trades(limit=500)

        # Date-filter to the requested window BEFORE any aggregation, so
        # every block in this report describes one identical population.
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
        all_trades = [
            t
            for t in all_trades
            if _parse_ts(t.get("timestamp")) is None or _parse_ts(t.get("timestamp")) >= cutoff
        ]

        # Separate reconciliation trades — they are forced broker-sync
        # closes, not real decisions, and must not distort attribution.
        decision_trades = [t for t in all_trades if not self._is_reconciliation(t)]
        recon_trades = [t for t in all_trades if self._is_reconciliation(t)]
        recon_pnls = [
            t.get("realized_pnl", 0) for t in recon_trades if t.get("realized_pnl") is not None
        ]

        # Separate by order lifecycle status for diagnostics.
        # Only FILLED trades should contribute to P&L attribution.
        #
        # ── AND ONLY A FILL THAT CAN HAVE HAPPENED ────────────────────
        # A resting protective order cannot fill at the moment it is placed, so
        # a STOP_LOSS or TAKE_PROFIT row marked FILLED with no broker evidence
        # behind it (``fill_source = 'executor_claim'``, migration 0023) records
        # an exit that did not occur. record_trade now refuses to write one, but
        # rows written before that fix are still in the journal: on 2026-09-25
        # four of them put phantom realized losses into this headline and
        # produced a false loss streak and profit factor from losses that never
        # happened. They are counted and reported separately rather than
        # silently dropped — an unverified fill is a defect to see, not to hide.
        filled_trades = [
            t
            for t in decision_trades
            if (t.get("order_status") or "FILLED") == "FILLED"
            and not self._is_unverified_protective_fill(t)
        ]
        unverified_fills = [t for t in decision_trades if self._is_unverified_protective_fill(t)]
        if unverified_fills:
            logger.warning(
                "%d row(s) claim a protective FILL with no broker evidence "
                "(ids %s) — excluded from realized P&L. These need repairing in "
                "the journal.",
                len(unverified_fills),
                ", ".join(str(t.get("id")) for t in unverified_fills),
            )
        pending_trades = [t for t in decision_trades if t.get("order_status") == "PENDING"]
        rejected_trades = [
            t for t in decision_trades if t.get("order_status") in ("REJECTED", "FAILED")
        ]
        cancelled_trades = [t for t in decision_trades if t.get("order_status") == "CANCELLED"]

        total_recon_pnl = sum(recon_pnls)
        # Derive the headline from filled_trades — the SAME list that feeds
        # regime_breakdown and signal_attribution. Previously this came from
        # a separate date-windowed journal query, so the headline could count
        # a few trades, all winners, while the breakdown in the very same report
        # counted every trade.
        overall_summary = self._summarise(filled_trades)
        overall_summary["trade_population"] = (
            f"{len(filled_trades)} filled decision trades within {lookback_days}d"
        )
        # Keep the raw journal summary for reference, clearly namespaced.
        journal_summary = await self._journal.get_performance_summary(days=lookback_days)
        overall_summary["journal_summary_unfiltered"] = (
            dict(journal_summary) if isinstance(journal_summary, dict) else {}
        )
        overall_summary["net_pnl_including_reconciliation"] = round(
            float(overall_summary.get("total_pnl", 0.0)) + total_recon_pnl, 4
        )

        analysis = {
            "period": {
                "lookback_days": lookback_days,
                "analysis_timestamp": datetime.now(UTC).isoformat(),
            },
            "overall": overall_summary,
            "regime_breakdown": self._analyse_by_regime(filled_trades),
            "signal_attribution": self._analyse_signal_sources(filled_trades),
            "streak_analysis": self._analyse_streaks(filled_trades),
            "reconciliation": {
                "trade_count": len(recon_trades),
                "total_pnl": total_recon_pnl,
                "note": (
                    "Broker-sync forced closes — not algo/LLM decisions. "
                    "Excluded from regime breakdown and signal attribution."
                ),
            },
            "order_lifecycle": {
                "filled_count": len(filled_trades),
                "pending_count": len(pending_trades),
                "rejected_count": len(rejected_trades),
                "cancelled_count": len(cancelled_trades),
                "rejected_details": [
                    {
                        "trade_id": t.get("id"),
                        "ticker": t.get("ticker"),
                        "direction": t.get("direction"),
                        "action": t.get("action"),
                        "option_id": t.get("option_id"),
                        "regime": t.get("regime"),
                        "broker_reason": t.get("broker_status_reason"),
                        "timestamp": t.get("timestamp"),
                    }
                    for t in rejected_trades
                ],
                "cancelled_details": [
                    {
                        "trade_id": t.get("id"),
                        "ticker": t.get("ticker"),
                        "broker_reason": t.get("broker_status_reason"),
                        "timestamp": t.get("timestamp"),
                    }
                    for t in cancelled_trades
                ],
            },
            "recommendations": [],
        }

        # Generate recommendations
        analysis["recommendations"] = self._generate_recommendations(analysis)

        return analysis

    def _analyse_by_regime(self, trades: list[dict]) -> dict[str, Any]:
        """Break down performance by market regime."""
        by_regime: dict[str, list[float]] = {}
        for t in trades:
            regime = t.get("regime", "unknown")
            pnl = t.get("realized_pnl")
            if pnl is not None:
                by_regime.setdefault(regime, []).append(pnl)

        result = {}
        for regime, pnls in by_regime.items():
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            result[regime] = {
                "total_trades": len(pnls),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "total_pnl": sum(pnls),
                "avg_win": sum(wins) / len(wins) if wins else 0,
                "avg_loss": sum(losses) / len(losses) if losses else 0,
            }
        return result

    def _analyse_signal_sources(self, trades: list[dict]) -> dict[str, Any]:
        """Analyse whether algo or LLM signals are more predictive."""
        algo_dominant = []
        llm_dominant = []
        algo_muted = []

        for t in trades:
            pnl = t.get("realized_pnl")
            if pnl is None:
                continue
            algo = abs(t.get("algo_signal", 0))
            llm = abs(t.get("llm_signal", 0) or 0)

            if algo < 1e-3:
                algo_muted.append(pnl)
            elif algo > llm:
                algo_dominant.append(pnl)
            else:
                llm_dominant.append(pnl)

        def _stats(pnls: list[float]) -> dict[str, Any]:
            return {
                "count": len(pnls),
                "win_rate": (len([p for p in pnls if p > 0]) / len(pnls) if pnls else 0.0),
                "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            }

        return {
            "algo_dominant": _stats(algo_dominant),
            "llm_dominant": _stats(llm_dominant),
            "algo_muted": _stats(algo_muted),
        }

    def _analyse_streaks(self, trades: list[dict]) -> dict[str, Any]:
        """Analyse win/loss streaks."""
        streak = 0
        max_win_streak = 0
        max_loss_streak = 0
        current_type = None

        # get_recent_trades returns newest-first; scanning that order makes
        # 'current_streak' describe the OLDEST run in the window.
        ordered = sorted(
            trades,
            key=lambda t: _parse_ts(t.get("timestamp")) or datetime.min.replace(tzinfo=UTC),
        )
        for t in ordered:
            pnl = t.get("realized_pnl")
            if pnl is None:
                continue

            if pnl > 0:
                if current_type == "win":
                    streak += 1
                else:
                    streak = 1
                    current_type = "win"
                max_win_streak = max(max_win_streak, streak)
            else:
                if current_type == "loss":
                    streak += 1
                else:
                    streak = 1
                    current_type = "loss"
                max_loss_streak = max(max_loss_streak, streak)

        return {
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "current_streak_type": current_type,
            "current_streak_length": streak,
        }

    def _generate_recommendations(self, analysis: dict[str, Any]) -> list[str]:
        """Generate actionable recommendations from the analysis."""
        recs: list[str] = []
        overall = analysis.get("overall", {})
        regime_data = analysis.get("regime_breakdown", {})
        signal_data = analysis.get("signal_attribution", {})

        # Win rate recommendations (only with sufficient sample size)
        trade_count = overall.get("trade_count", 0)
        win_rate = overall.get("win_rate", 0)
        if (
            isinstance(trade_count, int)
            and trade_count >= 10
            and isinstance(win_rate, (int, float))
            and win_rate < 0.45
        ):
            recs.append(
                "Win rate below 45%. Audit which sub-signal authored the "
                "losing entries (authoring_signal attribution) and consider "
                "reweighting, recalibrating, or inverting that channel. "
                "Do NOT raise entry thresholds — fewer trades means less "
                "learning, not better performance."
            )

        # Regime-specific recommendations
        for regime, stats in regime_data.items():
            if stats.get("total_trades", 0) >= 5 and stats.get("win_rate", 0) < 0.35:
                recs.append(
                    f"Poor performance in {regime} regime "
                    f"(win rate: {stats['win_rate']:.0%}). "
                    f"Investigate (a) whether the regime detector is "
                    f"misclassifying this tape, and (b) whether the "
                    f"{regime} weight vector can even EXPRESS the observed "
                    f"price direction. Do NOT skip the regime."
                )

        # Signal attribution
        algo = signal_data.get("algo_dominant", {})
        llm = signal_data.get("llm_dominant", {})
        if algo.get("count", 0) > 5 and llm.get("count", 0) > 5:
            if algo.get("avg_pnl", 0) > llm.get("avg_pnl", 0) * 1.5:
                recs.append(
                    "Algo-dominant trades outperform LLM-dominant trades. "
                    "Consider increasing algo weight in the hybrid scorer's blend parameters."
                )
            elif llm.get("avg_pnl", 0) > algo.get("avg_pnl", 0) * 1.5:
                recs.append(
                    "LLM-dominant trades outperform algo-dominant trades. "
                    "NOTE: verify this is not a labelling artifact — if the "
                    "composite is near-zero, trades are classified "
                    "llm_dominant by default. Check composite magnitude "
                    "health before concluding the algorithm is inferior. "
                    "(Blend weights live in the hybrid scorer, NOT in "
                    "regime_weights.)"
                )

        return recs
