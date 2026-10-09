"""A swing-failure vote's magnitude can be measured from below its gate.

Found by the evolution agent's code review. The magnitude ramp was anchored AT
the gate (``base = tanh((stretch - min_stretch_atr) / stretch_scale)``), so a
confirmed reversal that just cleared the gate passed it, joined the voting pool
and the participation count, and contributed next to nothing. Lowering the gate
had not lowered the ramp with it. ``stretch_anchor_atr`` separates the two: the gate still decides
whether the channel fires, the anchor how loud it is. Unset, the anchor is the
gate, and every vote is what it was.

Made-up bars and parameters.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.algorithms.strategies.swing_failure_reversal import SwingFailureReversalStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

FLUSH_LOW, FLUSH_HIGH, ATR = 100.0, 100.6, 2.0
PARAMS = {
    "lookback_bars": 20,
    "min_confirm_bars": 2,
    "max_confirm_bars": 8,
    "min_stretch_atr": 0.5,
    "stretch_scale": 0.4,
    "confirm_decay": 0.95,
    "reclaim_scale": 0.5,
    "base_strength": 0.8,
}


def snapshot(stretch_atr: float, bars_since_flush: int = 4) -> MarketSnapshot:
    """24 five-minute bars: a flush, higher lows, a reclaim of the flush bar's high.

    The session VWAP sits ``stretch_atr`` ATRs above the flush low.
    """
    now = datetime(2026, 3, 3, 17, 30, tzinfo=UTC)
    n = 24
    flush_idx = n - 1 - bars_since_flush
    bars = []
    for i in range(n):
        if i == flush_idx:
            low, close, high = FLUSH_LOW, FLUSH_LOW + 0.3, FLUSH_HIGH
        elif i < flush_idx:
            low, close = FLUSH_LOW + 1.0, FLUSH_LOW + 1.2
            high = close + 0.1
        else:
            low, close = FLUSH_LOW + 0.2, FLUSH_HIGH + 0.05
            high = close + 0.1
        bars.append(
            OHLCV(
                timestamp=now - timedelta(minutes=5 * (n - i)),
                open=close,
                high=high,
                low=low,
                close=close,
                volume=1e4,
            )
        )
    last = bars[-1].close
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=now,
        quote=Quote(
            ticker="XYZ", bid=last - 0.01, ask=last + 0.01, last=last, volume=1e5, timestamp=now
        ),
        indicators=TechnicalIndicators(
            vwap=FLUSH_LOW + stretch_atr * ATR, vwap_anchor="current_session", atr_14=ATR
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="t"
        ),
        recent_candles=bars,
    )


def vote(stretch_atr: float, **overrides):
    return SwingFailureReversalStrategy(**{**PARAMS, **overrides}).compute_signal(
        snapshot(stretch_atr)
    )


class TestTheDefaultIsTheGate:
    def test_unset_reproduces_the_ramp_from_the_gate(self) -> None:
        """Bit for bit: every stored vote is what it was."""
        sig = vote(0.503)
        meta = sig.metadata
        assert meta["reason"] == "confirmed_reversal"
        assert meta["stretch_anchor_atr"] == pytest.approx(0.5)
        base = math.tanh((meta["stretch_atr"] - 0.5) / 0.4)
        assert meta["base"] == pytest.approx(round(base, 4))
        expected = base * meta["freshness"] * (0.5 + 0.5 * meta["reclaim_quality"]) * 0.8
        assert sig.value == pytest.approx(expected, abs=1e-4)
        assert sig.value < 0.01, "just past the gate, the ramp says almost nothing"

    def test_an_anchor_at_the_gate_is_the_default(self) -> None:
        assert vote(0.65, stretch_anchor_atr=0.5).value == vote(0.65).value


class TestTheAnchorSetsTheLoudness:
    def test_from_zero_a_reversal_just_past_the_gate_is_heard(self) -> None:
        quiet, heard = vote(0.503), vote(0.503, stretch_anchor_atr=0.0)
        assert heard.metadata["base"] == pytest.approx(round(math.tanh(0.503 / 0.4), 4))
        assert heard.value > 0.25 > 0.01 > quiet.value
        # Every other factor is unchanged: only the base moved.
        for key in ("freshness", "reclaim_quality", "bars_since_flush", "stretch_atr"):
            assert heard.metadata[key] == quiet.metadata[key]

    def test_the_gate_still_decides_whether_it_fires(self) -> None:
        sig = vote(0.45, stretch_anchor_atr=0.0)
        assert sig.value == 0.0
        assert sig.metadata["reason"] == "stretch_too_shallow"

    def test_a_deeper_stretch_is_still_louder(self) -> None:
        values = [vote(s, stretch_anchor_atr=0.0).value for s in (0.51, 0.7, 1.2)]
        assert values == sorted(values)
        assert values[0] < values[-1]


class TestTheParameter:
    def test_get_and_set(self) -> None:
        strategy = SwingFailureReversalStrategy(**PARAMS)
        assert strategy.get_parameters()["stretch_anchor_atr"] is None
        strategy.set_parameters({"stretch_anchor_atr": 0.1})
        assert strategy.get_parameters()["stretch_anchor_atr"] == 0.1

    @pytest.mark.parametrize(
        ("params", "ok"),
        [
            ({"stretch_anchor_atr": None}, True),
            ({"stretch_anchor_atr": 0.0}, True),
            ({"stretch_anchor_atr": 0.5}, True),
            ({"stretch_anchor_atr": 0.51}, False),
            ({"stretch_anchor_atr": -0.1}, False),
            ({"stretch_anchor_atr": 0.55, "min_stretch_atr": 0.6}, True),
        ],
    )
    def test_it_lies_between_zero_and_the_gate(self, params: dict, ok: bool) -> None:
        errors = SwingFailureReversalStrategy(**PARAMS).validate_parameters(params)
        assert (not errors) is ok, errors

    def test_the_loader_passes_it_from_a_versions_config(self) -> None:
        from pathlib import Path

        from evotrader.algorithms.loader import StrategyLoader

        manifest = (
            Path(__file__).resolve().parents[2] / "starter_data/algorithms/strategy_manifest.yaml"
        )
        composite = StrategyLoader(manifest).build_composite(
            {"swing_failure_reversal": {**PARAMS, "stretch_anchor_atr": 0.0}}
        )
        swing = composite._strategies["swing_failure_reversal"]
        assert swing.get_parameters()["stretch_anchor_atr"] == 0.0
