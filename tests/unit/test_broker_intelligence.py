"""Tests for new Robinhood MCP tool integrations.

Covers:
- Tax lots / wash sale detection
- Level II order book depth analysis
- Options historicals / IV trend computation
- P&L reconciliation
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# ═══════════════════════════════════════════════════════════════════════
# Tax Lots / Wash Sale Detection
# ═══════════════════════════════════════════════════════════════════════


class TestCheckWashSaleRisk:
    """Tests for check_wash_sale_risk."""

    def _make_config(self, *, enabled: bool = True, mode: str = "warn", lookback_days: int = 30):
        from evotrader.models.config import WashSaleGuard

        return WashSaleGuard(enabled=enabled, mode=mode, lookback_days=lookback_days)

    def test_no_risk_when_disabled(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config(enabled=False)
        result = check_wash_sale_risk(
            "QQQ", [{"sold_date": "2026-07-27", "gain_loss": -50.0}], config
        )
        assert result["wash_sale_risk"] is False

    def test_no_risk_when_no_lots(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config()
        result = check_wash_sale_risk("QQQ", [], config)
        assert result["wash_sale_risk"] is False

    def test_no_risk_when_lot_is_gain(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config()
        recent = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        lots = [{"sold_date": recent, "gain_loss": 100.0}]
        result = check_wash_sale_risk("QQQ", lots, config)
        assert result["wash_sale_risk"] is False

    def test_risk_detected_within_window(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config(lookback_days=30)
        recent = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        lots = [{"sold_date": recent, "gain_loss": -75.50}]
        result = check_wash_sale_risk("QQQ", lots, config)

        assert result["wash_sale_risk"] is True
        assert result["days_since_last_loss_sale"] == 10
        assert result["disallowed_loss"] == 75.50
        assert len(result["loss_lots"]) == 1

    def test_no_risk_outside_window(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config(lookback_days=30)
        old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
        lots = [{"sold_date": old, "gain_loss": -100.0}]
        result = check_wash_sale_risk("QQQ", lots, config)
        assert result["wash_sale_risk"] is False

    def test_multiple_loss_lots_accumulate(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config()
        d1 = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        d2 = (datetime.now(UTC) - timedelta(days=15)).isoformat()
        lots = [
            {"sold_date": d1, "gain_loss": -30.0},
            {"sold_date": d2, "gain_loss": -20.0},
        ]
        result = check_wash_sale_risk("QQQ", lots, config)
        assert result["wash_sale_risk"] is True
        assert result["disallowed_loss"] == 50.0
        assert result["days_since_last_loss_sale"] == 5
        assert len(result["loss_lots"]) == 2

    def test_unsold_lots_ignored(self) -> None:
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config()
        lots = [{"quantity": 10, "cost_basis": 500.0}]  # No sold_date
        result = check_wash_sale_risk("QQQ", lots, config)
        assert result["wash_sale_risk"] is False

    def test_alternate_field_names(self) -> None:
        """Handles Robinhood-style 'close_date' and 'realized_gain_loss'."""
        from evotrader.tools.tax_lots import check_wash_sale_risk

        config = self._make_config()
        recent = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        lots = [{"close_date": recent, "realized_gain_loss": -42.0}]
        result = check_wash_sale_risk("QQQ", lots, config)
        assert result["wash_sale_risk"] is True
        assert result["disallowed_loss"] == 42.0


# ═══════════════════════════════════════════════════════════════════════
# Level II Order Book Depth
# ═══════════════════════════════════════════════════════════════════════


class TestAssessBookDepth:
    """Tests for assess_book_depth."""

    def _make_config(self, *, depth_levels: int = 5, thin_book_warn_pct: float = 50):
        from evotrader.models.config import LevelIIConfig

        return LevelIIConfig(
            enabled=True, depth_levels=depth_levels, thin_book_warn_pct=thin_book_warn_pct
        )

    def test_empty_data_returns_unavailable(self) -> None:
        from evotrader.tools.level_ii import assess_book_depth

        result = assess_book_depth({})
        assert result["available"] is False
        assert result["spread_bps"] is None

    def test_basic_depth_calculation(self) -> None:
        from evotrader.tools.level_ii import assess_book_depth

        data = {
            "data": {
                "bids": [
                    {"price": 500.0, "size": 100},
                    {"price": 499.50, "size": 200},
                ],
                "asks": [
                    {"price": 500.10, "size": 150},
                    {"price": 500.50, "size": 50},
                ],
            }
        }
        result = assess_book_depth(data)
        assert result["available"] is True
        assert result["best_bid"] == 500.0
        assert result["best_ask"] == 500.10
        assert result["bid_depth_usd"] == 500.0 * 100 + 499.50 * 200
        assert result["ask_depth_usd"] == 500.10 * 150 + 500.50 * 50
        assert result["depth_levels"] == 2

    def test_spread_bps(self) -> None:
        from evotrader.tools.level_ii import assess_book_depth

        data = {
            "data": {
                "bids": [{"price": 100.0, "size": 1}],
                "asks": [{"price": 100.10, "size": 1}],
            }
        }
        result = assess_book_depth(data)
        # spread = 0.10, mid = 100.05, bps = 0.10/100.05 * 10000 ≈ 9.99
        assert result["spread_bps"] is not None
        assert 9.0 < result["spread_bps"] < 11.0

    def test_thin_book_detection(self) -> None:
        from evotrader.tools.level_ii import assess_book_depth

        config = self._make_config(thin_book_warn_pct=50)
        data = {
            "data": {
                "bids": [{"price": 500.0, "size": 10}],  # $5000 depth
                "asks": [{"price": 500.10, "size": 10}],  # $5001 depth
            }
        }
        # Order value $6000 > 50% of total depth ($10001)
        result = assess_book_depth(data, order_value=6000, config=config)
        assert result["thin_book"] is True

    def test_no_thin_book_when_small_order(self) -> None:
        from evotrader.tools.level_ii import assess_book_depth

        config = self._make_config(thin_book_warn_pct=50)
        data = {
            "data": {
                "bids": [{"price": 500.0, "size": 100}],
                "asks": [{"price": 500.10, "size": 100}],
            }
        }
        result = assess_book_depth(data, order_value=1000, config=config)
        assert result["thin_book"] is False

    def test_imbalance_ratio(self) -> None:
        from evotrader.tools.level_ii import assess_book_depth

        data = {
            "data": {
                "bids": [{"price": 100.0, "size": 300}],  # $30000
                "asks": [{"price": 100.10, "size": 100}],  # $10010
            }
        }
        result = assess_book_depth(data)
        # bid-heavy → ratio > 0.5
        assert result["imbalance_ratio"] > 0.7


# ═══════════════════════════════════════════════════════════════════════
# Options Historicals / IV Trend
# ═══════════════════════════════════════════════════════════════════════


class TestComputeIvTrend:
    """Tests for compute_iv_trend."""

    def test_insufficient_data_returns_flat(self) -> None:
        from evotrader.tools.options_historicals import compute_iv_trend

        result = compute_iv_trend([{"implied_volatility": 0.3}])
        assert result["available"] is False
        assert result["iv_trend"] == "flat"

    def test_rising_iv(self) -> None:
        from evotrader.tools.options_historicals import compute_iv_trend

        # Steadily rising IV
        historicals = [{"implied_volatility": 0.20 + i * 0.03} for i in range(10)]
        result = compute_iv_trend(historicals, noise_threshold=0.005)
        assert result["available"] is True
        assert result["iv_trend"] == "rising"
        assert result["iv_slope"] > 0

    def test_falling_iv(self) -> None:
        from evotrader.tools.options_historicals import compute_iv_trend

        historicals = [{"implied_volatility": 0.50 - i * 0.03} for i in range(10)]
        result = compute_iv_trend(historicals, noise_threshold=0.005)
        assert result["available"] is True
        assert result["iv_trend"] == "falling"
        assert result["iv_slope"] < 0

    def test_flat_iv(self) -> None:
        from evotrader.tools.options_historicals import compute_iv_trend

        historicals = [{"implied_volatility": 0.30} for _ in range(10)]
        result = compute_iv_trend(historicals, noise_threshold=0.005)
        assert result["iv_trend"] == "flat"
        assert abs(result["iv_slope"]) < 0.005

    def test_iv_percentile(self) -> None:
        from evotrader.tools.options_historicals import compute_iv_trend

        # Values range 0.10 to 0.50; current (last) is 0.50 → 100th percentile
        historicals = [{"implied_volatility": 0.10 + i * 0.05} for i in range(9)]
        result = compute_iv_trend(historicals)
        assert result["iv_percentile"] == 100.0

    def test_skips_invalid_iv(self) -> None:
        from evotrader.tools.options_historicals import compute_iv_trend

        historicals = [
            {"implied_volatility": None},
            {"implied_volatility": 0.0},  # skipped (not > 0)
            {"implied_volatility": 0.3},
            {"implied_volatility": 0.31},
            {"implied_volatility": 0.32},
        ]
        result = compute_iv_trend(historicals)
        assert result["available"] is True
        assert result["data_points"] == 3


# ═══════════════════════════════════════════════════════════════════════
# P&L Reconciliation
# ═══════════════════════════════════════════════════════════════════════


class TestReconcilePnl:
    """Tests for reconcile_pnl."""

    @pytest.mark.asyncio
    async def test_reconciled_within_tolerance(self) -> None:
        from evotrader.tools.pnl_reconciliation import reconcile_pnl

        async def mock_mcp(tool_name, args):
            return {"data": {"total_realized_pnl": 100.30}}

        result = await reconcile_pnl(100.0, mock_mcp, tolerance=0.50)
        assert result["reconciled"] is True
        assert result["discrepancy"] == 0.30

    @pytest.mark.asyncio
    async def test_discrepancy_detected(self) -> None:
        from evotrader.tools.pnl_reconciliation import reconcile_pnl

        async def mock_mcp(tool_name, args):
            return {"data": {"total_realized_pnl": 150.0}}

        result = await reconcile_pnl(100.0, mock_mcp, tolerance=0.50)
        assert result["reconciled"] is False
        assert result["discrepancy"] == 50.0
        assert len(result["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_handles_mcp_failure_gracefully(self) -> None:
        from evotrader.tools.pnl_reconciliation import reconcile_pnl

        async def mock_mcp(tool_name, args):
            return None

        result = await reconcile_pnl(100.0, mock_mcp)
        assert result["reconciled"] is True
        assert "Could not fetch broker P&L" in result["warnings"][0]


# ═══════════════════════════════════════════════════════════════════════
# Constitution Config Models
# ═══════════════════════════════════════════════════════════════════════


class TestWashSaleGuardConfig:
    """Tests for WashSaleGuard Pydantic model."""

    def test_defaults(self) -> None:
        from evotrader.models.config import WashSaleGuard

        config = WashSaleGuard()
        assert config.enabled is True
        assert config.mode == "warn"
        assert config.lookback_days == 30

    def test_block_mode(self) -> None:
        from evotrader.models.config import WashSaleGuard

        config = WashSaleGuard(mode="block")
        assert config.mode == "block"

    def test_invalid_mode_rejected(self) -> None:
        from evotrader.models.config import WashSaleGuard

        with pytest.raises(Exception):
            WashSaleGuard(mode="ignore")

    def test_constitution_includes_wash_sale(self) -> None:
        from evotrader.models.config import Constitution

        c = Constitution()
        assert c.wash_sale_guard.enabled is True
        assert c.wash_sale_guard.mode == "warn"


class TestLevelIIConfig:
    """Tests for LevelIIConfig Pydantic model."""

    def test_defaults(self) -> None:
        from evotrader.models.config import LevelIIConfig

        config = LevelIIConfig()
        assert config.enabled is True
        assert config.depth_levels == 5
        assert config.thin_book_warn_pct == 0.50

    def test_constitution_includes_level_ii(self) -> None:
        from evotrader.models.config import Constitution

        c = Constitution()
        assert c.level_ii.enabled is True
        assert c.level_ii.depth_levels == 5
