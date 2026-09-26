"""A strategy with a weight but no entry in any regime's weights must not pass silently.

With regime-adaptive weighting on (the starter default), the per-regime table
replaces the flat weights, and a strategy missing from it counts as zero. A new
strategy added with only ``<name>_weight`` therefore runs, shows up as emitting
in the backtest's participation table, and never moves a single trade.
"""

from __future__ import annotations

import logging

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.models.market import MarketRegime, MarketSnapshot
from evotrader.models.signals import AlgoSignal


class _Vote(TradingAlgorithm):
    def __init__(self, label: str) -> None:
        self._label = label

    @property
    def name(self) -> str:
        return self._label

    @property
    def version(self) -> str:
        return "v001"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        return AlgoSignal(name=self._label, value=0.5, weight=1.0)


def _composite(regime_weights: dict[MarketRegime, dict[str, float]]) -> CompositeStrategy:
    return CompositeStrategy(
        sub_strategies={"old": _Vote("old"), "new": _Vote("new"), "shadow": _Vote("shadow")},
        weights={"old": 0.5, "new": 0.3, "shadow": 0.2},
        regime_adaptive=True,
        regime_weights=regime_weights,
    )


def test_warns_when_a_weighted_strategy_is_missing_from_every_regime(
    caplog: pytest.LogCaptureFixture,
) -> None:
    table = {r: {"old": 1.0, "shadow": 0.0} for r in MarketRegime}
    with caplog.at_level(logging.WARNING):
        _composite(table)
    assert "new" in caplog.text
    assert "regime_weights" in caplog.text
    # Listed at 0 on purpose (a strategy recorded but not yet voting): no warning.
    assert "shadow" not in caplog.text


def test_quiet_when_every_strategy_is_listed(caplog: pytest.LogCaptureFixture) -> None:
    table = {r: {"old": 0.7, "new": 0.3, "shadow": 0.0} for r in MarketRegime}
    with caplog.at_level(logging.WARNING):
        _composite(table)
    assert "regime_weights" not in caplog.text
