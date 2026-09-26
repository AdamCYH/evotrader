"""Unit tests for the dynamic StrategyLoader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evotrader.algorithms.loader import StrategyLoader
from evotrader.algorithms.strategies.gap import GapStrategy
from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.algorithms.strategies.momentum import MomentumStrategy


@pytest.fixture
def temp_manifest(tmp_path: Path) -> Path:
    """Creates a temporary manifest file with standard strategies."""
    manifest_data = {
        "strategies": {
            "momentum": {
                "module": "evotrader.algorithms.strategies.momentum",
                "class": "MomentumStrategy",
                "description": "EMA crossover strategy",
                "status": "active",
                "added_in": "v001",
            },
            "mean_reversion": {
                "module": "evotrader.algorithms.strategies.mean_reversion",
                "class": "MeanReversionStrategy",
                "description": "RSI mean reversion",
                "status": "active",
                "added_in": "v001",
            },
            "gap": {
                "module": "evotrader.algorithms.strategies.gap",
                "class": "GapStrategy",
                "description": "Gap strategy",
                "status": "disabled",
                "added_in": "v001",
            },
            "invalid_class": {
                "module": "evotrader.algorithms.strategies.momentum",
                "class": "NonExistentClass",
                "description": "Invalid strategy class reference",
                "status": "active",
                "added_in": "v001",
            },
        }
    }
    manifest_file = tmp_path / "strategy_manifest.yaml"
    with open(manifest_file, "w") as f:
        yaml.dump(manifest_data, f)
    return manifest_file


def test_discover_strategies(temp_manifest: Path) -> None:
    loader = StrategyLoader(temp_manifest)
    discovered = loader.discover_strategies()

    # Gap Strategy is valid class, so it should be discovered regardless of status
    assert "momentum" in discovered
    assert "mean_reversion" in discovered
    assert "gap" in discovered
    # Invalid class should not be in discovered
    assert "invalid_class" not in discovered

    assert discovered["momentum"] == MomentumStrategy
    assert discovered["mean_reversion"] == MeanReversionStrategy
    assert discovered["gap"] == GapStrategy


def test_get_active_strategies(temp_manifest: Path) -> None:
    loader = StrategyLoader(temp_manifest)
    active = loader.get_active_strategies()

    # Only active & successfully loaded strategies
    assert "momentum" in active
    assert "mean_reversion" in active
    assert "gap" not in active  # status: disabled
    assert "invalid_class" not in active  # status: active but invalid class


def test_validate_manifest_with_errors(temp_manifest: Path) -> None:
    loader = StrategyLoader(temp_manifest)
    errors = loader.validate_manifest()

    assert len(errors) == 1
    assert "NonExistentClass" in errors[0]


def test_validate_manifest_valid(tmp_path: Path) -> None:
    manifest_data = {
        "strategies": {
            "momentum": {
                "module": "evotrader.algorithms.strategies.momentum",
                "class": "MomentumStrategy",
                "status": "active",
            }
        }
    }
    manifest_file = tmp_path / "valid_manifest.yaml"
    with open(manifest_file, "w") as f:
        yaml.dump(manifest_data, f)

    loader = StrategyLoader(manifest_file)
    errors = loader.validate_manifest()
    assert len(errors) == 0


def test_build_composite(temp_manifest: Path) -> None:
    loader = StrategyLoader(temp_manifest)
    params = {
        "momentum": {"ema_short": 10},
        "mean_reversion": {"rsi_period": 15},
        "gap": {"min_gap_pct": 0.5},
        "composite": {"momentum_weight": 0.3, "mean_reversion_weight": 0.5, "gap_weight": 0.2},
    }
    composite = loader.build_composite(params)
    assert composite.version == "v001"
    assert composite._weights["momentum"] == pytest.approx(0.375)
    assert composite._weights["mean_reversion"] == pytest.approx(0.625)
    assert "gap" not in composite._weights


def test_build_composite_with_regime_weights(temp_manifest: Path) -> None:
    """Regression: regime_weights in config must be passed through to CompositeStrategy.

    See: data/evolution/reviews/20260629_024841_stale_vwap_narrative_in_algo_metadata_after_indicator_fix.md
    """
    from evotrader.models.market import MarketRegime

    params = {
        "momentum": {"ema_short": 10},
        "mean_reversion": {"rsi_period": 15},
        "composite": {
            "momentum_weight": 0.4,
            "mean_reversion_weight": 0.6,
            "regime_adaptive": True,
            "regime_weights": {
                "high_volatility": {
                    "momentum": 0.55,
                    "mean_reversion": 0.45,
                },
                "range_bound": {
                    "momentum": 0.20,
                    "mean_reversion": 0.80,
                },
            },
        },
    }
    composite = loader_build(temp_manifest, params)

    # Regime weights should be present and normalized
    hv = composite._regime_weights_map[MarketRegime.HIGH_VOLATILITY]
    assert hv["momentum"] == pytest.approx(0.55)
    assert hv["mean_reversion"] == pytest.approx(0.45)

    rb = composite._regime_weights_map[MarketRegime.RANGE_BOUND]
    assert rb["momentum"] == pytest.approx(0.20)
    assert rb["mean_reversion"] == pytest.approx(0.80)


def test_build_composite_unknown_regime_skipped(temp_manifest: Path) -> None:
    """Unknown regime keys in config should be gracefully skipped."""
    from evotrader.models.market import MarketRegime

    params = {
        "momentum": {"ema_short": 10},
        "mean_reversion": {"rsi_period": 15},
        "composite": {
            "momentum_weight": 0.5,
            "mean_reversion_weight": 0.5,
            "regime_weights": {
                "high_volatility": {
                    "momentum": 0.55,
                    "mean_reversion": 0.45,
                },
                "nonexistent_regime": {
                    "momentum": 0.5,
                    "mean_reversion": 0.5,
                },
            },
        },
    }
    composite = loader_build(temp_manifest, params)

    # Valid regime should be present
    assert MarketRegime.HIGH_VOLATILITY in composite._regime_weights_map
    # Invalid regime should not crash or appear
    for key in composite._regime_weights_map:
        assert isinstance(key, MarketRegime)


def loader_build(temp_manifest: Path, params: dict):
    """Helper to build composite from temp manifest and params."""
    loader = StrategyLoader(temp_manifest)
    return loader.build_composite(params)


def test_build_composite_active_version_propagated(temp_manifest: Path) -> None:
    """Regression: algo_version must reflect the active registry version, not 'v001'.

    See: data/evolution/reviews/20260701_210221_regime_direction_logic_and_algo_version_label_bugs.md
    """
    loader = StrategyLoader(temp_manifest)
    params = {"composite": {}}

    # active_version explicitly passed should be used
    composite = loader.build_composite(params, active_version="v004_highvol_momentum_tilt")
    assert composite.version == "v004_highvol_momentum_tilt"

    # Falls back to config version key if active_version not provided
    params_with_version = {"composite": {"version": "v002_from_config"}}
    composite = loader.build_composite(params_with_version)
    assert composite.version == "v002_from_config"

    # active_version takes precedence over config version
    composite = loader.build_composite(params_with_version, active_version="v005_override")
    assert composite.version == "v005_override"

    # Without either, falls back to "v001"
    composite = loader.build_composite({"composite": {}})
    assert composite.version == "v001"


def test_build_composite_default_weights_match_composite(temp_manifest: Path) -> None:
    """Regression: fallback weight defaults must match loader.py defaults.

    See: data/evolution/reviews/20260701_210221_regime_direction_logic_and_algo_version_label_bugs.md
    """
    loader = StrategyLoader(temp_manifest)
    # Empty composite config → no weights provided, triggers fallback defaults
    params = {"composite": {}}
    composite = loader.build_composite(params)

    # temp_manifest has momentum & mean_reversion active (gap is disabled).
    # Fallback defaults: momentum=0.20, mean_reversion=0.18
    # After normalization (0.20+0.18=0.38):
    assert composite._weights["momentum"] == pytest.approx(0.20 / 0.38)
    assert composite._weights["mean_reversion"] == pytest.approx(0.18 / 0.38)
