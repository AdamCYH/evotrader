"""Thresholds on the size of a price move mean the same thing on any ticker.

Four strategies gated on a move measured in percent of price: ``gap``
(``min_gap_pct``, ``gap_fade_threshold``), ``range_break_continuation``
(``min_break_pct``), ``trend_persistence`` (``counter_day_pct``) and
``event_window_timing`` (``min_overextension``, a fraction of price). A percent
bakes in one instrument's typical day. On the starter ticker, whose daily ATR
is about 1.05% of its price, a 0.7% move is two thirds of a normal day. On a
stock whose ATR is 6.6% it is a tenth of one, so the same gate fires on noise.

Each now takes an optional threshold in daily ATRs (``algorithms/units.py``).
These tests pin the three properties that make that safe to ship:

1. **Unset means unchanged.** With the ATR threshold left at ``None`` (every
   existing configuration) a strategy returns exactly what it did before.
2. **Set means portable.** With it set, the same move confirms on a calm
   instrument and not on a volatile one, which the percent rule cannot do.
3. **The starter configuration uses it**, so a newcomer who changes ticker gets
   thresholds that scale with the new instrument.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from evotrader.algorithms.strategies.event_window_timing import EventWindowTimingStrategy
from evotrader.algorithms.strategies.gap import GapStrategy
from evotrader.algorithms.strategies.range_break_continuation import (
    RangeBreakContinuationStrategy,
)
from evotrader.algorithms.strategies.trend_persistence import TrendPersistenceStrategy
from evotrader.algorithms.units import (
    MAX_THRESHOLD_ATR,
    check_move,
    move_in_atr,
    pct_move_in_atr,
    previous_close,
    validate_threshold_atr,
)
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from tests.unit.test_trend_persistence import _make_daily_candles, _make_snapshot

STARTER_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "starter_data" / "algorithms" / "v001_initial" / "config.yaml"
)

# Two instruments at the same price, one calm and one volatile.
CALM_ATR = 1.05   # 1.05% of a 100 price: an index fund
WILD_ATR = 6.6    # 6.6%: a volatile single stock
PREV = 100.0
OPEN_ET = datetime(2026, 9, 17, 13, 30, tzinfo=UTC)  # 09:30 ET: no session decay


# ── The shared rule ───────────────────────────────────────────────────────


class TestTheRule:
    def test_a_percent_move_in_atr(self) -> None:
        assert pct_move_in_atr(0.8, 100.0, 1.05) == pytest.approx(0.8 / 1.05)
        assert pct_move_in_atr(-0.8, 100.0, 1.05) == pytest.approx(0.8 / 1.05)

    def test_a_price_distance_in_atr(self) -> None:
        assert move_in_atr(-3.3, 6.6) == pytest.approx(0.5)

    @pytest.mark.parametrize("args", [(None, 100.0, 1.0), (0.8, None, 1.0),
                                      (0.8, 0.0, 1.0), (0.8, 100.0, None),
                                      (0.8, 100.0, 0.0)])
    def test_unmeasurable_is_none_not_zero(self, args) -> None:
        """Zero would read as "no move" and pass or fail a gate on no data."""
        assert pct_move_in_atr(*args) is None

    def test_previous_close_prefers_the_quote(self) -> None:
        assert previous_close(101.0, 1.0, 99.5) == 99.5

    def test_previous_close_is_derived_exactly_when_absent(self) -> None:
        assert previous_close(101.0, 1.0, None) == pytest.approx(100.0)

    def test_atr_decides_when_configured_and_measurable(self) -> None:
        chk = check_move(fallback_move=0.8, fallback_threshold=0.7,
                         move_atr=0.12, threshold_atr=0.65)
        assert (chk.met, chk.basis) == (False, "atr")
        assert chk.ratio == pytest.approx(0.12 / 0.65)

    def test_unset_falls_back_exactly(self) -> None:
        chk = check_move(fallback_move=-0.8, fallback_threshold=0.7,
                         move_atr=0.12, threshold_atr=None)
        assert (chk.met, chk.basis) == (True, "fallback")
        assert chk.ratio == 0.8 / 0.7

    def test_configured_but_unmeasurable_says_so(self) -> None:
        """A strategy that quietly reverted to the percent rule must be visible."""
        chk = check_move(fallback_move=0.8, fallback_threshold=0.7,
                         move_atr=None, threshold_atr=0.65)
        assert (chk.met, chk.basis) == (True, "fallback_no_atr")

    @pytest.mark.parametrize("value, ok", [(None, True), (0.65, True),
                                           (MAX_THRESHOLD_ATR, True), (0, False),
                                           (-0.5, False), (MAX_THRESHOLD_ATR + 0.1, False),
                                           ("abc", False)])
    def test_validation(self, value, ok) -> None:
        assert (validate_threshold_atr("x_atr", value) == []) is ok


# ── range_break_continuation ──────────────────────────────────────────────


def _break_snapshot(atr: float, day_chg: float = 0.8) -> MarketSnapshot:
    close = PREV * (1 + day_chg / 100)
    return MarketSnapshot(
        ticker="T", timestamp=OPEN_ET,
        quote=Quote(ticker="T", bid=close - 0.01, ask=close + 0.01, last=close,
                    volume=1e6, timestamp=OPEN_ET, previous_close=PREV),
        indicators=TechnicalIndicators(
            bollinger_upper=close - 0.3, bollinger_lower=close - 8.0,
            bollinger_middle=close - 4.0, atr_14=atr, macd_histogram=0.0,
            vwap=close - 0.5, vwap_anchor="current_session", ibs=0.9,
        ),
        regime=RegimeClassification(regime=MarketRegime.TRENDING_BULL,
                                    confidence=0.7, reasoning="t"),
        daily_change_pct=day_chg,
    )


class TestRangeBreak:
    def test_unset_is_the_old_percent_rule_on_both_instruments(self) -> None:
        s = RangeBreakContinuationStrategy(min_break_pct=0.7)
        for atr in (CALM_ATR, WILD_ATR):
            sig = s.compute_signal(_break_snapshot(atr))
            expected = min(1.0, 0.45 + (0.3 / atr) * 0.35)
            assert sig.value == pytest.approx(expected), atr
            assert sig.metadata["magnitude_basis"] == "fallback"

    def test_set_confirms_on_the_calm_instrument_only(self) -> None:
        s = RangeBreakContinuationStrategy(min_break_pct=0.7, min_break_atr=0.65)
        calm = s.compute_signal(_break_snapshot(CALM_ATR))
        wild = s.compute_signal(_break_snapshot(WILD_ATR))
        assert calm.value > 0 and calm.metadata["magnitude_basis"] == "atr"
        assert wild.value == 0.0, "0.8% is a tenth of a normal day here: noise"
        assert wild.metadata["day_chg_atr"] == pytest.approx(0.8 / WILD_ATR, abs=1e-4)


# ── gap ───────────────────────────────────────────────────────────────────


def _gap_snapshot(atr: float, gap: float = 0.8) -> MarketSnapshot:
    last = PREV * (1 + gap / 100)
    return MarketSnapshot(
        ticker="T", timestamp=OPEN_ET,
        quote=Quote(ticker="T", bid=last - 0.01, ask=last + 0.01, last=last,
                    volume=1e6, timestamp=OPEN_ET, previous_close=PREV),
        indicators=TechnicalIndicators(atr_14=atr),
        regime=RegimeClassification(regime=MarketRegime.RANGE_BOUND,
                                    confidence=0.6, reasoning="t"),
        daily_change_pct=gap, gap_pct=gap,
    )


class TestGap:
    def test_unset_is_the_old_percent_rule(self) -> None:
        s = GapStrategy(min_gap_pct=0.3, gap_fade_threshold=1.0)
        for atr in (CALM_ATR, WILD_ATR):
            sig = s.compute_signal(_gap_snapshot(atr))
            # moderate gap, old formula: -gap / fade * 0.3, no decay at the open
            assert sig.value == -0.8 / 1.0 * 0.3, atr
            assert sig.metadata["gap_basis"] == "fallback"

    def test_unset_large_gap_fades_exactly_as_before(self) -> None:
        s = GapStrategy(min_gap_pct=0.3, gap_fade_threshold=1.0)
        sig = s.compute_signal(_gap_snapshot(CALM_ATR, gap=-1.6))
        assert sig.metadata["gap_type"] == "fade"
        assert sig.value == max(-1.0, min(1.0, 1.6 / 1.0 * 1.0))  # unfilled

    def test_set_ignores_a_noise_sized_gap_on_the_volatile_instrument(self) -> None:
        s = GapStrategy(min_gap_pct=0.3, gap_fade_threshold=1.0,
                        min_gap_atr=0.3, gap_fade_threshold_atr=0.95)
        calm = s.compute_signal(_gap_snapshot(CALM_ATR))
        wild = s.compute_signal(_gap_snapshot(WILD_ATR))
        assert calm.metadata["applicable"] is True
        assert calm.value == pytest.approx(-(0.8 / CALM_ATR) / 0.95 * 0.3)
        assert wild.metadata["applicable"] is False and wild.value == 0.0


# ── trend_persistence ─────────────────────────────────────────────────────


def _grind(day_chg: float) -> MarketSnapshot:
    """A bearish grind that triggers, with ATR 5 on a 660 price (0.76%)."""
    closes = [690, 688, 685, 682, 678, 674, 660]
    return _make_snapshot(close=660.0, daily_candles=_make_daily_candles(closes),
                          daily_change_pct=day_chg, atr_14=5.0)


class TestTrendPersistenceCounterDay:
    def test_unset_is_the_old_percent_rule(self) -> None:
        s = TrendPersistenceStrategy(counter_day_pct=1.0)
        quiet = s.compute_signal(_grind(0.9))   # 0.9% < 1.0%: no guard
        loud = s.compute_signal(_grind(1.5))    # 1.5% >= 1.0%: guard
        assert quiet.value < 0 and not quiet.metadata.get("counter_day_applied")
        assert loud.value == pytest.approx(quiet.value * 0.5)
        assert loud.metadata["counter_day_basis"] == "fallback"

    def test_set_catches_a_large_counter_day_the_percent_rule_misses(self) -> None:
        """+0.9% is 1.18 daily ATRs on an instrument this calm."""
        pct_only = TrendPersistenceStrategy(counter_day_pct=1.0)
        in_atr = TrendPersistenceStrategy(counter_day_pct=1.0, counter_day_atr=0.95)
        base = pct_only.compute_signal(_grind(0.9)).value
        sig = in_atr.compute_signal(_grind(0.9))
        assert sig.metadata["counter_day_applied"] is True
        assert sig.metadata["counter_day_basis"] == "atr"
        assert sig.value == pytest.approx(base * 0.5)


# ── event_window_timing ───────────────────────────────────────────────────


def _post_event(atr: float, stretch_pct: float = 1.6) -> MarketSnapshot:
    vwap = 100.0
    price = vwap * (1 + stretch_pct / 100)
    return MarketSnapshot(
        ticker="T", timestamp=OPEN_ET,
        quote=Quote(ticker="T", bid=price - 0.01, ask=price + 0.01, last=price,
                    volume=1e6, timestamp=OPEN_ET, previous_close=PREV),
        indicators=TechnicalIndicators(
            atr_14=atr, vwap=vwap, vwap_anchor="current_session",
            hours_since_event=6.0, atm_iv_30dte=0.30, atm_iv_pre_event=0.50,
            event_type="earnings",
        ),
        regime=RegimeClassification(regime=MarketRegime.RANGE_BOUND,
                                    confidence=0.6, reasoning="t"),
        daily_change_pct=stretch_pct,
    )


class TestEventWindowOverextension:
    def test_unset_is_the_old_fraction_rule(self) -> None:
        s = EventWindowTimingStrategy(min_overextension=0.015)
        sig = s.compute_signal(_post_event(CALM_ATR))
        ov = (101.6 - 100.0) / 100.0
        assert sig.metadata["overextension_basis"] == "fallback"
        assert sig.value == pytest.approx(
            max(-1.0, min(1.0, -1.0 * math.tanh(ov / 0.015) * 1.5))
        )

    def test_set_does_not_fire_on_a_small_stretch_on_the_volatile_instrument(self) -> None:
        s = EventWindowTimingStrategy(min_overextension=0.015, min_overextension_atr=1.45)
        calm = s.compute_signal(_post_event(CALM_ATR))   # 1.52 ATR
        wild = s.compute_signal(_post_event(WILD_ATR))   # 0.24 ATR
        assert calm.metadata["overextension_met"] is True
        assert wild.metadata["overextension_met"] is False
        assert wild.metadata["overextension_basis"] == "atr"


# ── Configuration: parameters round-trip, starter uses them ───────────────


ATR_PARAMS = {
    GapStrategy: ("min_gap_atr", "gap_fade_threshold_atr"),
    RangeBreakContinuationStrategy: ("min_break_atr",),
    TrendPersistenceStrategy: ("counter_day_atr",),
    EventWindowTimingStrategy: ("min_overextension_atr",),
}


@pytest.mark.parametrize("cls", list(ATR_PARAMS))
def test_every_atr_threshold_defaults_to_off(cls) -> None:
    """An existing config that never names them keeps the old behaviour."""
    params = cls().get_parameters()
    for key in ATR_PARAMS[cls]:
        assert params[key] is None, (cls.__name__, key)


@pytest.mark.parametrize("cls", list(ATR_PARAMS))
def test_every_atr_threshold_round_trips_and_validates(cls) -> None:
    s = cls()
    for key in ATR_PARAMS[cls]:
        s.set_parameters({key: 0.8})
        assert s.get_parameters()[key] == 0.8
        s.set_parameters({key: None})
        assert s.get_parameters()[key] is None
        assert s.validate_parameters({key: 0.8}) == []
        assert s.validate_parameters({key: 0}) != []
        assert s.validate_parameters({key: None}) == []


def test_the_starter_config_sets_every_atr_threshold() -> None:
    cfg = yaml.safe_load(STARTER_CONFIG.read_text())
    blocks = {
        GapStrategy: "gap",
        RangeBreakContinuationStrategy: "range_break_continuation",
        TrendPersistenceStrategy: "trend_persistence",
        EventWindowTimingStrategy: "event_window_timing",
    }
    for cls, block in blocks.items():
        for key in ATR_PARAMS[cls]:
            value = cfg[block].get(key)
            assert value is not None, f"starter {block}.{key} is not set"
            assert cls().validate_parameters({key: value}) == [], (block, key)
