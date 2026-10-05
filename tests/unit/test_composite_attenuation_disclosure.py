"""The composite is a function of the hour, so it must say what the hour did.

See: data/evolution/reviews/
20260925_210715_record_trade_trusts_executor_fill_claims_phantom_stop_fills
_and_cost_basis_rebase.md
(finding 5)

2026-09-25, MSTR, identical channel votes from 08:30 to 17:00 ET — momentum
+0.544..+0.559, mean_reversion -0.028..-0.046, everything else silent. The
composite read:

    08:30 ET   +0.3800   voting 3 / in_scope 4  -> no attenuation
    10:30 ET   +0.3218   voting 3 / in_scope 6  -> no attenuation
    11:30-15:30 +0.2297  voting 2 / in_scope 7  -> scaled by (2/7)/0.4 = 0.714
    17:00 ET   +0.3231   intraday channels out of scope again

The low-participation attenuation divides by how many channels COULD speak, and
that count changes with the clock: intraday channels are out of scope pre-market
and after-hours. So the strategy add rule — "composite higher than at entry AND
participation the same or better" — was evaluated eight times against a
pre-market entry composite of 0.380 and could not pass during regular hours no
matter what the market did. The agent explained each refusal as a price echo.
The cause was arithmetic.

The traded value does not change. What changes is that the number now arrives
with the factor that was applied to it, so a comparison across hours can be made
on like terms.

2026-10-05 (review 20261005_211602): the seven channels in scope in the
regular-hours fixture include the starter algorithm's two shadow channels, gap
and vwap_reclaim_continuation (weight 0.0 in every regime). Counted over the
channels that carry weight, the same votes read 2 of 5 and are not scaled, so
the regular-hours read equals the after-hours one; with the shadows counted
they read 2 of 7, the 0.2297 above. The disclosure stays: it is exercised
below on a thinner pool, where the scale does apply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

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


class _Fixed(TradingAlgorithm):
    """A channel that emits exactly what the live snapshot recorded."""

    def __init__(self, name: str, value: float, *, in_scope: bool = True) -> None:
        self._name = name
        self._value = value
        self._in_scope = in_scope

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "live"

    @property
    def description(self) -> str:
        return f"{self._name} pinned to its 2026-09-25 reading"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        return AlgoSignal(
            name=self._name,
            value=self._value,
            weight=1.0,
            metadata={"in_scope": self._in_scope},
        )

    def get_parameters(self) -> dict:
        return {}

    def set_parameters(self, params: dict) -> None:
        return None

    def validate_parameters(self, params: dict) -> list[str]:
        return []


def _snapshot() -> MarketSnapshot:
    now = datetime(2026, 9, 25, 15, 30, tzinfo=UTC)
    px = 158.57
    return MarketSnapshot(
        ticker="MSTR",
        timestamp=now,
        quote=Quote(
            ticker="MSTR", bid=px - 0.02, ask=px + 0.02, last=px, volume=5e6, timestamp=now
        ),
        indicators=TechnicalIndicators(atr_14=8.5, rsi_14=44.0),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BULL, confidence=0.6, reasoning="live"
        ),
        daily_change_pct=-1.9,
    )


def _composite(channels: dict[str, tuple[float, bool]], weights: dict[str, float]):
    strategies = {
        name: _Fixed(name, value, in_scope=in_scope) for name, (value, in_scope) in channels.items()
    }
    return CompositeStrategy(
        sub_strategies=strategies,
        weights=weights,
        regime_adaptive=False,
    ).compute_detailed_signal(_snapshot())


# The two cycles the review names, with the starter trending_bull weights. Channels
# that were not applicable in either cycle (event_window_timing, options_positioning)
# and range_break_continuation (off duty, price inside the bands) change nothing
# and are left out.
_MOMENTUM = 0.546
_MEAN_REVERSION = -0.034
_W = {
    "momentum": 0.2045,
    "mean_reversion": 0.129,
    "intraday_vwap_zscore": 0.1935,
    "swing_failure_reversal": 0.129,
    "trend_persistence": 0.129,
    "gap": 0.0,
    "vwap_reclaim_continuation": 0.0,
}

_RTH_1130 = {
    # Two channels vote; seven are in scope, because the intraday readers are
    # on duty during regular hours even when they see nothing. Two of the
    # seven (gap, vwap_reclaim_continuation) carry no weight.
    "momentum": (_MOMENTUM, True),
    "mean_reversion": (_MEAN_REVERSION, True),
    "intraday_vwap_zscore": (0.0, True),
    "swing_failure_reversal": (0.0, True),
    "trend_persistence": (0.0, True),
    "gap": (0.0, True),
    "vwap_reclaim_continuation": (0.0, True),
}
_AFTER_HOURS_1700 = {
    # The same two votes, but the intraday readers are out of scope.
    **_RTH_1130,
    "intraday_vwap_zscore": (0.0, False),
    "swing_failure_reversal": (0.0, False),
    "gap": (0.0, False),
    "vwap_reclaim_continuation": (0.0, False),
}
# A thinner pool, where the scale does apply: mean_reversion silent, so one of
# the five weighted channels on duty votes.
_RTH_THIN = {**_RTH_1130, "mean_reversion": (0.0, True)}
_WEIGHTS = _W


class TestTheLiveArithmeticIsReproduced:
    def test_the_regular_hours_read(self) -> None:
        """2 of 7 and scale 0.714 when the shadows were counted (+0.2297);
        counted over the weighted channels it is 2 of 5 and not scaled."""
        sig = _composite(_RTH_1130, _WEIGHTS)
        assert sig.composite_value == pytest.approx(0.3217, abs=5e-4)
        assert sig.n_voting == 2
        assert sig.n_in_scope == 7
        assert (sig.participation_numerator, sig.participation_denominator) == (2, 5)

    def test_the_old_reading_came_from_the_shadow_channels(self) -> None:
        weights = {**_WEIGHTS, "gap": 1e-9, "vwap_reclaim_continuation": 1e-9}
        sig = _composite(_RTH_1130, weights)
        assert sig.composite_value == pytest.approx(0.2297, abs=5e-4)

    def test_the_after_hours_read(self) -> None:
        sig = _composite(_AFTER_HOURS_1700, _WEIGHTS)
        assert sig.composite_value == pytest.approx(0.3217, abs=5e-4)
        assert sig.n_voting == 2
        assert sig.n_in_scope == 3
        assert (sig.participation_numerator, sig.participation_denominator) == (2, 3)


class TestTheHourIsDisclosed:
    def test_the_unattenuated_value_is_the_same_in_both_hours(self) -> None:
        """The information did not change between 11:30 and 17:00. This is the
        number an add rule must compare."""
        rth = _composite(_RTH_1130, _WEIGHTS)
        after = _composite(_AFTER_HOURS_1700, _WEIGHTS)

        assert rth.composite_unattenuated == pytest.approx(0.3217, abs=5e-4)
        assert after.composite_unattenuated == pytest.approx(0.3217, abs=5e-4)
        assert rth.composite_unattenuated == pytest.approx(after.composite_unattenuated, abs=1e-9)
        # And with two weighted votes the hour no longer changes the traded value.
        assert rth.composite_value == pytest.approx(after.composite_value, abs=1e-9)

    def test_a_thin_pool_still_differs_by_the_hour(self) -> None:
        """One weighted vote: 1 of 5 in regular hours, 1 of 3 after them."""
        thin = _composite(_RTH_THIN, _WEIGHTS)
        after = _composite({**_AFTER_HOURS_1700, "mean_reversion": (0.0, True)}, _WEIGHTS)
        assert thin.participation_scale == pytest.approx((1 / 5) / 0.4)
        assert after.participation_scale == pytest.approx((1 / 3) / 0.4)
        assert thin.composite_unattenuated == pytest.approx(after.composite_unattenuated, abs=1e-9)

    def test_the_scale_is_reported(self) -> None:
        thin = _composite(_RTH_THIN, _WEIGHTS)
        assert thin.participation_scale == pytest.approx((1 / 5) / 0.4, abs=1e-6)
        assert thin.composite_value == pytest.approx(
            thin.composite_unattenuated * thin.participation_scale, abs=1e-6
        )

    def test_no_attenuation_reports_a_scale_of_one(self) -> None:
        after = _composite(_AFTER_HOURS_1700, _WEIGHTS)
        assert after.participation_scale == pytest.approx(1.0)
        assert after.composite_unattenuated == pytest.approx(after.composite_value, abs=1e-9)


class TestTheAgentIsToldBothNumbers:
    def test_provenance_carries_them(self) -> None:
        from evotrader.agents.tools import _composite_provenance

        block = _composite_provenance(_composite(_RTH_THIN, _WEIGHTS))
        assert block["composite_unattenuated"] == pytest.approx(0.546, abs=5e-4)
        assert block["participation_scale"] == pytest.approx(0.5, abs=1e-3)
        assert block["participation_numerator"] == 1
        assert block["participation_denominator"] == 5
        assert block["attenuation_applied"] is True

    def test_provenance_survives_an_older_signal_shape(self) -> None:
        """The helper is called with whatever the cycle path produced."""
        from evotrader.agents.tools import _composite_provenance

        class _Old:
            n_additive = 2
            n_applicable = 2
            n_voting = 2
            n_in_scope = 2
            amplification_capped: ClassVar[list[str]] = []

        block = _composite_provenance(_Old())
        assert block["composite_unattenuated"] is None
        assert block["participation_scale"] == pytest.approx(1.0)
        assert block["attenuation_applied"] is False
