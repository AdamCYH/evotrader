"""A read guarded on the vote is bucketed at its guarded depth.

Found by the evolution agent's code review. ``get_signal_calibration``'s
``by_depth`` buckets a vote-scaled channel by its emission before the scale
(``raw_value``). mean_reversion's trend guard multiplies the emission by
default, so a guarded read's ``raw_value`` is already guarded. With
``trend_guard_on_vote`` the emission is left whole and the composite applies
the guard to the vote, so ``raw_value`` is the read before the guard: the same
market read landed in a deeper bucket once a version switched the guard to the
vote, and the table mixed the two. The attribution now keeps
``trend_guard_vote_factor`` with the vote, and the depth is ``raw_value`` times
that factor when it is there. Made-up ticker and numbers.
"""

from __future__ import annotations

import pytest

from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.db.signal_attribution import channel_votes_from_sub_signals
from evotrader.evolution.tools import _depth_bucket, _depth_of
from tests.unit.test_mean_reversion_trend_guard_decay import BEAR, PARAMS, _snapshot


class TestTheDepth:
    def test_a_read_guarded_on_the_vote_is_multiplied_by_its_factor(self) -> None:
        vote = {
            "value": 0.25,
            "raw_value": 0.48,
            "vote_scale": 0.1,
            "trend_guard_vote_factor": 0.25,
        }
        depth, exact = _depth_of(vote, 0.1)
        assert (depth, exact) == (pytest.approx(0.12), True)
        assert _depth_bucket(depth, exact) == "0.05_and_over"

    def test_the_same_read_guarded_on_the_emission_lands_in_the_same_bucket(self) -> None:
        on_vote = {"value": 0.25, "raw_value": 0.16, "trend_guard_vote_factor": 0.25}
        on_emission = {"value": 0.4, "raw_value": 0.04}  # 0.16 x 0.25, the guard inside
        assert _depth_of(on_vote, 0.1)[0] == pytest.approx(_depth_of(on_emission, 0.1)[0])
        assert _depth_bucket(*_depth_of(on_vote, 0.1)) == "0.02_to_0.05"
        assert _depth_bucket(*_depth_of(on_emission, 0.1)) == "0.02_to_0.05"

    def test_a_vote_without_the_factor_is_unchanged(self) -> None:
        assert _depth_of({"value": 0.6, "raw_value": 0.06, "vote_scale": 0.1}, 0.1) == (
            pytest.approx(0.06),
            True,
        )

    def test_a_row_from_before_raw_value_is_unchanged(self) -> None:
        assert _depth_of({"value": 1.0}, 0.1) == (0.1, False)


def _composite(on_vote: bool) -> CompositeStrategy:
    mr = MeanReversionStrategy(
        **{**PARAMS, "trend_guard_decay": 0.25, "trend_guard_on_vote": on_vote}
    )
    return CompositeStrategy(
        {"mean_reversion": mr},
        {"mean_reversion": 1.0},
        regime_adaptive=False,
        vote_scales={"mean_reversion": 0.1},
    )


def _stored_vote(on_vote: bool, snap) -> dict:
    """The vote as the attribution keeps it, from the composite's sub-signals."""
    detail = _composite(on_vote).compute_detailed_signal(snap)
    subs = [
        {"name": s.name, "value": s.value, "weight": s.weight, "metadata": s.metadata}
        for s in detail.signals
    ]
    return channel_votes_from_sub_signals(subs)["mean_reversion"]


@pytest.mark.parametrize("rsi, ibs", [(26.0, 0.05), (31.0, 0.18), (22.0, 0.02)])
def test_one_read_one_bucket_whichever_way_the_guard_ran(rsi: float, ibs: float) -> None:
    snap = _snapshot(price=95.0, rsi=rsi, ibs=ibs, stack=BEAR)
    on_vote, on_emission = _stored_vote(True, snap), _stored_vote(False, snap)
    assert on_vote["trend_guard_vote_factor"] == 0.25
    assert "trend_guard_vote_factor" not in on_emission
    assert on_vote["raw_value"] == pytest.approx(on_emission["raw_value"] / 0.25)
    assert _depth_of(on_vote, 0.1)[0] == pytest.approx(_depth_of(on_emission, 0.1)[0])
    assert _depth_bucket(*_depth_of(on_vote, 0.1)) == _depth_bucket(*_depth_of(on_emission, 0.1))
