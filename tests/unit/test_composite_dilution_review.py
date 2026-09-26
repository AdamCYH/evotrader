"""Regression tests: silent channels must not dilute the ensemble.

See: data/evolution/reviews/20260908_210841_unverifiable_stop_price_and_range_bound_composite_dilution.md

Finding 3. A sub-signal that is `applicable: true` but emits ~0.0 used to sit in
the renormalization denominator, contributing nothing to the numerator while
absorbing its full share of ensemble authority. In `range_bound` this was
structural: with the observed live participation pattern, momentum would have
had to emit 0.356 to clear the 0.05 entry threshold against an observed ceiling
of 0.123, so the regime could not produce an entry at all.
"""

from __future__ import annotations

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import SIGNAL_EPSILON, CompositeStrategy
from evotrader.models.signals import AlgoSignal


class _Stub(TradingAlgorithm):
    """Emits a fixed value, with a configurable `applicable` flag."""

    def __init__(self, name: str, value: float, applicable: bool = True) -> None:
        self._name = name
        self._value = value
        self._applicable = applicable

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return f"stub {self._name}"

    def compute_signal(self, snapshot) -> AlgoSignal:
        return AlgoSignal(
            name=self._name,
            value=self._value,
            weight=1.0,
            metadata={"applicable": self._applicable},
        )

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        return None

    def validate_parameters(self, params: dict) -> list[str]:
        return []


# The live range_bound weighting, and the participation pattern observed across
# ten consecutive no-trade cycles: only momentum and mean_reversion spoke.
_WEIGHTS = {
    "momentum": 0.15,
    "mean_reversion": 0.07,
    "gap": 0.0,
    "intraday_vwap_zscore": 0.25,
    "event_window_timing": 0.04,
    "range_break_continuation": 0.18,
    "options_positioning": 0.07,
    "swing_failure_reversal": 0.12,
    "trend_persistence": 0.12,
}
_DARK = {"gap", "options_positioning", "event_window_timing"}


def _build(values: dict[str, float]) -> CompositeStrategy:
    subs = {n: _Stub(n, values.get(n, 0.0), applicable=n not in _DARK) for n in _WEIGHTS}
    return CompositeStrategy(
        sub_strategies=subs,
        weights=dict(_WEIGHTS),
        regime_adaptive=False,
        version="test",
    )


class TestSilentChannelDilution:
    def test_range_bound_pattern_now_clears_the_entry_threshold(self, sample_snapshot) -> None:
        """The exact live pattern must produce a tradeable signal.

        momentum 0.123 is the top of its observed range. Before the fix this
        composite reached 0.024 against a 0.05 threshold.
        """
        comp = _build({"momentum": 0.123, "mean_reversion": 0.10})
        value = comp.compute_detailed_signal(sample_snapshot).composite_value
        assert abs(value) >= 0.05, f"still below entry threshold: {value}"

    def test_silent_channels_do_not_shrink_the_signal(self, sample_snapshot) -> None:
        """Adding channels that emit nothing must not weaken the ones that do."""
        two_voters = _build({"momentum": 0.4, "mean_reversion": 0.3})
        loud = two_voters.compute_detailed_signal(sample_snapshot).composite_value

        # Same two voters, but now a third applicable channel sits silent.
        with_silence = _build({"momentum": 0.4, "mean_reversion": 0.3, "trend_persistence": 0.0})
        quiet = with_silence.compute_detailed_signal(sample_snapshot).composite_value
        assert abs(quiet) >= abs(loud) * 0.999, (
            f"silent channel diluted the composite: {loud} -> {quiet}"
        )

    def test_fix_is_expansionary_never_contractionary(self, sample_snapshot) -> None:
        """Excluding silence can only increase magnitude, never decrease it."""
        for m, mr in ((0.1, 0.1), (0.5, -0.2), (-0.3, -0.4), (0.05, 0.02)):
            comp = _build({"momentum": m, "mean_reversion": mr})
            v = comp.compute_detailed_signal(sample_snapshot).composite_value
            naive = m * _WEIGHTS["momentum"] + mr * _WEIGHTS["mean_reversion"]
            assert abs(v) >= abs(naive) * 0.999, f"contracted at {m},{mr}"

    def test_participation_haircut_is_still_applied(self, sample_snapshot) -> None:
        """The lone-survivor safeguard must survive the dilution fix.

        The renormalization pool excludes silent channels, but participation is
        still measured against every channel that COULD have spoken. Computing
        it over the voting set instead would make the ratio identically 1.0 and
        silently delete this protection.
        """
        one_voter = _build({"momentum": 0.5})
        v = one_voter.compute_detailed_signal(sample_snapshot).composite_value
        # 1 voter of 6 applicable = 0.167 participation, below MIN_PARTICIPATION
        # of 0.4, so the composite must be scaled down rather than speaking at
        # full strength.
        assert abs(v) < 0.5, f"no attenuation applied: {v}"

    def test_fully_silent_ensemble_yields_zero_not_a_crash(self, sample_snapshot) -> None:
        """An empty voting pool must fall back, not divide by nothing."""
        comp = _build({})
        assert comp.compute_detailed_signal(sample_snapshot).composite_value == pytest.approx(0.0)

    def test_epsilon_is_the_shared_silence_threshold(self, sample_snapshot) -> None:
        """A sub-epsilon emission counts as silence, not as a tiny opinion."""
        comp = _build({"momentum": 0.4, "mean_reversion": SIGNAL_EPSILON / 10})
        detailed = comp.compute_detailed_signal(sample_snapshot)
        # mean_reversion is below epsilon, so momentum should be the sole voter
        # and carry the signal rather than being averaged against a near-zero.
        assert abs(detailed.composite_value) > 0.0


class TestReportedPoolSizes:
    """Review 20260909_211917 finding 4.

    compute_signal used to rebuild the additive/applicable pools with its own
    comprehensions, separate from the ones compute_detailed_signal actually used.
    Editing one filter and not the other would have left participation_ratio
    quietly disagreeing with the arithmetic it describes. The counts are now
    reported by the method that used them.
    """

    def test_counts_match_the_pools_actually_used(self, sample_snapshot) -> None:
        comp = _build({"momentum": 0.4, "mean_reversion": 0.3})
        d = comp.compute_detailed_signal(sample_snapshot)
        additive = [s for s in d.signals if s.metadata.get("role") != "multiplier"]
        applicable = [s for s in additive if s.metadata.get("applicable", True)]
        voting = [s for s in applicable if abs(s.value) >= SIGNAL_EPSILON]
        assert d.n_additive == len(additive)
        assert d.n_applicable == len(applicable)
        assert d.n_voting == len(voting)

    def test_participation_ratio_derives_from_reported_counts(self, sample_snapshot) -> None:
        comp = _build({"momentum": 0.4, "mean_reversion": 0.3})
        d = comp.compute_detailed_signal(sample_snapshot)
        md = comp.compute_signal(sample_snapshot).metadata
        assert md["participation_ratio"] == pytest.approx(round(d.n_applicable / d.n_additive, 3))

    def test_voting_ratio_is_reported_separately(self, sample_snapshot) -> None:
        """Silence and inapplicability are different failures.

        participation_ratio counts channels that could vote; voting_ratio counts
        those that actually spoke. Two voters of six applicable is 0.333 — the
        value the attenuation acts on — while participation stays 6/9.
        """
        comp = _build({"momentum": 0.4, "mean_reversion": 0.3})
        d = comp.compute_detailed_signal(sample_snapshot)
        md = comp.compute_signal(sample_snapshot).metadata
        assert md["voting_ratio"] == pytest.approx(round(d.n_voting / d.n_applicable, 3))
        assert md["voting_ratio"] != md["participation_ratio"]

    def test_counts_are_consistent_orderings(self, sample_snapshot) -> None:
        comp = _build({"momentum": 0.4, "mean_reversion": 0.3})
        d = comp.compute_detailed_signal(sample_snapshot)
        assert d.n_voting <= d.n_applicable <= d.n_additive

    def test_no_voting_channels_reports_zero_not_a_crash(self, sample_snapshot) -> None:
        comp = _build({})
        d = comp.compute_detailed_signal(sample_snapshot)
        md = comp.compute_signal(sample_snapshot).metadata
        assert d.n_voting == 0
        assert md["voting_ratio"] == pytest.approx(0.0)
