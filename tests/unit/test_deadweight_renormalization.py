"""Regression tests: deadweight dilution renormalization and gap signal lifecycle.

Guards against silent composite signal attenuation from non-applicable
sub-strategies (gap emitting 0.0 with gap_type='none') and verifies
gap intraday decay and fill-fraction scaling.

See: data/evolution/reviews/20260713_210454_composite_deadweight_dilution_and_gap_signal_lifecycle.md
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.gap import GapStrategy
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal
from evotrader.tools.market_hours import minutes_since_open

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConstantStrategy(TradingAlgorithm):
    """Test double that returns a fixed signal with configurable applicability."""

    def __init__(
        self,
        name: str,
        signal_value: float,
        applicable: bool = True,
    ) -> None:
        self._name = name
        self._signal_value = signal_value
        self._applicable = applicable

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "v001"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        return AlgoSignal(
            name=self.name,
            value=self._signal_value,
            weight=1.0,
            metadata={"applicable": self._applicable},
        )


def _make_snapshot(
    *,
    gap_pct: float | None = 0.0,
    daily_change_pct: float | None = 0.0,
    regime: MarketRegime = MarketRegime.RANGE_BOUND,
    timestamp: datetime | None = None,
) -> MarketSnapshot:
    """Build a minimal MarketSnapshot for testing."""
    ts = timestamp or datetime(2026, 7, 10, 10, 30)  # 1h after open
    return MarketSnapshot(
        ticker="SPY",
        timestamp=ts,
        quote=Quote(
            ticker="SPY",
            bid=450.0,
            ask=450.02,
            last=450.01,
            volume=50e6,
            timestamp=ts,
        ),
        indicators=TechnicalIndicators(),
        regime=RegimeClassification(
            regime=regime,
            confidence=0.8,
            reasoning="test",
        ),
        gap_pct=gap_pct,
        daily_change_pct=daily_change_pct,
    )


# ===========================================================================
# 1. Composite renormalization
# ===========================================================================


class TestCompositeRenormalization:
    """Verify that the composite excludes abstaining strategies and rescales."""

    def test_renormalizes_when_one_strategy_abstains(self) -> None:
        """With gap abstaining, composite should only average momentum + MR."""
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": _ConstantStrategy("momentum", -0.5),
                "mean_rev": _ConstantStrategy("mean_rev", -0.3),
                "gap": _ConstantStrategy("gap", 0.0, applicable=False),
            },
            weights={"momentum": 0.4, "mean_rev": 0.35, "gap": 0.25},
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        # Expected: (0.4*-0.5 + 0.35*-0.3) / (0.4+0.35) = -0.305 / 0.75 ≈ -0.4067
        assert signal.value == pytest.approx(-0.4067, abs=0.01)
        assert signal.metadata.get("renormalized") is True

    def test_no_renormalization_when_all_applicable(self) -> None:
        """All strategies applicable → standard weighted average, no renormalization."""
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": _ConstantStrategy("momentum", 0.6),
                "mean_rev": _ConstantStrategy("mean_rev", 0.2),
                "gap": _ConstantStrategy("gap", -0.1, applicable=True),
            },
            weights={"momentum": 0.4, "mean_rev": 0.35, "gap": 0.25},
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        expected = 0.4 * 0.6 + 0.35 * 0.2 + 0.25 * (-0.1)
        assert signal.value == pytest.approx(expected, abs=0.001)

    def test_renormalize_disabled_preserves_old_behavior(self) -> None:
        """renormalize_on_abstain=False → include zero-signal at full weight."""
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": _ConstantStrategy("momentum", -0.5),
                "mean_rev": _ConstantStrategy("mean_rev", -0.3),
                "gap": _ConstantStrategy("gap", 0.0, applicable=False),
            },
            weights={"momentum": 0.4, "mean_rev": 0.35, "gap": 0.25},
            regime_adaptive=False,
            renormalize_on_abstain=False,
        )
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        # Old behavior: 0.4*-0.5 + 0.35*-0.3 + 0.25*0.0 = -0.305
        expected = 0.4 * (-0.5) + 0.35 * (-0.3) + 0.25 * 0.0
        assert signal.value == pytest.approx(expected, abs=0.001)

    def test_compute_signal_matches_detailed_signal(self) -> None:
        """compute_signal and compute_detailed_signal must agree on composite_value."""
        composite = CompositeStrategy(
            sub_strategies={
                "a": _ConstantStrategy("a", 0.8),
                "b": _ConstantStrategy("b", -0.4, applicable=False),
            },
            weights={"a": 0.6, "b": 0.4},
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_snapshot()

        simple = composite.compute_signal(snapshot)
        detailed = composite.compute_detailed_signal(snapshot)

        assert simple.value == pytest.approx(detailed.composite_value, abs=1e-9)

    def test_all_abstain_returns_zero(self) -> None:
        """If every strategy abstains, composite should return 0.0."""
        composite = CompositeStrategy(
            sub_strategies={
                "a": _ConstantStrategy("a", 0.0, applicable=False),
                "b": _ConstantStrategy("b", 0.0, applicable=False),
            },
            weights={"a": 0.5, "b": 0.5},
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_snapshot()
        signal = composite.compute_signal(snapshot)

        # No applicable signals → falls through to standard path (all zeros)
        assert signal.value == pytest.approx(0.0)

    def test_detailed_signal_renormalized_flag(self) -> None:
        """CompositeAlgoSignal.renormalized is set correctly."""
        composite = CompositeStrategy(
            sub_strategies={
                "a": _ConstantStrategy("a", 0.5),
                "b": _ConstantStrategy("b", 0.0, applicable=False),
            },
            weights={"a": 0.6, "b": 0.4},
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_snapshot()
        detailed = composite.compute_detailed_signal(snapshot)
        assert detailed.renormalized is True

        # All applicable → not renormalized
        composite2 = CompositeStrategy(
            sub_strategies={
                "a": _ConstantStrategy("a", 0.5),
                "b": _ConstantStrategy("b", 0.3),
            },
            weights={"a": 0.6, "b": 0.4},
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        detailed2 = composite2.compute_detailed_signal(snapshot)
        assert detailed2.renormalized is False


# ===========================================================================
# 2. Gap strategy lifecycle
# ===========================================================================


class TestGapApplicable:
    """Gap strategy marks no-gap cycles as not applicable."""

    def test_no_gap_is_not_applicable(self) -> None:
        strategy = GapStrategy()
        snapshot = _make_snapshot(gap_pct=0.1)  # Below default min_gap_pct=0.3
        signal = strategy.compute_signal(snapshot)

        assert signal.value == 0.0
        assert signal.metadata["gap_type"] == "none"
        assert signal.metadata["applicable"] is False

    def test_significant_gap_is_applicable(self) -> None:
        strategy = GapStrategy()
        snapshot = _make_snapshot(gap_pct=-1.5, daily_change_pct=-1.0)
        signal = strategy.compute_signal(snapshot)

        assert signal.metadata["gap_type"] == "fade"
        assert signal.metadata["applicable"] is True


class TestGapFillFraction:
    """Continuous fill-fraction scaling replaces the binary 0.5 dampener."""

    def test_fully_filled_gap_produces_zero_signal(self) -> None:
        """When daily_change retraces the full gap, signal should be ~0."""
        strategy = GapStrategy(decay_minutes=9999)  # Disable time decay for this test
        # Gap up 1.5%, daily change also +1.5% (full fill)
        snapshot = _make_snapshot(
            gap_pct=1.5,
            daily_change_pct=0.0,
            timestamp=datetime(2026, 7, 10, 9, 30),  # exactly at open
        )
        signal = strategy.compute_signal(snapshot)
        # fill_progress = (1.5 - 0.0)/1.5 = 1.0, remaining = 0.0
        assert signal.value == pytest.approx(0.0)

    def test_unfilled_gap_full_signal(self) -> None:
        """No fill progress → full signal strength."""
        strategy = GapStrategy(decay_minutes=9999)
        # Gap up 1.5%, daily_change = gap_pct (price still at open, no fill)
        snapshot = _make_snapshot(
            gap_pct=1.5,
            daily_change_pct=1.5,
            timestamp=datetime(2026, 7, 10, 9, 30),
        )
        signal = strategy.compute_signal(snapshot)

        # fill_progress = (0.015 - 0.015)/0.015 = 0, remaining = 1.0
        assert signal.metadata["fill_remaining"] == pytest.approx(1.0)
        # Fade signal should be negative (fading the gap-up)
        assert signal.value < 0

    def test_half_filled_gap(self) -> None:
        """Half fill → signal scaled by ~0.5."""
        strategy = GapStrategy(decay_minutes=9999)
        # Gap up 2%, daily change = 1% (half the gap has filled)
        snapshot = _make_snapshot(
            gap_pct=2.0,
            daily_change_pct=1.0,
            timestamp=datetime(2026, 7, 10, 9, 30),
        )
        signal = strategy.compute_signal(snapshot)

        # fill_progress = (0.02 - 0.01)/0.02 = 0.5, remaining = 0.5
        assert signal.metadata["fill_remaining"] == pytest.approx(0.5)

    def test_gap_down_fill_fraction(self) -> None:
        """Fill fraction works correctly for gap-downs too."""
        strategy = GapStrategy(decay_minutes=9999)
        # Gap down 1.5%, daily change = -0.5% (partially filled)
        snapshot = _make_snapshot(
            gap_pct=-1.5,
            daily_change_pct=-0.5,
            timestamp=datetime(2026, 7, 10, 9, 30),
        )
        signal = strategy.compute_signal(snapshot)

        # fill_progress = (-0.015 - (-0.005)) / -0.015 = -0.01 / -0.015 = 0.667
        # remaining = 1 - 0.667 = 0.333
        assert signal.metadata["fill_remaining"] == pytest.approx(0.333, abs=0.01)
        # Fade signal should be positive (fading the gap-down = buy)
        assert signal.value > 0


class TestGapIntradayDecay:
    """Linear session-time decay of gap signal."""

    def test_signal_at_open(self) -> None:
        """At 9:30 AM ET (market open), signal is at full strength."""
        strategy = GapStrategy(decay_minutes=120)
        snapshot = _make_snapshot(
            gap_pct=-1.5,
            daily_change_pct=-1.5,  # No fill yet
            timestamp=datetime(2026, 7, 10, 9, 30),
        )
        signal = strategy.compute_signal(snapshot)
        assert signal.metadata["decay_factor"] == pytest.approx(1.0)
        assert abs(signal.value) > 0

    def test_signal_at_half_decay(self) -> None:
        """At 10:30 AM ET (60 min into session), signal halved."""
        strategy = GapStrategy(decay_minutes=120)
        snapshot = _make_snapshot(
            gap_pct=-1.5,
            daily_change_pct=-1.5,
            timestamp=datetime(2026, 7, 10, 10, 30),
        )
        signal = strategy.compute_signal(snapshot)
        assert signal.metadata["decay_factor"] == pytest.approx(0.5)

    def test_signal_at_full_decay(self) -> None:
        """At 11:30 AM ET (120 min), signal decayed to zero."""
        strategy = GapStrategy(decay_minutes=120)
        snapshot = _make_snapshot(
            gap_pct=-1.5,
            daily_change_pct=-1.5,
            timestamp=datetime(2026, 7, 10, 11, 30),
        )
        signal = strategy.compute_signal(snapshot)
        assert signal.metadata["decay_factor"] == pytest.approx(0.0)
        assert signal.value == pytest.approx(0.0)

    def test_signal_after_full_decay(self) -> None:
        """After 120 min, signal stays at zero (clamped)."""
        strategy = GapStrategy(decay_minutes=120)
        snapshot = _make_snapshot(
            gap_pct=-1.5,
            daily_change_pct=-1.5,
            timestamp=datetime(2026, 7, 10, 14, 0),  # 4.5 hours after open
        )
        signal = strategy.compute_signal(snapshot)
        assert signal.metadata["decay_factor"] == pytest.approx(0.0)
        assert signal.value == pytest.approx(0.0)


# ===========================================================================
# 3. Shared market_hours utility
# ===========================================================================


class TestMinutesSinceOpen:
    """Verify the shared minutes_since_open helper."""

    def test_at_open(self) -> None:
        result = minutes_since_open(datetime(2026, 7, 10, 9, 30))
        assert result == pytest.approx(0.0)

    def test_one_hour_after_open(self) -> None:
        result = minutes_since_open(datetime(2026, 7, 10, 10, 30))
        assert result == pytest.approx(60.0)

    def test_before_open_returns_none(self) -> None:
        result = minutes_since_open(datetime(2026, 7, 10, 8, 0))
        assert result is None

    def test_at_close(self) -> None:
        result = minutes_since_open(datetime(2026, 7, 10, 16, 0))
        assert result == pytest.approx(390.0)


# ===========================================================================
# 4. Gap parameter surface cleanup
# ===========================================================================


class TestGapParameterSurface:
    """Verify opening_range_minutes removed and decay_minutes added."""

    def test_get_parameters_includes_decay_minutes(self) -> None:
        strategy = GapStrategy(decay_minutes=90)
        params = strategy.get_parameters()
        assert params["decay_minutes"] == 90.0
        assert "opening_range_minutes" not in params

    def test_set_parameters_decay_minutes(self) -> None:
        strategy = GapStrategy()
        strategy.set_parameters({"decay_minutes": 60})
        assert strategy.get_parameters()["decay_minutes"] == 60.0

    def test_validate_decay_minutes(self) -> None:
        strategy = GapStrategy()
        errors = strategy.validate_parameters({"decay_minutes": -10})
        assert any("decay_minutes" in e for e in errors)

    def test_old_opening_range_minutes_kwarg_accepted(self) -> None:
        """Backward compat: old configs passing opening_range_minutes don't crash."""
        strategy = GapStrategy(opening_range_minutes=30)
        assert strategy.name == "gap"  # Just verify it doesn't raise
