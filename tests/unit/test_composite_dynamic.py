"""Tests for dynamic behavior in CompositeStrategy."""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal


class MockSimpleStrategy(TradingAlgorithm):
    """A simple mock strategy that returns a constant signal."""

    def __init__(self, name: str, signal_value: float, version: str = "v001") -> None:
        self._name = name
        self._signal_value = signal_value
        self._version = version

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        return AlgoSignal(name=self.name, value=self._signal_value, weight=1.0)

    def get_parameters(self) -> dict:
        return {"signal_value": self._signal_value}

    def set_parameters(self, params: dict) -> None:
        if "signal_value" in params:
            self._signal_value = params["signal_value"]


@pytest.fixture
def base_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        ticker="SPY",
        timestamp=datetime(2026, 6, 18, 14, 30),
        quote=Quote(
            ticker="SPY",
            bid=100.0,
            ask=100.0,
            last=100.0,
            volume=1e6,
            timestamp=datetime(2026, 6, 18, 14, 30),
        ),
        indicators=TechnicalIndicators(),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=1.0,
            reasoning="mock",
        ),
        daily_change_pct=0.0,
        gap_pct=0.0,
    )


def test_composite_dynamic_initialization() -> None:
    sub_strategies = {
        "strat_a": MockSimpleStrategy("strat_a", 0.5),
        "strat_b": MockSimpleStrategy("strat_b", -0.2),
    }
    weights = {"strat_a": 0.7, "strat_b": 0.3}

    composite = CompositeStrategy(
        sub_strategies=sub_strategies,
        weights=weights,
        regime_adaptive=False,
    )

    assert composite.version == "v001"
    assert "strat_a(70%)" in composite.description
    assert "strat_b(30%)" in composite.description


def test_composite_dynamic_weight_normalization() -> None:
    sub_strategies = {
        "strat_a": MockSimpleStrategy("strat_a", 0.5),
        "strat_b": MockSimpleStrategy("strat_b", -0.2),
    }
    # Weights do not sum to 1.0 (0.8 + 0.8 = 1.6)
    weights = {"strat_a": 0.8, "strat_b": 0.8}

    composite = CompositeStrategy(
        sub_strategies=sub_strategies,
        weights=weights,
        regime_adaptive=False,
    )

    # Should normalize weights to 0.5 each
    assert composite._weights["strat_a"] == pytest.approx(0.5)
    assert composite._weights["strat_b"] == pytest.approx(0.5)


def test_composite_dynamic_signal_computation(base_snapshot: MarketSnapshot) -> None:
    sub_strategies = {
        "strat_a": MockSimpleStrategy("strat_a", 0.8),
        "strat_b": MockSimpleStrategy("strat_b", -0.4),
    }
    weights = {"strat_a": 0.7, "strat_b": 0.3}

    composite = CompositeStrategy(
        sub_strategies=sub_strategies,
        weights=weights,
        regime_adaptive=False,
    )

    signal = composite.compute_signal(base_snapshot)

    # Expected value = 0.7 * 0.8 + 0.3 * (-0.4) = 0.56 - 0.12 = 0.44
    assert signal.value == pytest.approx(0.44)
    assert signal.metadata["strat_a_signal"] == 0.8
    assert signal.metadata["strat_b_signal"] == -0.4
    assert signal.metadata["strat_a_weight"] == pytest.approx(0.7)
    assert signal.metadata["strat_b_weight"] == pytest.approx(0.3)


def test_composite_dynamic_detailed_signal(base_snapshot: MarketSnapshot) -> None:
    sub_strategies = {
        "strat_a": MockSimpleStrategy("strat_a", 0.8),
        "strat_b": MockSimpleStrategy("strat_b", -0.4),
    }
    weights = {"strat_a": 0.7, "strat_b": 0.3}

    composite = CompositeStrategy(
        sub_strategies=sub_strategies,
        weights=weights,
        regime_adaptive=False,
    )

    detailed = composite.compute_detailed_signal(base_snapshot)

    assert detailed.composite_value == pytest.approx(0.44)
    assert len(detailed.signals) == 2
    assert detailed.signals[0].name == "strat_a"
    assert detailed.signals[0].value == 0.8
    assert detailed.signals[0].weight == pytest.approx(0.7)
    assert detailed.signals[1].name == "strat_b"
    assert detailed.signals[1].value == -0.4
    assert detailed.signals[1].weight == pytest.approx(0.3)


def test_composite_dynamic_parameters() -> None:
    sub_strategies = {
        "strat_a": MockSimpleStrategy("strat_a", 0.8),
        "strat_b": MockSimpleStrategy("strat_b", -0.4),
    }
    weights = {"strat_a": 0.7, "strat_b": 0.3}

    composite = CompositeStrategy(
        sub_strategies=sub_strategies,
        weights=weights,
        regime_adaptive=False,
    )

    params = composite.get_parameters()
    assert params["regime_adaptive"] is False
    assert params["strat_a_weight"] == pytest.approx(0.7)
    assert params["strat_b_weight"] == pytest.approx(0.3)
    assert params["strat_a"] == {"signal_value": 0.8}
    assert params["strat_b"] == {"signal_value": -0.4}

    # Set parameter test
    composite.set_parameters(
        {
            "strat_a_weight": 0.6,
            "strat_a": {"signal_value": 0.9},
        }
    )
    new_params = composite.get_parameters()
    assert new_params["strat_a_weight"] == pytest.approx(0.6)
    assert new_params["strat_a"] == {"signal_value": 0.9}


def test_composite_dynamic_validation() -> None:
    sub_strategies = {
        "strat_a": MockSimpleStrategy("strat_a", 0.8),
        "strat_b": MockSimpleStrategy("strat_b", -0.4),
    }
    weights = {"strat_a": 0.7, "strat_b": 0.3}

    composite = CompositeStrategy(
        sub_strategies=sub_strategies,
        weights=weights,
        regime_adaptive=False,
    )

    # Valid parameters check (should sum to 1.0)
    errors = composite.validate_parameters(
        {
            "strat_a_weight": 0.6,
            "strat_b_weight": 0.4,
        }
    )
    assert len(errors) == 0

    # Invalid parameters check (does not sum to 1.0)
    errors = composite.validate_parameters(
        {
            "strat_a_weight": 0.6,
            "strat_b_weight": 0.2,
        }
    )
    assert len(errors) == 1
    assert "weights must sum to 1.0" in errors[0]


def test_default_high_volatility_regime_weights() -> None:
    """Verify HIGH_VOLATILITY defaults match 8-strategy ensemble.

    See: data/evolution/reviews/20260701_171516_regime_detector_highvol_masks_trend_direction.md
    Updated to 8-strategy ensemble with swing_failure_reversal (v017).
    """
    defaults = CompositeStrategy._get_default_regime_weights_map()
    hv = defaults[MarketRegime.HIGH_VOLATILITY]

    assert hv["momentum"] == pytest.approx(0.47)
    assert hv["mean_reversion"] == pytest.approx(0.13)
    assert hv["gap"] == pytest.approx(0.17)
    assert hv["intraday_vwap_zscore"] == pytest.approx(0.10)
    assert hv["event_window_timing"] == pytest.approx(0.03)
    assert hv["range_break_continuation"] == pytest.approx(0.0)
    assert hv["options_positioning"] == pytest.approx(0.10)
    assert hv["swing_failure_reversal"] == pytest.approx(0.0)

    # All regime weights should sum to 1.0
    for regime, weights in defaults.items():
        total = sum(weights.values())
        assert total == pytest.approx(1.0), f"{regime} weights sum to {total}, expected 1.0"
