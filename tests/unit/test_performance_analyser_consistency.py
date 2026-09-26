"""Regression tests for Code Review: 20260904_211130_analyser_headline_metrics_inconsistent_and_recommendations_violate_mandate.

Guards against:
1. Headline block and breakdown blocks describing different populations.
2. Recommendations violating the anti-conservatism mandate (recommending tightening thresholds or skipping regimes).
3. Recommendation referencing nonexistent regime_weights parameter for LLM blending.
4. profit_factor reporting 0.0 when there are zero losses.
5. Reverse-chronological streak calculation in _analyse_streaks.
6. Broken composite causing trades to be mislabelled llm_dominant instead of algo_muted.

See: data/evolution/reviews/20260904_211130_analyser_headline_metrics_inconsistent_and_recommendations_violate_mandate.md
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from evotrader.evolution.analyser import PerformanceAnalyser


class TestPerformanceAnalyserConsistency:
    """Tests guarding against findings in code review 20260904_211130."""

    @pytest.mark.asyncio
    async def test_headline_and_breakdown_describe_same_population(self) -> None:
        """Finding 1: overall metrics must be derived from filled_trades matching
        the breakdown sections, with population metadata explicitly surfaced.
        """
        now = datetime.now(UTC)
        recent_trades = [
            # 3 range_bound filled trades
            {
                "id": 1,
                "regime": "range_bound",
                "realized_pnl": 10.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            {
                "id": 2,
                "regime": "range_bound",
                "realized_pnl": -5.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            {
                "id": 3,
                "regime": "range_bound",
                "realized_pnl": 15.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            # 2 trending_bear filled trades
            {
                "id": 4,
                "regime": "trending_bear",
                "realized_pnl": -8.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            {
                "id": 5,
                "regime": "trending_bear",
                "realized_pnl": -2.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            # 1 reconciliation trade (forced close)
            {
                "id": 6,
                "regime": "reconciliation",
                "realized_pnl": -50.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            # 1 rejected trade
            {
                "id": 7,
                "regime": "range_bound",
                "realized_pnl": None,
                "order_status": "REJECTED",
                "timestamp": now.isoformat(),
            },
        ]
        journal = AsyncMock()
        journal.get_recent_trades.return_value = recent_trades
        journal.get_performance_summary.return_value = {
            "trade_count": 999,  # Unfiltered journal dummy
            "win_rate": 0.99,
        }
        analyser = PerformanceAnalyser(journal=journal, metrics=AsyncMock())
        result = await analyser.full_analysis(lookback_days=30)

        overall = result["overall"]
        regimes = result["regime_breakdown"]

        # Exactly 5 filled decision trades
        assert overall["trade_count"] == 5
        sum_breakdown_trades = sum(r["total_trades"] for r in regimes.values())
        assert sum_breakdown_trades == 5
        assert overall["win_rate"] == 2 / 5  # 2 wins, 3 losses
        assert overall["total_pnl"] == 10.0  # 10 - 5 + 15 - 8 - 2
        assert "5 filled decision trades within 30d" in overall["trade_population"]
        assert overall["journal_summary_unfiltered"]["trade_count"] == 999
        # Net including reconciliation: 10.0 + (-50.0) = -40.0
        assert overall["net_pnl_including_reconciliation"] == -40.0

    @pytest.mark.asyncio
    async def test_date_window_filters_trades(self) -> None:
        """Finding 1: all_trades must be date-filtered by lookback_days before analysis."""
        now = datetime.now(UTC)
        old_time = now - timedelta(days=45)
        recent_trades = [
            {
                "id": 1,
                "regime": "range_bound",
                "realized_pnl": 10.0,
                "order_status": "FILLED",
                "timestamp": now.isoformat(),
            },
            {
                "id": 2,
                "regime": "range_bound",
                "realized_pnl": 20.0,
                "order_status": "FILLED",
                "timestamp": old_time.isoformat(),
            },
        ]
        journal = AsyncMock()
        journal.get_recent_trades.return_value = recent_trades
        journal.get_performance_summary.return_value = {}
        analyser = PerformanceAnalyser(journal=journal, metrics=AsyncMock())

        result = await analyser.full_analysis(lookback_days=30)
        assert result["overall"]["trade_count"] == 1
        assert result["overall"]["total_pnl"] == 10.0

    def test_profit_factor_sentinel_on_zero_losses(self) -> None:
        """Finding 4: profit_factor must return None (not 0.0) when there are no losses."""
        wins_only = [
            {"realized_pnl": 25.0},
            {"realized_pnl": 15.0},
        ]
        summary = PerformanceAnalyser._summarise(wins_only)
        assert summary["win_rate"] == 1.0
        assert summary["profit_factor"] is None

        mixed = [
            {"realized_pnl": 20.0},
            {"realized_pnl": -10.0},
        ]
        summary_mixed = PerformanceAnalyser._summarise(mixed)
        assert summary_mixed["profit_factor"] == 2.0

        losses_only = [
            {"realized_pnl": -10.0},
            {"realized_pnl": -5.0},
        ]
        summary_loss = PerformanceAnalyser._summarise(losses_only)
        assert summary_loss["profit_factor"] == 0.0

    def test_streaks_ordered_chronologically(self) -> None:
        """Finding 5: _analyse_streaks must process chronologically ascending so
        current_streak describes the latest trades, even if input is newest-first.
        """
        now = datetime.now(UTC)
        t_10am = (now - timedelta(hours=3)).isoformat()
        t_11am = (now - timedelta(hours=2)).isoformat()
        t_12pm = (now - timedelta(hours=1)).isoformat()

        # Input is newest-first: 12pm (WIN), 11am (WIN), 10am (LOSS)
        newest_first_trades = [
            {"realized_pnl": 20.0, "timestamp": t_12pm},
            {"realized_pnl": 15.0, "timestamp": t_11am},
            {"realized_pnl": -10.0, "timestamp": t_10am},
        ]
        analyser = PerformanceAnalyser(journal=None, metrics=None)
        streaks = analyser._analyse_streaks(newest_first_trades)

        assert streaks["current_streak_type"] == "win"
        assert streaks["current_streak_length"] == 2
        assert streaks["max_win_streak"] == 2
        assert streaks["max_loss_streak"] == 1

    def test_recommendations_comply_with_anti_conservatism_mandate(self) -> None:
        """Finding 2 & 3: Recommendations must never recommend skipping regimes
        or raising entry thresholds, and must refer to hybrid scorer for blend weights.
        """
        analyser = PerformanceAnalyser(journal=None, metrics=None)
        analysis = {
            "overall": {"trade_count": 15, "win_rate": 0.30},
            "regime_breakdown": {
                "trending_bear": {"total_trades": 8, "win_rate": 0.20},
            },
            "signal_attribution": {
                "algo_dominant": {"count": 6, "avg_pnl": 2.0},
                "llm_dominant": {"count": 6, "avg_pnl": 5.0},
            },
        }
        recs = analyser._generate_recommendations(analysis)

        # Win rate recommendation
        win_rec = [r for r in recs if "Win rate below 45%" in r]
        assert len(win_rec) == 1
        assert "Do NOT raise entry thresholds" in win_rec[0]
        assert "authoring_signal attribution" in win_rec[0]

        # Regime recommendation
        regime_rec = [r for r in recs if "Poor performance in trending_bear regime" in r]
        assert len(regime_rec) == 1
        assert "Do NOT skip the regime" in regime_rec[0]
        assert "misclassifying this tape" in regime_rec[0]

        # LLM recommendation
        llm_rec = [r for r in recs if "LLM-dominant trades outperform" in r]
        assert len(llm_rec) == 1
        assert "hybrid scorer" in llm_rec[0]
        assert "NOT in regime_weights" in llm_rec[0]
        assert "labelling artifact" in llm_rec[0]

    def test_algo_muted_classification(self) -> None:
        """Finding 3: Near-zero algo signal (< 1e-3) is labelled algo_muted,
        not llm_dominant.
        """
        analyser = PerformanceAnalyser(journal=None, metrics=None)
        trades = [
            # Near-zero algo signal (e.g. annihilated 0.000164)
            {"realized_pnl": 10.0, "algo_signal": 0.000164, "llm_signal": 0.4},
            # Real algo dominant
            {"realized_pnl": 15.0, "algo_signal": 0.5, "llm_signal": 0.2},
            # Real LLM dominant
            {"realized_pnl": -5.0, "algo_signal": 0.1, "llm_signal": 0.4},
        ]
        sa = analyser._analyse_signal_sources(trades)

        assert sa["algo_muted"]["count"] == 1
        assert sa["algo_dominant"]["count"] == 1
        assert sa["llm_dominant"]["count"] == 1
