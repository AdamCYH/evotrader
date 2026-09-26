"""The six 2026-09-17 live cycles, as a regression fixture.

See: data/evolution/reviews/20260917_200126_vwap_zscore_unconditional_countertrend_fade.md
(findings 7 and 8)

The evolution agent reconstructed the live composite by hand for every cycle
that reported sub-signals and matched the engine to four decimals. Finding 8
asks for those numbers to be kept as a fixture, so any future change to the
ensemble can be checked against a real engine rather than against intuition.

They also document what the composite actually WAS that day — a two-state
machine:

    vwap_z silent (pool 0.31)  ->  +0.08..+0.10   the momentum DC offset
    vwap_z fires  (pool 0.49)  ->  -0.08..-0.17   sign set almost wholly by vwap_z

``momentum`` and ``mean_reversion`` are both built from daily MAs, daily MACD
and daily RSI, so on an hourly cadence they cannot vary. Their combined
numerator across the six cycles was 0.0247, 0.0298, 0.0313, 0.0307, 0.0312,
0.0311 — one witness repeated six times, summing to a permanent +0.096 offset
in a bull stack. That is structure, not evidence.
"""

from __future__ import annotations

import types

import pytest

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.composite import CompositeStrategy
from evotrader.models.market import MarketRegime
from evotrader.models.signals import AlgoSignal

# trending_bull weights, live on 2026-09-17.
_W = {"momentum": 0.19, "mean_reversion": 0.12, "intraday_vwap_zscore": 0.18}

# (label, sub-signal values, engine-reported composite)
_CYCLES = [
    ("11:30 silent", {"momentum": 0.1564, "mean_reversion": -0.0420}, +0.0796),
    ("12:30 silent", {"momentum": 0.2056, "mean_reversion": -0.0770}, +0.0962),
    ("15:30 silent", {"momentum": 0.2184, "mean_reversion": -0.0852}, +0.1009),
    (
        "10:30 firing",
        {"momentum": 0.2333, "mean_reversion": -0.1139, "intraday_vwap_zscore": -0.6306},
        -0.1691,
    ),
    (
        "13:30 firing",
        {"momentum": 0.2365, "mean_reversion": -0.1187, "intraday_vwap_zscore": -0.5298},
        -0.1320,
    ),
    (
        "14:30 firing",
        {"momentum": 0.2335, "mean_reversion": -0.1092, "intraday_vwap_zscore": -0.3806},
        -0.0760,
    ),
]


class _Stub(TradingAlgorithm):
    def __init__(
        self, name: str, value: float, *, applicable: bool = True, in_scope: bool | None = None
    ) -> None:
        self._name, self._value = name, value
        self._applicable, self._in_scope = applicable, in_scope

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "fixture"

    @property
    def description(self) -> str:
        return self._name

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


def _snapshot():
    return types.SimpleNamespace(regime=types.SimpleNamespace(regime=MarketRegime.TRENDING_BULL))


def _build(values: dict[str, float], shadow: _Stub | None = None) -> CompositeStrategy:
    subs = {n: _Stub(n, v) for n, v in values.items()}
    weights = {n: _W[n] for n in values}
    if shadow is not None:
        subs["vwap_reclaim_continuation"] = shadow
        weights["vwap_reclaim_continuation"] = 0.0
    return CompositeStrategy(
        sub_strategies=subs, weights=weights, regime_adaptive=False, version="fixture"
    )


@pytest.mark.parametrize(
    ("label", "values", "expected"),
    _CYCLES,
    ids=[c[0].replace(" ", "-") for c in _CYCLES],
)
def test_the_engine_is_reproduced_to_four_decimals(
    label: str, values: dict[str, float], expected: float
) -> None:
    """If a future ensemble change breaks these, it changed the arithmetic."""
    got = _build(values).compute_detailed_signal(_snapshot()).composite_value
    assert got == pytest.approx(expected, abs=5e-5), f"{label}: {got} vs {expected}"


def test_the_daily_pair_is_a_near_constant() -> None:
    """The finding-8 evidence, asserted rather than narrated.

    A spread this tight across six hourly cycles is the signature of a DAILY
    input being re-emitted, not of a market being read.
    """
    numerators = [
        _W["momentum"] * v["momentum"] + _W["mean_reversion"] * v["mean_reversion"]
        for _, v, _ in _CYCLES
    ]
    assert min(numerators) == pytest.approx(0.0247, abs=5e-5)
    assert max(numerators) == pytest.approx(0.0313, abs=5e-5)
    assert max(numerators) - min(numerators) < 0.007


class TestShadowModeIsInert:
    """A 0.0-weight channel must not perturb the ensemble it is observing.

    Weight 0.0 contributes nothing to the numerator, but an `applicable`
    channel still occupies a slot in the PARTICIPATION denominator — which is
    precisely the unearned-haircut mechanism fixed on 2026-09-10. Verified
    rather than assumed.
    """

    @pytest.mark.parametrize(
        ("label", "values", "expected"),
        _CYCLES,
        ids=[c[0].replace(" ", "-") for c in _CYCLES],
    )
    def test_silent_shadow_channel_changes_nothing(
        self, label: str, values: dict[str, float], expected: float
    ) -> None:
        shadow = _Stub("vwap_reclaim_continuation", 0.0, applicable=True)
        got = _build(values, shadow).compute_detailed_signal(_snapshot()).composite_value
        assert got == pytest.approx(expected, abs=5e-5)

    def test_off_duty_shadow_channel_changes_nothing(self) -> None:
        _, values, expected = _CYCLES[3]
        shadow = _Stub("vwap_reclaim_continuation", 0.0, applicable=True, in_scope=False)
        got = _build(values, shadow).compute_detailed_signal(_snapshot()).composite_value
        assert got == pytest.approx(expected, abs=5e-5)

    def test_a_firing_shadow_channel_still_changes_nothing(self) -> None:
        """Even when it votes, weight 0.0 must keep it out of the answer."""
        _, values, expected = _CYCLES[0]
        shadow = _Stub("vwap_reclaim_continuation", 0.62, applicable=True)
        got = _build(values, shadow).compute_detailed_signal(_snapshot()).composite_value
        assert got == pytest.approx(expected, abs=5e-5), (
            "shadow mode leaked into the composite — it is not shadow mode"
        )
