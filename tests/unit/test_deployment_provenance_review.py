"""Regression tests: engine build provenance and off-duty channel accounting.

See: data/evolution/reviews/20260910_212711_deployed_engine_predates_composite_renormalization_fix.md

The CRITICAL finding was not a source defect at all — the repo was already
correct and the *deployed process* was two days stale, renormalizing over the
`applicable` pool while this source had renormalized over `voting` since
2026-09-08. It took three sessions to notice, because the only way to tell the
two builds apart was to reconstruct a live cycle's arithmetic by hand and match
it to nine significant figures.

These tests cover the parts of that review that live in source:

  F1  build provenance is emitted, so the running engine is identifiable
      from a log line instead of by reverse-engineering its numbers
  F2  channels that are structurally unable to fire in this regime stop
      being counted as absent witnesses
  F3  the volume dampener floor's clamp is countable
  F4  MAX_AMPLIFICATION's influence is observable, including the case
      where it silently does nothing

The redeploy itself is operational and cannot be asserted here.
"""

from __future__ import annotations

import re

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import (
    COMPOSITE_SOURCE_FINGERPRINT,
    CompositeStrategy,
)
from evotrader.models.signals import AlgoSignal


class _Stub(TradingAlgorithm):
    """Emits a fixed value, with configurable `applicable` / `in_scope`."""

    def __init__(
        self,
        name: str,
        value: float,
        applicable: bool = True,
        in_scope: bool | None = None,
    ) -> None:
        self._name = name
        self._value = value
        self._applicable = applicable
        self._in_scope = in_scope

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
        meta: dict = {"applicable": self._applicable}
        if self._in_scope is not None:
            meta["in_scope"] = self._in_scope
        return AlgoSignal(name=self._name, value=self._value, weight=1.0, metadata=meta)

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        return None

    def validate_parameters(self, params: dict) -> list[str]:
        return []


def _build(
    weights: dict[str, float],
    values: dict[str, float],
    dark: set[str] | None = None,
    off_duty: set[str] | None = None,
) -> CompositeStrategy:
    dark = dark or set()
    off_duty = off_duty or set()
    subs = {
        n: _Stub(
            n,
            values.get(n, 0.0),
            applicable=n not in dark,
            in_scope=False if n in off_duty else None,
        )
        for n in weights
    }
    return CompositeStrategy(
        sub_strategies=subs,
        weights=dict(weights),
        regime_adaptive=False,
        version="test",
    )


# The 2026-09-10 range_bound ensemble: six applicable channels, of which
# range_break_continuation and trend_persistence were 0-for-18 structurally.
_LIVE_WEIGHTS = {
    "momentum": 0.22,
    "mean_reversion": 0.12,
    "intraday_vwap_zscore": 0.30,
    "range_break_continuation": 0.10,
    "swing_failure_reversal": 0.12,
    "trend_persistence": 0.06,
    "gap": 0.0,
    "options_positioning": 0.04,
    "event_window_timing": 0.04,
}
_DARK = {"gap", "options_positioning", "event_window_timing"}


class TestBuildProvenance:
    """F1. The engine that produced a number must be identifiable from it."""

    def test_fingerprint_is_a_short_stable_hash(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{12}", COMPOSITE_SOURCE_FINGERPRINT), (
            f"expected 12 hex chars, got {COMPOSITE_SOURCE_FINGERPRINT!r}"
        )

    def test_cycle_metadata_carries_the_fingerprint(self, sample_snapshot) -> None:
        comp = _build(_LIVE_WEIGHTS, {"momentum": 0.2}, dark=_DARK)
        md = comp.compute_signal(sample_snapshot).metadata
        assert md["composite_source_fingerprint"] == COMPOSITE_SOURCE_FINGERPRINT

    def test_renormalization_pool_is_self_describing(self, sample_snapshot) -> None:
        """The pre-fix build renormalized over `applicable` and could never
        report "voting". That makes this field a one-glance build check."""
        comp = _build(_LIVE_WEIGHTS, {"momentum": 0.2}, dark=_DARK)
        md = comp.compute_signal(sample_snapshot).metadata
        assert md["renormalization_pool"] == "voting"

    def test_pool_reads_applicable_when_nobody_votes(self, sample_snapshot) -> None:
        comp = _build(_LIVE_WEIGHTS, {}, dark=_DARK)
        md = comp.compute_signal(sample_snapshot).metadata
        assert md["renormalization_pool"] == "applicable"


class TestOffDutyChannelsAreNotAbsentWitnesses:
    """F2. 'Off duty' and 'looked and found nothing' must be distinguishable."""

    def test_off_duty_channels_leave_the_participation_denominator(self, sample_snapshot) -> None:
        """Cycle 8's shape: two of six voted, and the 0.333 participation
        triggered a 0.833x haircut. Two of those six could not have fired at
        all, so the real participation was 2/4 and no haircut was earned."""
        values = {"momentum": -0.05, "mean_reversion": 0.03}
        counted = _build(_LIVE_WEIGHTS, values, dark=_DARK)
        excused = _build(
            _LIVE_WEIGHTS,
            values,
            dark=_DARK,
            off_duty={"range_break_continuation", "trend_persistence"},
        )
        assert abs(excused.compute_detailed_signal(sample_snapshot).composite_value) > abs(
            counted.compute_detailed_signal(sample_snapshot).composite_value
        ), "excusing off-duty channels must lift the unearned attenuation"

    def test_haircut_is_removed_not_merely_reduced(self, sample_snapshot) -> None:
        """2 of 4 in-scope clears MIN_PARTICIPATION (0.4), so the composite
        must equal the un-attenuated renormalized value exactly."""
        values = {"momentum": -0.05, "mean_reversion": 0.03}
        excused = _build(
            _LIVE_WEIGHTS,
            values,
            dark=_DARK,
            off_duty={"range_break_continuation", "trend_persistence"},
        )
        detailed = excused.compute_detailed_signal(sample_snapshot)
        assert detailed.n_in_scope == 4
        assert detailed.n_voting == 2
        # 2/4 = 0.5 >= 0.4, so no attenuation factor was applied.
        pool_w = _LIVE_WEIGHTS["momentum"] + _LIVE_WEIGHTS["mean_reversion"]
        numerator = (
            values["momentum"] * _LIVE_WEIGHTS["momentum"]
            + values["mean_reversion"] * _LIVE_WEIGHTS["mean_reversion"]
        )
        assert detailed.composite_value == pytest.approx(numerator / pool_w, abs=1e-9)

    def test_a_voting_channel_is_never_excused(self, sample_snapshot) -> None:
        """Participation must never exceed 1.0 even if a strategy emits a
        contradictory pair (in_scope False while actually voting)."""
        comp = _build(
            _LIVE_WEIGHTS,
            {"momentum": -0.05, "range_break_continuation": 0.4},
            dark=_DARK,
            off_duty={"range_break_continuation"},
        )
        detailed = comp.compute_detailed_signal(sample_snapshot)
        assert detailed.n_voting <= detailed.n_in_scope

    def test_off_duty_channels_are_reported(self, sample_snapshot) -> None:
        comp = _build(
            _LIVE_WEIGHTS,
            {"momentum": -0.05, "mean_reversion": 0.03},
            dark=_DARK,
            off_duty={"range_break_continuation", "trend_persistence"},
        )
        md = comp.compute_signal(sample_snapshot).metadata
        assert "range_break_continuation" in md["off_duty_signals"]
        assert "trend_persistence" in md["off_duty_signals"]
        assert md["in_scope_count"] == 4

    def test_unmarked_silence_still_counts_against_participation(self, sample_snapshot) -> None:
        """The lone-survivor safeguard must survive this change: a channel that
        is simply quiet is still an absent witness."""
        comp = _build(_LIVE_WEIGHTS, {"momentum": -0.05}, dark=_DARK)
        detailed = comp.compute_detailed_signal(sample_snapshot)
        assert detailed.n_in_scope == 6
        assert detailed.n_voting == 1


class TestScopeMarkersOnTheChannelsThemselves:
    """F2, strategy side. The markers must be set only when preconditions
    are genuinely absent — never to excuse a channel that simply declined."""

    def test_range_break_is_out_of_scope_inside_the_bands(self, sample_snapshot) -> None:
        from evotrader.algorithms.strategies.range_break_continuation import (
            RangeBreakContinuationStrategy,
        )

        snap = sample_snapshot.model_copy(deep=True)
        ind = snap.indicators
        # Price parked well inside both bands — the first gate is unreachable.
        ind.bollinger_upper = snap.quote.last + 10.0
        ind.bollinger_lower = snap.quote.last - 10.0
        md = RangeBreakContinuationStrategy().compute_signal(snap).metadata
        if md.get("applicable", True):
            assert md.get("in_scope") is False
            assert md["out_of_scope_reason"] == "price_inside_bands"

    def test_range_break_stays_in_scope_outside_the_bands(self, sample_snapshot) -> None:
        """Outside a band the channel IS on duty — even if it declines to fire
        because MACD or magnitude did not confirm. That is a real abstention."""
        from evotrader.algorithms.strategies.range_break_continuation import (
            RangeBreakContinuationStrategy,
        )

        snap = sample_snapshot.model_copy(deep=True)
        ind = snap.indicators
        ind.bollinger_upper = snap.quote.last - 5.0
        ind.bollinger_lower = snap.quote.last - 20.0
        md = RangeBreakContinuationStrategy().compute_signal(snap).metadata
        assert md.get("in_scope", True) is True


class TestVolumeFloorIsCountable:
    """F3. A floor that binds every cycle makes the curve beneath it
    unobservable — the entry_z/min_std failure mode in a second place."""

    def test_repo_volume_signal_is_the_recentred_one(self) -> None:
        """Second, independent probe of the deployment gap.

        The live engine reported volume_multiplier_raw 0.24651333 at
        relative_volume 0.86977. That is exactly the OLD single linear map
        (rvol - 0.5) / 1.5. This source implements the recentred two-segment
        map and must return ~0.37 — a different file and a different code
        path reaching the same conclusion as the CRITICAL finding.
        """
        from evotrader.indicators.volume import volume_signal

        assert volume_signal(0.86977) == pytest.approx(0.36977, abs=1e-5)
        assert volume_signal(0.86977) != pytest.approx(0.24651333, abs=1e-5)
        # The documented anchors.
        assert volume_signal(0.5) == pytest.approx(0.0)
        assert volume_signal(1.0) == pytest.approx(0.5)
        assert volume_signal(2.0) == pytest.approx(1.0)

    def test_floor_binding_is_emitted_when_clamped(self, sample_snapshot) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        snap = sample_snapshot.model_copy(deep=True)
        snap.indicators.relative_volume = 0.86977  # raw 0.370 < floor 0.7
        md = MomentumStrategy(volume_dampener_floor=0.7).compute_signal(snap).metadata
        assert md["volume_floor_binding"] is True
        assert md["volume_multiplier_applied"] == pytest.approx(0.7)

    def test_floor_not_binding_on_high_volume(self, sample_snapshot) -> None:
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        snap = sample_snapshot.model_copy(deep=True)
        snap.indicators.relative_volume = 1.8  # raw 0.9 > floor
        md = MomentumStrategy(volume_dampener_floor=0.7).compute_signal(snap).metadata
        assert md["volume_floor_binding"] is False

    def test_no_false_clamp_when_confirmation_disabled(self, sample_snapshot) -> None:
        """The 0.5 placeholder on the disabled path is below the 0.7 floor but
        nothing is multiplied, so reporting a clamp there would be a lie."""
        from evotrader.algorithms.strategies.momentum import MomentumStrategy

        md = (
            MomentumStrategy(volume_confirmation=False, volume_dampener_floor=0.7)
            .compute_signal(sample_snapshot)
            .metadata
        )
        assert md["volume_floor_binding"] is False


class TestAmplificationCapIsObservable:
    """F4. Applied before normalisation, the cap divides out when it binds on
    every survivor — a guard that silently does nothing."""

    def test_cap_binding_on_all_survivors_is_a_no_op(self, sample_snapshot) -> None:
        """Cycle 8 under repo code: momentum 0.22 and mean_reversion 0.12,
        both capped, final weights identical to the uncapped ratio."""
        weights = {**_LIVE_WEIGHTS}
        values = {"momentum": -0.05, "mean_reversion": 0.03}
        comp = _build(
            weights, values, dark=_DARK, off_duty={"range_break_continuation", "trend_persistence"}
        )
        detailed = comp.compute_detailed_signal(sample_snapshot)
        assert set(detailed.amplification_capped) == {"momentum", "mean_reversion"}

        # Uncapped renormalization gives the same answer, which is the point.
        w_total = weights["momentum"] + weights["mean_reversion"]
        uncapped = (
            values["momentum"] * weights["momentum"]
            + values["mean_reversion"] * weights["mean_reversion"]
        ) / w_total
        assert detailed.composite_value == pytest.approx(uncapped, abs=1e-9)

    def test_cap_is_empty_when_it_does_not_bind(self, sample_snapshot) -> None:
        """With a large pool, raw_eff stays under weight * 2.5 for everyone."""
        comp = _build(
            _LIVE_WEIGHTS,
            {n: 0.2 for n in _LIVE_WEIGHTS if n not in _DARK},
            dark=_DARK,
        )
        detailed = comp.compute_detailed_signal(sample_snapshot)
        assert detailed.amplification_capped == []

    def test_capped_names_reach_cycle_metadata(self, sample_snapshot) -> None:
        comp = _build(
            _LIVE_WEIGHTS,
            {"momentum": -0.05, "mean_reversion": 0.03},
            dark=_DARK,
        )
        md = comp.compute_signal(sample_snapshot).metadata
        assert "momentum" in md["amplification_capped"]
