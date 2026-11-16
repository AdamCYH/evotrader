"""mean_reversion's trend guard can apply to its vote, after the vote scale.

Found by the evolution agent's code review. The guard multiplies a
counter-trend read inside a confirmed MA stack by ``trend_guard_decay``. It
does so to the emission, and the composite then divides that by the channel's
vote scale and clips to +/-1: under a 0.1 scale any guarded read of 0.1 or
more was still a full vote, so on the deeper reads the guard was written for
it changed nothing. ``trend_guard_on_vote`` leaves the emission whole and has the
composite multiply the vote by the guard after the clip. Off by default: every
existing version votes as before. Made-up ticker and prices.
"""

from __future__ import annotations

import random

import pytest

from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from tests.unit.test_mean_reversion_trend_guard_decay import BEAR, BULL, PARAMS, _snapshot

# An oversold dip in a bear stack: RSI and IBS read a buy against the trend.
DIP = _snapshot(price=95.0, rsi=26.0, ibs=0.05, stack=BEAR)
NO_STACK = _snapshot(price=95.0, rsi=26.0, ibs=0.05, stack={})


def _composite(**kw) -> CompositeStrategy:
    mr = MeanReversionStrategy(**{**PARAMS, "trend_guard_decay": 0.5, **kw})
    return CompositeStrategy(
        {"mean_reversion": mr},
        {"mean_reversion": 1.0},
        regime_adaptive=False,
        vote_scales={"mean_reversion": 0.1},
    )


def _mr_vote(composite: CompositeStrategy, snap):
    detail = composite.compute_detailed_signal(snap)
    return next(s for s in detail.signals if s.name == "mean_reversion")


class TestTheDefect:
    def test_on_the_emission_a_deep_guarded_read_is_still_a_full_vote(self) -> None:
        vote = _mr_vote(_composite(), DIP)
        assert vote.metadata["trend_guard_applied"] is True
        assert vote.metadata["raw_value"] >= 0.10
        assert vote.value == 1.0


class TestOnTheVote:
    def test_the_guarded_vote_is_the_factor_of_a_full_vote(self) -> None:
        vote = _mr_vote(_composite(trend_guard_on_vote=True), DIP)
        assert vote.metadata["trend_guard_applied"] is True
        assert vote.metadata["pre_guard_vote"] == 1.0
        assert vote.value == pytest.approx(0.5)

    def test_the_emission_is_left_whole(self) -> None:
        on = MeanReversionStrategy(
            **{**PARAMS, "trend_guard_decay": 0.5, "trend_guard_on_vote": True}
        )
        off = MeanReversionStrategy(**{**PARAMS, "trend_guard_decay": 0.5})
        assert on.compute_signal(DIP).value == pytest.approx(off.compute_signal(DIP).value / 0.5)
        assert on.compute_signal(DIP).metadata["trend_guard_vote_factor"] == 0.5

    def test_a_read_with_no_stack_is_unchanged(self) -> None:
        on = _mr_vote(_composite(trend_guard_on_vote=True), NO_STACK)
        off = _mr_vote(_composite(), NO_STACK)
        assert on.metadata["trend_guard_applied"] is False
        assert "pre_guard_vote" not in on.metadata
        assert on.value == off.value

    def test_off_every_vote_is_as_before(self) -> None:
        rng = random.Random(11)
        for _ in range(200):
            snap = _snapshot(
                price=rng.uniform(92, 108),
                rsi=rng.uniform(15, 85),
                ibs=rng.uniform(0, 1),
                stack=rng.choice([BEAR, BULL, {}]),
            )
            default = _mr_vote(_composite(), snap)
            explicit = _mr_vote(_composite(trend_guard_on_vote=False), snap)
            assert default.value == explicit.value
            assert "pre_guard_vote" not in default.metadata
            assert "trend_guard_vote_factor" not in default.metadata


def test_the_parameter_round_trips() -> None:
    strat = MeanReversionStrategy()
    assert strat.get_parameters()["trend_guard_on_vote"] is False
    strat.set_parameters({"trend_guard_on_vote": True})
    assert strat.get_parameters()["trend_guard_on_vote"] is True
