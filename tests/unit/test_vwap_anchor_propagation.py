"""Regression tests for VWAP anchor propagation and participation attenuation.

Verifies:
1. normalize_market_snapshot preserves vwap_anchor without string->float corruption.
2. TechnicalIndicators model validator warns when vwap is unanchored.
3. intraday_vwap_zscore gates upfront with explicit applicable=False abstentions.
4. CompositeStrategy participation attenuation denominator uses applicable channels.
5. PerformanceAnalyser full_analysis includes net_pnl_including_reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from evotrader.agents.tools import normalize_market_snapshot
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.intraday_vwap_zscore import (
    IntradayVwapZscoreStrategy,
)
from evotrader.evolution.analyser import PerformanceAnalyser
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)


def _make_snapshot(
    vwap: float = 710.0,
    vwap_anchor: str | None = "current_session",
    last_price: float = 715.0,
    atr_14: float = 2.0,
    rvol: float = 1.0,
    n_candles: int = 30,
) -> MarketSnapshot:
    now = datetime.now(UTC)
    candles = [
        OHLCV(
            timestamp=now,
            open=710.0,
            high=710.5,
            low=709.5,
            close=710.0 + (i * 0.1),
            volume=1000.0,
        )
        for i in range(n_candles)
    ]
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=now,
        quote=Quote(
            ticker="QQQ",
            bid=last_price - 0.05,
            ask=last_price + 0.05,
            last=last_price,
            volume=50000.0,
            timestamp=now,
        ),
        indicators=TechnicalIndicators(
            vwap=vwap,
            vwap_anchor=vwap_anchor,
            atr_14=atr_14,
            relative_volume=rvol,
            rsi_14=55.0,
            macd_line=0.5,
            macd_signal=0.4,
            macd_histogram=0.1,
            bollinger_upper=720.0,
            bollinger_middle=710.0,
            bollinger_lower=700.0,
            bollinger_width=0.028,
            ema_9=712.0,
            ema_21=710.0,
            sma_20=710.0,
            sma_50=705.0,
            ibs=0.5,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.8,
            reasoning="Test range_bound",
        ),
        recent_candles=candles,
        daily_change_pct=0.5,
        gap_pct=0.1,
    )


class TestVwapAnchorNormalization:
    """Verify normalize_market_snapshot preserves vwap_anchor and context fields."""

    def test_vwap_anchor_preserved_current_session(self):
        data = {
            "ticker": "QQQ",
            "quote": {"last": 710.0},
            "indicators": {
                "vwap": 709.5,
                "vwap_anchor": "current_session",
                "rsi_14": 50.0,
                "atr_14": 2.5,
            },
            "regime": {"regime": "range_bound", "confidence": 0.9},
        }
        normalized = normalize_market_snapshot(data)
        assert normalized["indicators"]["vwap_anchor"] == "current_session"
        assert normalized["indicators"]["vwap"] == 709.5

        snapshot = MarketSnapshot.model_validate(normalized)
        assert snapshot.indicators.vwap_anchor == "current_session"

    def test_vwap_anchor_preserved_prior_session(self):
        data = {
            "ticker": "QQQ",
            "indicators": {
                "vwap": 705.0,
                "vwap_anchor": "prior_session",
            },
        }
        normalized = normalize_market_snapshot(data)
        assert normalized["indicators"]["vwap_anchor"] == "prior_session"

    def test_event_context_and_options_context_preserved(self):
        data = {
            "ticker": "QQQ",
            "indicators": {
                "vwap": 705.0,
                "vwap_anchor": "current_session",
                "atm_iv_30dte": 0.22,
                "event_type": "earnings",
                "hours_to_event": 14.5,
            },
            "options_context": {
                "pc_volume_ratio": 1.15,
                "pc_oi_ratio": 0.95,
            },
        }
        normalized = normalize_market_snapshot(data)
        assert normalized["indicators"]["event_type"] == "earnings"
        assert normalized["indicators"]["atm_iv_30dte"] == 0.22
        assert normalized["options_context"]["pc_volume_ratio"] == 1.15
        assert normalized["options_context"]["pc_oi_ratio"] == 0.95


class TestTechnicalIndicatorsValidator:
    """Verify TechnicalIndicators model validator warns on unanchored vwap."""

    def test_unanchored_vwap_triggers_warning(self, caplog):
        with caplog.at_level("WARNING"):
            ind = TechnicalIndicators(vwap=710.0, vwap_anchor=None)
            assert ind.vwap == 710.0
            assert ind.vwap_anchor is None
            assert any(
                "vwap=710.0000 populated but vwap_anchor is unset" in r.message
                for r in caplog.records
            )

    def test_anchored_vwap_no_warning(self, caplog):
        with caplog.at_level("WARNING"):
            ind = TechnicalIndicators(vwap=710.0, vwap_anchor="current_session")
            assert ind.vwap == 710.0
            assert ind.vwap_anchor == "current_session"
            assert not any("vwap_anchor is unset" in r.message for r in caplog.records)


class TestIntradayVwapZscoreAnchorGating:
    """Verify intraday_vwap_zscore upfront gating behavior."""

    def test_unknown_anchor_returns_anchor_unavailable(self):
        strat = IntradayVwapZscoreStrategy(entry_z=1.1)
        snapshot = _make_snapshot(
            vwap=700.0, vwap_anchor=None, last_price=715.0
        )  # dislocation exists
        sig = strat.compute_signal(snapshot)
        assert sig.value == 0.0
        assert sig.metadata.get("applicable") is False
        assert sig.metadata.get("reason") == "anchor_unavailable"

    def test_prior_session_anchor_returns_stale_vwap_anchor(self):
        strat = IntradayVwapZscoreStrategy(entry_z=1.1)
        snapshot = _make_snapshot(vwap=700.0, vwap_anchor="prior_session", last_price=715.0)
        sig = strat.compute_signal(snapshot)
        assert sig.value == 0.0
        assert sig.metadata.get("applicable") is False
        assert sig.metadata.get("reason") == "stale_vwap_anchor"

    def test_current_session_anchor_computes_signal(self):
        strat = IntradayVwapZscoreStrategy(entry_z=1.1)
        # Big dislocation: price 715, vwap 700, atr 2.0 -> deviation 7.5 ATR
        snapshot = _make_snapshot(vwap=700.0, vwap_anchor="current_session", last_price=715.0)
        sig = strat.compute_signal(snapshot)
        assert sig.value < 0.0  # Overextended above VWAP -> Sell/fade signal
        assert sig.metadata.get("applicable") is True
        assert sig.metadata.get("vwap_anchor") == "current_session"


class TestCompositeParticipationDenominator:
    """Verify composite participation attenuation denominator excludes non-applicable channels."""

    def test_participation_calculated_against_applicable_channels(self):
        strat = IntradayVwapZscoreStrategy(entry_z=1.1)
        snapshot = _make_snapshot(vwap=700.0, vwap_anchor="current_session", last_price=715.0)

        # Build a composite with 1 voting strategy and 5 non-applicable strategies
        class NonApplicableStrategy(IntradayVwapZscoreStrategy):
            def __init__(self, name: str):
                self._test_name = name

            @property
            def name(self) -> str:
                return self._test_name

            def compute_signal(self, s):
                from evotrader.models.signals import AlgoSignal

                return AlgoSignal(
                    name=self._test_name, value=0.0, weight=1.0, metadata={"applicable": False}
                )

        sub_strats = {"vwap_z": strat}
        weights = {"vwap_z": 0.5}
        for i in range(5):
            name = f"dark_{i}"
            sub_strats[name] = NonApplicableStrategy(name)
            weights[name] = 0.1

        comp = CompositeStrategy(
            sub_strategies=sub_strats,
            weights=weights,
            renormalize_on_abstain=True,
        )

        sig = comp.compute_detailed_signal(snapshot)
        raw_strat_val = strat.compute_signal(snapshot).value

        # 1 voting out of 1 applicable channel (denom = 1, NOT 6).
        # participation = 1/1 = 1.0 >= MIN_PARTICIPATION (0.4) -> NO haircut!
        assert sig.composite_value != 0.0
        assert pytest.approx(sig.composite_value, rel=1e-5) == raw_strat_val


class TestPerformanceAnalyserReconciliationNet:
    """Verify analyser includes net_pnl_including_reconciliation."""

    @pytest.mark.asyncio
    async def test_full_analysis_includes_net_pnl_including_reconciliation(self):
        journal = MagicMock()
        journal.get_performance_summary = AsyncMock(
            return_value={
                "trade_count": 10,
                "win_rate": 0.6,
                "avg_win": 20.0,
                "avg_loss": -10.0,
                "profit_factor": 2.0,
                "total_pnl": 100.0,
            }
        )
        journal.get_recent_trades = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "regime": "reconciliation",
                    "algo_version": "system_sync",
                    "realized_pnl": -30.0,
                    "order_status": "FILLED",
                },
                {
                    "id": 2,
                    "regime": "range_bound",
                    "algo_version": "v022_vwap_zscore_recalibration",
                    "realized_pnl": 100.0,
                    "order_status": "FILLED",
                },
            ]
        )
        metrics = MagicMock()

        analyser = PerformanceAnalyser(journal=journal, metrics=metrics)
        result = await analyser.full_analysis(lookback_days=30)

        assert "overall" in result
        assert result["overall"]["total_pnl"] == 100.0
        assert result["overall"]["net_pnl_including_reconciliation"] == 70.0  # 100 - 30
        assert result["reconciliation"]["total_pnl"] == -30.0
