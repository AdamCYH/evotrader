"""composite.vote_scales: a channel's full-vote magnitude.

The composite is a weighted mean of raw votes, and the call's author is the
largest weight x value. Votes are not on one scale: a mean-reversion read is a
few hundredths at mid-RSI by construction, a momentum read a few tenths, so
weight alone could not give mean reversion the say its weight implied, and
momentum authored nearly every call. ``composite.vote_scales`` divides a
channel's emission by its scale and clips it to +/-1 before aggregation; the
raw emission is kept in the vote's metadata (``raw_value``, ``vote_scale``).
Absent, every composite is what it was. Made-up ticker and prices.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from typing import Any

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.loader import StrategyLoader
from evotrader.db.signal_attribution import algo_call_from_snapshot
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal

NOW = datetime(2026, 3, 5, 15, 30, tzinfo=UTC)
SNAP = MarketSnapshot(
    ticker="XYZ",
    timestamp=NOW,
    quote=Quote(ticker="XYZ", bid=99.99, ask=100.01, last=100.0, volume=1e5, timestamp=NOW),
    indicators=TechnicalIndicators(),
    regime=RegimeClassification(
        regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="fixture"
    ),
)


class Fixed(TradingAlgorithm):
    def __init__(self, name: str, value: float, **meta: Any) -> None:
        self._name, self._value, self._meta = name, value, meta

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
        return AlgoSignal(name=self._name, value=self._value, weight=1.0, metadata=dict(self._meta))

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        pass

    def validate_parameters(self, params: dict) -> list:
        return []


def _composite(mom: float, mr: float, **kw) -> CompositeStrategy:
    return CompositeStrategy(
        sub_strategies={
            "momentum": Fixed("momentum", mom),
            "mean_reversion": Fixed("mean_reversion", mr),
        },
        weights={"momentum": 0.4, "mean_reversion": 0.6},
        regime_adaptive=False,
        **kw,
    )


def _detail(comp: CompositeStrategy):
    d = comp.compute_detailed_signal(SNAP)
    return d, {s.name: s for s in d.signals}


class TestAbsentIsTheOldArithmetic:
    def test_random_composites_are_unchanged(self) -> None:
        rng = random.Random(3)
        for _ in range(300):
            mom, mr = rng.uniform(-0.6, 0.6), rng.uniform(-0.05, 0.05)
            plain = _composite(mom, mr).compute_detailed_signal(SNAP).composite_value
            ones = _composite(mom, mr, vote_scales={"mean_reversion": 1.0})
            assert ones.compute_detailed_signal(SNAP).composite_value == plain

    def test_no_new_metadata_and_no_new_parameter(self) -> None:
        comp = _composite(-0.2, 0.02)
        _, votes = _detail(comp)
        assert "vote_scale" not in votes["mean_reversion"].metadata
        assert "vote_scales" not in comp.get_parameters()


class TestAScaledVote:
    def test_the_vote_is_divided_by_its_scale(self) -> None:
        d, votes = _detail(_composite(-0.2, 0.02, vote_scales={"mean_reversion": 0.1}))
        mr = votes["mean_reversion"]
        assert mr.value == pytest.approx(0.2)
        assert (mr.metadata["vote_scale"], mr.metadata["raw_value"]) == (0.1, 0.02)
        # (0.4 x -0.2 + 0.6 x 0.2) / 1.0: the weighted mean of the scaled votes.
        assert d.composite_value == pytest.approx(0.04)

    def test_it_is_clipped_at_a_full_vote(self) -> None:
        _, votes = _detail(_composite(-0.2, 0.3, vote_scales={"mean_reversion": 0.1}))
        assert votes["mean_reversion"].value == 1.0
        assert votes["mean_reversion"].metadata["raw_value"] == 0.3

    def test_a_read_under_the_vote_threshold_stays_a_non_vote(self) -> None:
        _, votes = _detail(_composite(-0.2, 0.0005, vote_scales={"mean_reversion": 0.1}))
        assert votes["mean_reversion"].value == 0.0005
        assert "vote_scale" not in votes["mean_reversion"].metadata

    def test_the_author_follows_the_scaled_vote(self) -> None:
        for scales, author in (({}, "momentum"), ({"mean_reversion": 0.1}, "mean_reversion")):
            d, _ = _detail(_composite(-0.2, 0.02, vote_scales=scales))
            subs = [
                {"name": s.name, "value": s.value, "weight": s.weight, "metadata": s.metadata}
                for s in d.signals
            ]
            assert algo_call_from_snapshot(d.composite_value, subs)[1] == author

    def test_an_inverted_channel_is_flipped_then_scaled(self) -> None:
        comp = _composite(
            -0.2, 0.02, vote_scales={"mean_reversion": 0.1}, inverted_strategies={"mean_reversion"}
        )
        _, votes = _detail(comp)
        assert votes["mean_reversion"].value == pytest.approx(-0.2)
        assert votes["mean_reversion"].metadata["raw_value"] == 0.02


class TestConfiguration:
    def test_out_of_range_scales_are_dropped(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            comp = _composite(
                -0.2, 0.02, vote_scales={"mean_reversion": 0.0, "momentum": 1.5, "gap": "x"}
            )
        assert comp.vote_scales == {}
        assert caplog.text.count("outside (0, 1]") == 3

    def test_a_scale_for_an_absent_channel_is_named(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            _composite(-0.2, 0.02, vote_scales={"keltner": 0.5})
        assert "not in this composite: keltner" in caplog.text

    def test_parameters_round_trip_and_the_description_shows_it(self) -> None:
        comp = _composite(-0.2, 0.02)
        comp.set_parameters({"vote_scales": {"mean_reversion": 0.1}})
        assert comp.get_parameters()["vote_scales"] == {"mean_reversion": 0.1}
        assert "mean_reversion(60%, s0.1)" in comp.description

    def test_the_loader_reads_it_from_the_version_config(self, tmp_path) -> None:
        manifest = tmp_path / "strategy_manifest.yaml"
        manifest.write_text(
            "strategies:\n"
            "  momentum:\n"
            "    module: evotrader.algorithms.strategies.momentum\n"
            "    class: MomentumStrategy\n"
            "    status: active\n"
            "  mean_reversion:\n"
            "    module: evotrader.algorithms.strategies.mean_reversion\n"
            "    class: MeanReversionStrategy\n"
            "    status: active\n"
        )
        loader = StrategyLoader(manifest)
        base = {"composite": {"momentum_weight": 0.5, "mean_reversion_weight": 0.5}}
        assert loader.build_composite(base).vote_scales == {}
        scaled = {"composite": {**base["composite"], "vote_scales": {"mean_reversion": 0.1}}}
        assert loader.build_composite(scaled).vote_scales == {"mean_reversion": 0.1}
