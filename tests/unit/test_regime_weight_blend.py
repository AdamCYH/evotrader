"""A low-confidence regime tag no longer picks the call's author on its own.

Found by the evolution agent's code review. Two things let the regime tag flip
inside a session and, on a pool of two daily channels, decide which of them
authored the composite while neither changed its read:

* the tag picks a whole weight vector, and the range-bound and trending
  vectors order the two daily channels oppositely, so a tag read at under 0.5
  confidence chose the author on a coin flip. ``composite.regime_weight_blend:
  confidence`` blends the tag's vector with the flat weights by the tag's
  confidence, c x regime + (1 - c) x flat;
* live, the daily series ends with today's bar still forming, and the
  regime's volatility test read it: the forming bar's true range grows through
  the session, so the test changed its answer as the day's range printed.
  ``composite.regime_volatility_on_completed_bars: true`` reads it on the
  completed bars, as the backtest always has.

Both are off unless an algorithm version sets them. Made-up ticker and prices.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.indicators.regime import detect_regime_rule_based
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal

NOW = datetime(2026, 3, 6, 15, 30, tzinfo=UTC)


class Fixed(TradingAlgorithm):
    def __init__(self, name: str, value: float) -> None:
        self._name, self._value = name, value

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "v001"

    @property
    def description(self) -> str:
        return "fixed"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        return AlgoSignal(name=self._name, value=self._value, weight=1.0, metadata={})

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        pass

    def validate_parameters(self, params: dict) -> list:
        return []


def _snapshot(regime: MarketRegime, confidence: float) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(ticker="XYZ", bid=99.99, ask=100.01, last=100.0, volume=1e5, timestamp=NOW),
        indicators=TechnicalIndicators(),
        regime=RegimeClassification(regime=regime, confidence=confidence, reasoning="fixture"),
    )


# A range-bound vector that favours "trend", a trending one that favours
# "revert", and flat weights that favour "revert" a little.
FLAT = {"trend": 0.4, "revert": 0.6}
REGIMES = {
    MarketRegime.RANGE_BOUND: {"trend": 0.8, "revert": 0.2},
    MarketRegime.TRENDING_BEAR: {"trend": 0.2, "revert": 0.8},
    MarketRegime.TRENDING_BULL: {"trend": 0.2, "revert": 0.8},
    MarketRegime.HIGH_VOLATILITY: {"trend": 0.5, "revert": 0.5},
}


def _composite(**kw) -> CompositeStrategy:
    return CompositeStrategy(
        sub_strategies={"trend": Fixed("trend", -0.2), "revert": Fixed("revert", 0.15)},
        weights=FLAT,
        regime_weights=REGIMES,
        **kw,
    )


class TestTheBlend:
    def test_off_the_tag_picks_the_vector(self) -> None:
        comp = _composite()
        assert comp._get_regime_weights(MarketRegime.RANGE_BOUND, 0.44) == pytest.approx(
            {"trend": 0.8, "revert": 0.2}
        )
        assert (
            "regime_weight_blend"
            not in comp.compute_signal(_snapshot(MarketRegime.RANGE_BOUND, 0.44)).metadata
        )

    def test_on_it_blends_by_confidence(self) -> None:
        comp = _composite(regime_weight_blend="confidence")
        w = comp._get_regime_weights(MarketRegime.RANGE_BOUND, 0.44)
        assert w["trend"] == pytest.approx(0.44 * 0.8 + 0.56 * 0.4)
        assert w["revert"] == pytest.approx(0.44 * 0.2 + 0.56 * 0.6)
        assert sum(w.values()) == pytest.approx(1.0)
        full = comp._get_regime_weights(MarketRegime.RANGE_BOUND, 1.0)
        assert full == pytest.approx({"trend": 0.8, "revert": 0.2})
        assert comp._get_regime_weights(MarketRegime.RANGE_BOUND, 0.0) == pytest.approx(FLAT)

    def test_a_coin_flip_tag_moves_the_weights_less_than_halfway(self) -> None:
        """Under 0.5 confidence every weight stays nearer the flat one than the tag's."""
        comp = _composite(regime_weight_blend="confidence")
        for regime, vector in REGIMES.items():
            blended = comp._get_regime_weights(regime, 0.45)
            for name, flat in FLAT.items():
                assert abs(blended[name] - flat) <= abs(blended[name] - vector[name]) + 1e-12

    def test_the_tag_alone_flipped_the_sign_and_the_blend_damps_it(self) -> None:
        plain, blended = _composite(), _composite(regime_weight_blend="confidence")

        def spread(comp: CompositeStrategy) -> float:
            return abs(
                comp.compute_signal(_snapshot(MarketRegime.RANGE_BOUND, 0.45)).value
                - comp.compute_signal(_snapshot(MarketRegime.TRENDING_BEAR, 0.45)).value
            )

        plain_values = [
            plain.compute_signal(_snapshot(r, 0.45)).value
            for r in (MarketRegime.RANGE_BOUND, MarketRegime.TRENDING_BEAR)
        ]
        assert plain_values[0] < 0 < plain_values[1], "the same reads, opposite calls"
        assert spread(blended) == pytest.approx(0.45 * spread(plain))

    def test_the_blend_is_recorded(self) -> None:
        sig = _composite(regime_weight_blend="confidence").compute_signal(
            _snapshot(MarketRegime.TRENDING_BEAR, 0.47)
        )
        assert sig.metadata["regime_weight_blend"] == 0.47

    def test_not_regime_adaptive_ignores_it(self) -> None:
        comp = _composite(regime_weight_blend="confidence", regime_adaptive=False)
        assert comp._get_regime_weights(MarketRegime.RANGE_BOUND, 0.44) == pytest.approx(FLAT)

    def test_configuration(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert _composite(regime_weight_blend="hysteresis")._regime_weight_blend is None
        assert "not 'confidence'" in caplog.text
        comp = _composite()
        comp.set_parameters({"regime_weight_blend": "confidence"})
        assert comp.get_parameters()["regime_weight_blend"] == "confidence"
        assert "regime_weight_blend" not in _composite().get_parameters()

    def test_the_loader_reads_it(self, tmp_path) -> None:
        from evotrader.algorithms.loader import StrategyLoader

        manifest = tmp_path / "strategy_manifest.yaml"
        manifest.write_text(
            "strategies:\n"
            "  momentum:\n"
            "    module: evotrader.algorithms.strategies.momentum\n"
            "    class: MomentumStrategy\n"
            "    status: active\n"
        )
        loader = StrategyLoader(manifest)
        config: dict[str, Any] = {"composite": {"momentum_weight": 1.0}}
        assert loader.build_composite(config)._regime_weight_blend is None
        config["composite"]["regime_weight_blend"] = "confidence"
        assert loader.build_composite(config)._regime_weight_blend == "confidence"


# ── The regime's volatility test on completed bars ───────────────────────


def _series(forming_range: float) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Seventy sessions, quiet then volatile, and a forming bar with the given range."""
    rng = np.random.default_rng(4)
    closes, highs, lows = [], [], []
    price = 100.0
    for i in range(70):
        span = 1.0 if i < 55 else 4.0  # the last fifteen sessions swing wide
        price += rng.normal(0, 0.3)
        closes.append(price)
        highs.append(price + span / 2)
        lows.append(price - span / 2)
    closes.append(price)
    highs.append(price + forming_range / 2)
    lows.append(price - forming_range / 2)
    n = len(closes)
    return (
        pd.Series(closes),
        pd.Series(highs),
        pd.Series(lows),
        pd.Series([1e6] * n),
    )


class TestCompletedBarVolatility:
    def test_a_forming_bars_small_range_drags_the_percentile_down(self) -> None:
        close, high, low, volume = _series(forming_range=0.5)  # early in the session
        live = detect_regime_rule_based(close, high, low, volume)
        completed = detect_regime_rule_based(
            close, high, low, volume, volatility_on_completed_bars=True
        )
        assert live.volatility_percentile < completed.volatility_percentile
        assert completed.volatility_percentile >= 75.0

    def test_completed_bars_do_not_move_with_the_forming_range(self) -> None:
        readings = {
            detect_regime_rule_based(
                *_series(forming_range=r), volatility_on_completed_bars=True
            ).volatility_percentile
            for r in (0.5, 2.0, 6.0)
        }
        assert len(readings) == 1, "the day's range so far no longer moves the test"

    def test_off_by_default(self) -> None:
        close, high, low, volume = _series(forming_range=0.5)
        default = detect_regime_rule_based(close, high, low, volume)
        explicit = detect_regime_rule_based(
            close, high, low, volume, volatility_on_completed_bars=False
        )
        assert default.volatility_percentile == explicit.volatility_percentile

    def test_compute_indicators_applies_it_only_to_a_forming_bar(self) -> None:
        import json

        from evotrader.agents.tools import compute_indicators

        close, high, low, volume = _series(forming_range=0.5)
        candles = json.dumps(
            [
                {"open": c, "high": h, "low": lo, "close": c, "volume": v}
                for c, h, lo, v in zip(close, high, low, volume, strict=True)
            ]
        )

        def pct(**kw) -> float:
            return compute_indicators(candles, **kw)["regime"]["volatility_percentile"]

        forming_off = pct(forming_last_bar=True)
        forming_on = pct(forming_last_bar=True, regime_volatility_on_completed_bars=True)
        completed_on = pct(forming_last_bar=False, regime_volatility_on_completed_bars=True)
        assert forming_on > forming_off
        assert completed_on == pct(), "with every bar completed there is nothing to drop"

    def test_the_market_data_tool_reads_the_active_version(self, monkeypatch) -> None:
        import evotrader.agents.tools as tools

        class Registry:
            def __init__(self, params: dict) -> None:
                self._params = params

            def load_parameters(self) -> dict:
                return self._params

        monkeypatch.setattr(tools, "_algo_registry", Registry({"composite": {}}))
        assert tools._regime_volatility_on_completed_bars() is False
        on = {"composite": {"regime_volatility_on_completed_bars": True}}
        monkeypatch.setattr(tools, "_algo_registry", Registry(on))
        assert tools._regime_volatility_on_completed_bars() is True
        monkeypatch.setattr(tools, "_algo_registry", None)
        assert tools._regime_volatility_on_completed_bars() is False
