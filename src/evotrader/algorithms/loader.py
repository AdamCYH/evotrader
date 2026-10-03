"""Dynamic strategy loader from manifest."""

from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import Any

import yaml

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.models.market import MarketRegime

logger = logging.getLogger(__name__)


class StrategyLoader:
    """Loads and instantiates trading strategies dynamically using a manifest file.

    Enables adding, removing, and changing the ensemble of strategies
    without modifying core infrastructure code.
    """

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path

    def _load_manifest_data(self) -> dict[str, Any]:
        """Load manifest from disk."""
        if not self.manifest_path.is_file():
            logger.warning(
                "Manifest file not found at %s. Returning empty strategies list.",
                self.manifest_path,
            )
            return {"strategies": {}}
        try:
            with open(self.manifest_path) as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {"strategies": {}}
        except Exception as e:
            logger.error("Failed to parse strategy manifest: %s", e)
            return {"strategies": {}}

    def discover_strategies(self) -> dict[str, type[TradingAlgorithm]]:
        """Discover and load all strategy classes defined in the manifest.

        Returns:
            Dict mapping strategy name to class type.
        """
        data = self._load_manifest_data()
        strategies_data = data.get("strategies", {})
        discovered: dict[str, type[TradingAlgorithm]] = {}

        for name, config in strategies_data.items():
            module_name = config.get("module")
            class_name = config.get("class")
            if not module_name or not class_name:
                logger.warning("Strategy '%s' is missing module or class name configuration.", name)
                continue

            try:
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                if not issubclass(cls, TradingAlgorithm):
                    logger.error(
                        "Strategy class %s in %s does not extend TradingAlgorithm",
                        class_name,
                        module_name,
                    )
                    continue
                discovered[name] = cls
            except (ImportError, AttributeError) as e:
                logger.error(
                    "Failed to load strategy '%s' (%s.%s): %s", name, module_name, class_name, e
                )

        return discovered

    def get_active_strategies(self) -> dict[str, type[TradingAlgorithm]]:
        """Return only active strategy classes from the manifest.

        Returns:
            Dict mapping strategy name to active class type.
        """
        data = self._load_manifest_data()
        strategies_data = data.get("strategies", {})
        discovered = self.discover_strategies()
        active: dict[str, type[TradingAlgorithm]] = {}

        for name, cls in discovered.items():
            config = strategies_data.get(name, {})
            if config.get("status") == "active":
                active[name] = cls

        return active

    def validate_manifest(self) -> list[str]:
        """Validate the manifest file structure and class definitions.

        Returns:
            List of validation errors (empty if valid).
        """
        errors: list[str] = []
        if not self.manifest_path.is_file():
            errors.append(f"Manifest file not found at {self.manifest_path}")
            return errors

        data = self._load_manifest_data()
        strategies_data = data.get("strategies")
        if strategies_data is None:
            errors.append("Manifest is missing top-level 'strategies' key.")
            return errors

        if not isinstance(strategies_data, dict):
            errors.append("'strategies' key in manifest must be a dictionary.")
            return errors

        for name, config in strategies_data.items():
            if not isinstance(config, dict):
                errors.append(f"Strategy configuration for '{name}' must be a dictionary.")
                continue

            module_name = config.get("module")
            class_name = config.get("class")

            if not module_name:
                errors.append(f"Strategy '{name}' is missing 'module' field.")
            if not class_name:
                errors.append(f"Strategy '{name}' is missing 'class' field.")

            if module_name and class_name:
                try:
                    module = importlib.import_module(module_name)
                    cls = getattr(module, class_name)
                    if not issubclass(cls, TradingAlgorithm):
                        errors.append(
                            f"Strategy class '{module_name}.{class_name}' for '{name}' "
                            f"does not inherit from TradingAlgorithm."
                        )
                except ImportError as e:
                    errors.append(
                        f"Could not import module '{module_name}' for strategy '{name}': {e}"
                    )
                except AttributeError:
                    errors.append(
                        f"Module '{module_name}' has no class '{class_name}' for strategy '{name}'."
                    )

        return errors

    @staticmethod
    def _filter_params(
        name: str, cls: type[TradingAlgorithm], strat_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop config keys the strategy no longer accepts.

        Archived version configs are frozen snapshots, but strategy
        signatures keep evolving — ``MeanReversionStrategy`` dropped
        ``vwap_weight``, which made every config from v005..v016 raise
        TypeError and silently disappear from ``--compare``. A stale key
        is a back-compat problem, not a reason to lose the version.
        """
        try:
            sig = inspect.signature(cls.__init__)
        except (TypeError, ValueError):
            return dict(strat_params)

        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return dict(strat_params)

        accepted = {
            p_name
            for p_name, p in sig.parameters.items()
            if p_name != "self"
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        kept = {k: v for k, v in strat_params.items() if k in accepted}
        dropped = sorted(set(strat_params) - accepted)
        if dropped:
            logger.warning(
                "Strategy '%s': dropping %d config key(s) no longer accepted by %s: %s. "
                "The strategy will use its current defaults for these.",
                name,
                len(dropped),
                cls.__name__,
                ", ".join(dropped),
            )
        return kept

    def build_composite(
        self, params: dict[str, Any], active_version: str | None = None
    ) -> CompositeStrategy:
        """Build and return a CompositeStrategy instance using loaded parameters."""
        active_classes = self.get_active_strategies()
        sub_strategies = {}

        for name, cls in active_classes.items():
            strat_params = params.get(name, {})
            sub_strategies[name] = cls(**self._filter_params(name, cls, strat_params))

        composite_config = params.get("composite", {})

        # Extract weights from composite config
        weights = {}
        for name in sub_strategies:
            weight_val = composite_config.get(f"{name}_weight")
            if weight_val is not None:
                weights[name] = float(weight_val)
            else:
                # Keep in sync with composite.py global defaults.
                default_weights = {
                    "momentum": 0.20,
                    "mean_reversion": 0.18,
                    "gap": 0.07,
                    "intraday_vwap_zscore": 0.14,
                    "event_window_timing": 0.06,
                    "range_break_continuation": 0.13,
                    "options_positioning": 0.10,
                    "swing_failure_reversal": 0.12,
                    "trend_persistence": 0.0,
                    # Shadow channels: a version whose config predates them
                    # runs them at weight 0 (recorded, not voting).
                    "vwap_reclaim_continuation": 0.0,
                    "gap_fail_continuation": 0.0,
                }
                weights[name] = default_weights.get(name, 1.0 / len(sub_strategies))

        regime_adaptive = composite_config.get("regime_adaptive", True)
        renormalize_on_abstain = composite_config.get("renormalize_on_abstain", True)
        raw_inverted = composite_config.get("inverted") or []
        inverted = {str(x) for x in raw_inverted} if isinstance(raw_inverted, list) else set()

        # Parse regime_weights from config if present, mapping string keys
        # (e.g. "trending_bull") to MarketRegime enum members.
        regime_weights = None
        raw_regime_weights = composite_config.get("regime_weights")
        if raw_regime_weights and isinstance(raw_regime_weights, dict):
            regime_weights = {}
            for regime_key, weight_dict in raw_regime_weights.items():
                try:
                    regime_enum = MarketRegime(regime_key)
                except ValueError:
                    logger.warning(
                        "Unknown regime '%s' in config regime_weights, skipping.",
                        regime_key,
                    )
                    continue
                if isinstance(weight_dict, dict):
                    regime_weights[regime_enum] = {k: float(v) for k, v in weight_dict.items()}

        resolved_version = active_version or composite_config.get("version") or "v001"
        return CompositeStrategy(
            sub_strategies=sub_strategies,
            weights=weights,
            regime_adaptive=regime_adaptive,
            version=resolved_version,
            regime_weights=regime_weights,
            renormalize_on_abstain=renormalize_on_abstain,
            inverted_strategies=inverted,
        )
