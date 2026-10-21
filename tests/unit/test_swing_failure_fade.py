"""swing_failure_fade: a session high that failed, the mirror of swing_failure_reversal.

swing_failure_reversal fires long after a flush LOW has held: higher lows since,
and a close back above the flush bar's high. Nothing read the other side, so a
session high that had failed went unrecorded, and a sell-leg firing of the VWAP
stretch at the high had no confirmation to wait for. swing_failure_fade is the
same rule reflected: a flush HIGH stretched above session VWAP, lower highs
since, a close back below the flush bar's low, and no new high on the last bar.
It is short-only, records the daily MA stack it fired in, and ships at weight 0.

The mirror is checked exactly: on any series, the fade of the reflected series
is minus the long channel's vote on the original, reason for reason. Made-up
ticker and prices.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from evotrader.algorithms.loader import StrategyLoader
from evotrader.algorithms.strategies.swing_failure_fade import SwingFailureFadeStrategy
from evotrader.algorithms.strategies.swing_failure_reversal import SwingFailureReversalStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

NOW = datetime(2026, 3, 3, 18, 30, tzinfo=UTC)  # 13:30 ET
ATR = 4.0
VWAP = 100.0
PARAMS = {
    "lookback_bars": 20,
    "min_confirm_bars": 2,
    "max_confirm_bars": 14,
    "min_stretch_atr": 0.3,
    "stretch_scale": 0.3,
    "confirm_decay": 0.87,
    "reclaim_scale": 0.5,
    "base_strength": 0.8,
}
STARTER = Path(__file__).resolve().parents[2] / "starter_data" / "algorithms"


def _bars(ohlc: list[tuple[float, float, float, float]]) -> list[OHLCV]:
    n = len(ohlc)
    return [
        OHLCV(
            timestamp=NOW - timedelta(minutes=5 * (n - i)),
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=1e4,
        )
        for i, (o, h, lo, c) in enumerate(ohlc)
    ]


def _snapshot(
    ohlc: list[tuple[float, float, float, float]],
    vwap: float | None = VWAP,
    anchor: str | None = "current_session",
    stack: dict | None = None,
) -> MarketSnapshot:
    bars = _bars(ohlc)
    last = bars[-1].close if bars else VWAP
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ", bid=last - 0.01, ask=last + 0.01, last=last, volume=1e5, timestamp=NOW
        ),
        indicators=TechnicalIndicators(
            vwap=vwap, vwap_anchor=anchor, atr_14=ATR, ibs=0.4, **(stack or {})
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="fixture"
        ),
        recent_candles=bars,
    )


def _spike(bars_after: int, closes_after: list[float] | None = None):
    """Flat bars near VWAP, a spike bar to 103.0 (its low 101.5), then
    ``bars_after`` bars under it closing at ``closes_after`` (default: a break
    below the spike bar's low on the first bar after it)."""
    flat = [(100.4, 100.8, 100.0, 100.5)] * (22 - bars_after)
    spike = [(101.6, 103.0, 101.5, 102.0)]
    closes = closes_after or [101.2 - 0.1 * i for i in range(bars_after)]
    after = [(c + 0.1, c + 0.3, c - 0.2, c) for c in closes]
    return flat + spike + after


def _fade(ohlc, **kw):
    params = {**PARAMS, **kw.pop("params", {})}
    return SwingFailureFadeStrategy(**params).compute_signal(_snapshot(ohlc, **kw))


# ── The rule ─────────────────────────────────────────────────────────────


class TestTheRule:
    def test_a_failed_high_fires_short(self) -> None:
        sig = _fade(_spike(3))
        meta = sig.metadata
        assert meta["reason"] == "confirmed_fade"
        assert meta["flush_high"] == 103.0
        assert meta["flush_low"] == 101.5
        assert meta["stretch_atr"] == pytest.approx(0.75)
        # Broke below 101.5 on the first bar after the spike; min_confirm_bars
        # 2 makes it confirmed one bar later, so it is one bar old.
        assert (meta["bars_since_flush"], meta["bars_since_confirmed"]) == (3, 1)
        base = math.tanh((0.75 - 0.3) / 0.3)
        quality = (101.5 - 101.0) / ATR / 0.5
        expected = -base * 0.87 * (0.5 + 0.5 * quality) * 0.8
        assert sig.value == pytest.approx(expected)
        assert -1.0 <= sig.value < 0

    def test_too_soon_after_the_high(self) -> None:
        assert _fade(_spike(1)).metadata["reason"] == "too_few_confirm_bars"

    def test_an_old_high_has_expired(self) -> None:
        assert _fade(_spike(15)).metadata["reason"] == "stale_flush"

    def test_a_shallow_high_is_no_setup(self) -> None:
        sig = _fade(_spike(3), vwap=102.0)  # the high only 0.25 ATR above VWAP
        assert sig.metadata["reason"] == "stretch_too_shallow"
        assert sig.value == 0.0

    def test_still_above_the_spike_bars_low(self) -> None:
        sig = _fade(_spike(3, closes_after=[102.2, 102.0, 101.8]))
        assert sig.metadata["reason"] == "not_broken_down"
        assert sig.metadata["lower_high"] is True
        assert sig.value == 0.0

    def test_a_retest_of_the_high_is_not_a_lower_high(self) -> None:
        ohlc = _spike(3)
        o, _, lo, c = ohlc[-2]
        ohlc[-2] = (o, 103.0, lo, c)  # equals the flush high
        assert _fade(ohlc).metadata["reason"] == "no_lower_high"

    def test_the_value_does_not_depend_on_the_stack_mode(self) -> None:
        bear = {"ema_9": 102.0, "ema_21": 102.5, "sma_20": 103.0, "sma_50": 90.0}
        slow = _fade(_spike(3), stack=bear, params={"stack_mode": "slow"})
        location = _fade(_spike(3), stack=bear, params={"stack_mode": "location"})
        assert slow.value == location.value
        assert slow.metadata["stack_context"] == "no_stack", "SMA 20 still above SMA 50"
        assert location.metadata["stack_context"] == "bear", "price under EMA 9, 21 and SMA 20"
        assert location.metadata["sma_cross"] == "bull"
        assert location.metadata["leg"] == "fade"


class TestAbstains:
    def test_no_vwap(self) -> None:
        meta = _fade(_spike(3), vwap=None).metadata
        assert (meta["applicable"], meta["reason"]) == (False, "no_vwap")

    def test_a_stale_vwap_anchor(self) -> None:
        meta = _fade(_spike(3), anchor="prior_session").metadata
        assert (meta["applicable"], meta["reason"]) == (False, "stale_vwap_anchor")

    def test_early_in_the_session_it_is_warming_up(self) -> None:
        meta = _fade(_spike(3)[-12:]).metadata
        assert (meta["applicable"], meta["reason"]) == (False, "insufficient_data")
        assert meta["warming_up"] is True


# ── The mirror ───────────────────────────────────────────────────────────

MIRROR = 400.0  # reflect around 200: every price stays positive


def _reflect(ohlc):
    return [(MIRROR - o, MIRROR - lo, MIRROR - h, MIRROR - c) for o, h, lo, c in ohlc]


LONG_TO_FADE = {
    "confirmed_reversal": "confirmed_fade",
    "no_higher_low": "no_lower_high",
    "not_reclaimed": "not_broken_down",
    "new_low_on_last_bar": "new_high_on_last_bar",
}


def _random_series(rng: random.Random) -> tuple[list, float]:
    """A random walk on a 0.05 grid (so ties happen), with a flush down
    somewhere in it; the VWAP a little above the start."""
    n = rng.randint(18, 26)
    price, out = 100.0, []
    flush_at = rng.randrange(n)
    for i in range(n):
        step = -rng.uniform(1.0, 3.0) if i == flush_at else rng.uniform(-0.6, 0.6)
        o = price
        c = round((price + step) * 20) / 20
        h = round((max(o, c) + rng.uniform(0, 0.4)) * 20) / 20
        lo = round((min(o, c) - rng.uniform(0, 0.4)) * 20) / 20
        out.append((o, h, lo, c))
        price = c
    return out, 100.0 + rng.uniform(-0.5, 2.0)


@pytest.mark.parametrize(
    "params",
    [PARAMS, {**PARAMS, "stretch_anchor_atr": 0.0}, {**PARAMS, "max_confirm_bars": 8}],
    ids=["starter", "anchor 0", "max 8"],
)
def test_the_fade_is_the_reversal_reflected(params: dict) -> None:
    rng = random.Random(11)
    long, fade = SwingFailureReversalStrategy(**params), SwingFailureFadeStrategy(**params)
    seen = set()
    for _ in range(3000):
        ohlc, vwap = _random_series(rng)
        up = long.compute_signal(_snapshot(ohlc, vwap=vwap))
        down = fade.compute_signal(_snapshot(_reflect(ohlc), vwap=MIRROR - vwap))
        reason = "|".join(LONG_TO_FADE.get(r, r) for r in up.metadata["reason"].split("|"))
        assert down.metadata["reason"] == reason
        assert down.value == pytest.approx(-up.value, abs=1e-12)
        assert down.metadata.get("bars_since_flush") == up.metadata.get("bars_since_flush")
        for key in ("stretch_atr", "freshness", "base"):  # stored to 4 places
            if key in up.metadata:
                assert down.metadata[key] == pytest.approx(up.metadata[key], abs=1.5e-4), key
        seen.add(down.metadata["reason"])
    # The walk reaches every branch, so the mirror is checked on each.
    assert {"confirmed_fade", "stale_flush", "too_few_confirm_bars"} <= seen
    assert {"stretch_too_shallow", "not_broken_down"} <= seen
    assert any("no_lower_high" in r for r in seen)


# ── Parameters ───────────────────────────────────────────────────────────


class TestParameters:
    def test_round_trip(self) -> None:
        strat = SwingFailureFadeStrategy()
        assert strat.get_parameters()["stack_mode"] == "slow"
        strat.set_parameters({"stack_mode": "location", "min_stretch_atr": 0.4})
        params = strat.get_parameters()
        assert (params["stack_mode"], params["min_stretch_atr"]) == ("location", 0.4)

    @pytest.mark.parametrize(
        "bad",
        [
            {"lookback_bars": 2},
            {"min_confirm_bars": 0},
            {"min_confirm_bars": 5, "max_confirm_bars": 5},
            {"min_stretch_atr": 0},
            {"confirm_decay": 1.0},
            {"base_strength": 0.0},
            {"min_stretch_atr": 0.3, "stretch_anchor_atr": 0.4},
            {"stack_mode": "fast"},
        ],
    )
    def test_validation(self, bad: dict) -> None:
        assert SwingFailureFadeStrategy().validate_parameters(bad)

    def test_the_starter_parameters_pass(self) -> None:
        config = yaml.safe_load((STARTER / "v001_initial" / "config.yaml").read_text())
        assert SwingFailureFadeStrategy().validate_parameters(config["swing_failure_fade"]) == []


# ── In the ensemble: recorded, not voting ────────────────────────────────


def _composite(config: dict):
    return StrategyLoader(STARTER / "strategy_manifest.yaml").build_composite(config)


def test_the_starter_runs_it_at_weight_zero_in_every_regime() -> None:
    config = yaml.safe_load((STARTER / "v001_initial" / "config.yaml").read_text())
    comp = _composite(config)
    assert "swing_failure_fade" in comp._strategies
    assert comp._weights["swing_failure_fade"] == 0.0
    for regime in MarketRegime:
        assert comp._regime_weights_map[regime].get("swing_failure_fade", 0.0) == 0.0


def test_a_version_that_predates_it_runs_it_at_zero() -> None:
    config = yaml.safe_load((STARTER / "v001_initial" / "config.yaml").read_text())
    config.pop("swing_failure_fade")
    config["composite"].pop("swing_failure_fade_weight")
    for weights in config["composite"]["regime_weights"].values():
        weights.pop("swing_failure_fade")
    assert _composite(config)._weights["swing_failure_fade"] == 0.0


def test_its_vote_is_recorded_and_moves_nothing() -> None:
    config = yaml.safe_load((STARTER / "v001_initial" / "config.yaml").read_text())
    snap = _snapshot(_spike(3))
    detailed = _composite(config).compute_detailed_signal(snap)
    vote = next(s for s in detailed.signals if s.name == "swing_failure_fade")
    assert vote.metadata["reason"] == "confirmed_fade"
    assert vote.value < 0
    without = _composite(config)
    without._strategies.pop("swing_failure_fade")
    assert detailed.composite_value == pytest.approx(
        without.compute_detailed_signal(snap).composite_value, abs=1e-12
    )
