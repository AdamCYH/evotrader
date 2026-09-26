"""Regression tests: snapshot field passthrough in normalize_market_snapshot().

Guards against the bug where normalize_market_snapshot() silently dropped
daily_change_pct, gap_pct, and previous_close — disabling four independent
algorithm guards (momentum divergence, gap strategy, range_break_continuation
magnitude gate, trend_persistence counter-day).

See: data/evolution/reviews/20260803_220901_critical_snapshot_field_drop_disables_four_algo_guards.md
"""

from __future__ import annotations

from datetime import datetime

from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

# ─── Helpers ──────────────────────────────────────────────────────────


def _base_snapshot_dict(
    daily_change_pct: float | None = 1.83,
    gap_pct: float | None = 0.45,
    previous_close: float | None = 449.0,
) -> dict:
    """Build a minimal valid snapshot dict for normalization tests."""
    return {
        "ticker": "QQQ",
        "timestamp": "2026-08-03T14:30:00+00:00",
        "quote": {
            "ticker": "QQQ",
            "bid": 450.00,
            "ask": 450.10,
            "last": 450.05,
            "volume": 50_000_000.0,
            "timestamp": "2026-08-03T14:30:00+00:00",
            "previous_close": previous_close,
        },
        "indicators": {
            "rsi_14": 45.0,
            "macd_line": 0.5,
            "macd_signal": 0.3,
            "macd_histogram": 0.2,
            "bollinger_upper": 455.0,
            "bollinger_middle": 450.0,
            "bollinger_lower": 445.0,
            "bollinger_width": 0.022,
            "ema_9": 450.5,
            "ema_21": 449.8,
            "sma_20": 450.0,
            "sma_50": 448.0,
            "vwap": 450.10,
            "ibs": 0.55,
            "atr_14": 3.5,
            "volume_sma_20": 45_000_000.0,
            "relative_volume": 1.11,
        },
        "regime": {
            "regime": "range_bound",
            "confidence": 0.75,
            "reasoning": "test",
        },
        "daily_change_pct": daily_change_pct,
        "gap_pct": gap_pct,
        "recent_candles": [],
        "daily_candles": [],
    }


def _make_snapshot(**overrides) -> MarketSnapshot:
    """Build a MarketSnapshot for strategy-level tests."""
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 8, 3, 14, 30, 0),
        quote=Quote(
            ticker="QQQ",
            bid=450.00,
            ask=450.10,
            last=450.05,
            volume=50_000_000.0,
            timestamp=datetime(2026, 8, 3, 14, 30, 0),
            previous_close=overrides.get("previous_close", 449.0),
        ),
        indicators=TechnicalIndicators(
            rsi_14=45.0,
            macd_line=0.5,
            macd_signal=0.3,
            macd_histogram=0.2,
            bollinger_upper=455.0,
            bollinger_middle=450.0,
            bollinger_lower=445.0,
            bollinger_width=0.022,
            ema_9=450.5,
            ema_21=449.8,
            sma_20=450.0,
            sma_50=448.0,
            vwap=450.10,
            ibs=0.55,
            atr_14=3.5,
            volume_sma_20=45_000_000.0,
            relative_volume=1.11,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.75,
            reasoning="test",
        ),
        daily_change_pct=overrides.get("daily_change_pct", 1.83),
        gap_pct=overrides.get("gap_pct", 0.45),
    )


# ─── Finding #1: normalize_market_snapshot preserves fields ───────────


class TestNormalizeFieldPassthrough:
    """Verify daily_change_pct, gap_pct, and previous_close survive normalisation."""

    def test_daily_change_pct_preserved(self):
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(daily_change_pct=1.83)
        result = normalize_market_snapshot(data)
        assert result["daily_change_pct"] == 1.83

    def test_gap_pct_preserved(self):
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(gap_pct=-0.72)
        result = normalize_market_snapshot(data)
        assert result["gap_pct"] == -0.72

    def test_previous_close_preserved_in_quote(self):
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(previous_close=449.0)
        result = normalize_market_snapshot(data)
        assert result["quote"]["previous_close"] == 449.0

    def test_previous_close_none_when_missing(self):
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(previous_close=None)
        result = normalize_market_snapshot(data)
        assert result["quote"]["previous_close"] is None

    def test_end_to_end_market_snapshot_validates(self):
        """Normalised dict with daily_change_pct produces a valid model."""
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(daily_change_pct=1.83, gap_pct=0.45)
        normalized = normalize_market_snapshot(data)
        snapshot = MarketSnapshot.model_validate(normalized)
        assert snapshot.daily_change_pct == 1.83
        assert snapshot.gap_pct == 0.45

    def test_last_resort_derivation_fires(self):
        """When daily_change_pct is absent, derive from last/previous_close."""
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(
            daily_change_pct=None,
            previous_close=440.0,
        )
        # last is 450.05, previous_close is 440.0
        # expected: ((450.05 - 440.0) / 440.0) * 100 ≈ 2.2841
        result = normalize_market_snapshot(data)
        assert result["daily_change_pct"] is not None
        assert abs(result["daily_change_pct"] - 2.2841) < 0.01

    def test_no_derivation_when_previous_close_missing(self):
        """Last-resort derivation skipped when previous_close unavailable."""
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict(
            daily_change_pct=None,
            previous_close=None,
        )
        result = normalize_market_snapshot(data)
        # Can't derive — should stay None
        assert result["daily_change_pct"] is None

    def test_coerce_pct_handles_string(self):
        """Numeric strings should be coerced to float."""
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict()
        data["daily_change_pct"] = "1.83"
        result = normalize_market_snapshot(data)
        assert result["daily_change_pct"] == 1.83

    def test_coerce_pct_handles_empty_string(self):
        """Empty string should resolve to None, not crash."""
        from evotrader.agents.tools import normalize_market_snapshot

        data = _base_snapshot_dict()
        data["gap_pct"] = ""
        result = normalize_market_snapshot(data)
        assert result["gap_pct"] is None


# ─── Finding #2: Quote model includes previous_close ─────────────────


class TestQuoteModelPreviousClose:
    def test_previous_close_field_exists(self):
        q = Quote(
            ticker="QQQ",
            bid=450.0,
            ask=450.1,
            last=450.05,
            volume=50_000_000.0,
            timestamp=datetime(2026, 8, 3, 14, 30, 0),
            previous_close=449.0,
        )
        assert q.previous_close == 449.0

    def test_previous_close_defaults_none(self):
        q = Quote(
            ticker="QQQ",
            bid=450.0,
            ask=450.1,
            last=450.05,
            volume=50_000_000.0,
            timestamp=datetime(2026, 8, 3, 14, 30, 0),
        )
        assert q.previous_close is None

    def test_getattr_resolves(self):
        """range_break_continuation uses getattr(quote, 'previous_close', None)."""
        q = Quote(
            ticker="QQQ",
            bid=450.0,
            ask=450.1,
            last=450.05,
            volume=50_000_000.0,
            timestamp=datetime(2026, 8, 3, 14, 30, 0),
            previous_close=449.0,
        )
        assert getattr(q, "previous_close", None) == 449.0


# ─── Finding #4: Momentum telemetry distinguishes None from false ─────


class TestMomentumDivergenceTelemetry:
    def test_day_change_available_true(self):
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        snapshot = _make_snapshot(daily_change_pct=1.83)
        signal = s.compute_signal(snapshot)
        assert signal.metadata["day_change_available"] is True
        assert signal.metadata["day_change_pct"] == 1.83
        assert "divergence_threshold_pct" in signal.metadata

    def test_day_change_available_false(self):
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        s = MomentumStrategy()
        snapshot = _make_snapshot(daily_change_pct=None)
        signal = s.compute_signal(snapshot)
        assert signal.metadata["day_change_available"] is False
        assert signal.metadata["day_change_pct"] == "N/A"
        # Divergence guard should not fire when input is None
        assert signal.metadata["divergence_applied"] is False


# ─── Finding #3: Event window units guard ─────────────────────────────


class TestEventWindowUnitsGuard:
    def test_valid_fraction_passes(self):
        from evotrader.algorithms.strategies.event_window_timing import (
            EventWindowTimingStrategy,
        )

        s = EventWindowTimingStrategy()
        errors = s.validate_parameters({"min_overextension": 0.015})
        assert not errors, f"Expected 0.015 to pass, got: {errors}"

    def test_percent_value_rejected(self):
        from evotrader.algorithms.strategies.event_window_timing import (
            EventWindowTimingStrategy,
        )

        s = EventWindowTimingStrategy()
        errors = s.validate_parameters({"min_overextension": 1.5})
        assert len(errors) == 1
        assert "FRACTION" in errors[0]
        assert "150%" in errors[0]

    def test_v020_value_passes(self):
        """v020 sets min_overextension=0.015, which must pass the guard."""
        from evotrader.algorithms.strategies.event_window_timing import (
            EventWindowTimingStrategy,
        )

        s = EventWindowTimingStrategy()
        errors = s.validate_parameters({"min_overextension": 0.015})
        assert not errors

    def test_boundary_value_passes(self):
        from evotrader.algorithms.strategies.event_window_timing import (
            EventWindowTimingStrategy,
        )

        s = EventWindowTimingStrategy()
        errors = s.validate_parameters({"min_overextension": 0.5})
        assert not errors

    def test_just_above_boundary_rejected(self):
        from evotrader.algorithms.strategies.event_window_timing import (
            EventWindowTimingStrategy,
        )

        s = EventWindowTimingStrategy()
        errors = s.validate_parameters({"min_overextension": 0.51})
        assert len(errors) == 1


# ─── Finding #5: Composite participation telemetry ────────────────────


class TestCompositeParticipation:
    def test_participation_ratio_present(self):
        """Composite metadata includes participation_ratio."""
        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        composite = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
            },
            weights={"momentum": 0.5, "mean_reversion": 0.5},
        )
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)
        assert "participation_ratio" in signal.metadata

    def test_abstaining_signals_listed(self):
        """When a sub-signal abstains, it appears in abstaining_signals."""
        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.algorithms.strategies.gap import GapStrategy
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        composite = CompositeStrategy(
            sub_strategies={"momentum": MomentumStrategy(), "gap": GapStrategy()},
            weights={"momentum": 0.5, "gap": 0.5},
        )
        # gap_pct=None should cause gap to abstain (applicable=False)
        snapshot = _make_snapshot(gap_pct=None)
        signal = composite.compute_signal(snapshot)
        assert "gap" in signal.metadata.get("abstaining_signals", "")
