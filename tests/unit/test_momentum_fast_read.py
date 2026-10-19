"""Momentum reports which way its fast pair points, whatever the MA term reads.

``fast_dissent`` is the fast pair (price beyond EMA 9 by ``fast_dissent_min_atr``
daily ATRs, and the daily MACD histogram outside its neutral band) AGAINST the
moving-average term. After a rally SMA 20 stays above SMA 50 for weeks; once
price falls far under SMA 20 that stale part cancels the price part, and the MA
term crosses just below zero. From then on the fast pair can point down as hard
as it likes and ``fast_dissent`` stays false, so a rule that reads it to close a
long disarms where the tape is most bearish.

``fast_read`` (+1, -1, 0) is the pair's own direction and ``fast_dissent`` the
same condition as before, read off it; ``fast_read_agrees_value`` says whether
the pair points the way the vote does. Momentum also reports the MA term's
three weighted parts (``ma_signal_parts``), and trend_persistence how far past
its displacement gate a grind is (``gate_margin_atr``). No vote changes.
Made-up ticker and prices.
"""

from __future__ import annotations

import itertools
import math
import random
from datetime import UTC, datetime

import pytest

from evotrader.algorithms.strategies.momentum import (
    MACD_NEUTRAL_ATR,
    MomentumStrategy,
    fast_pair_direction,
)
from evotrader.algorithms.strategies.trend_persistence import TrendPersistenceStrategy
from evotrader.db.signal_attribution import channel_votes_from_sub_signals
from evotrader.indicators.moving_averages import (
    MovingAverageParts,
    moving_average_parts,
    moving_average_signal,
)
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)

NOW = datetime(2026, 3, 10, 16, 0, tzinfo=UTC)
ATR = 4.0
# After a rally: SMA 20 still well above SMA 50, price far under SMA 20 and
# under both EMAs. The MA term reads just below zero.
STALE = {"ema_9": 88.5, "ema_21": 88.6, "sma_20": 90.0, "sma_50": 80.0}
# A bull stack that price has just broken under: the MA term still reads long.
BULL = {"ema_9": 100.0, "ema_21": 98.0, "sma_20": 97.0, "sma_50": 90.0}


def _snapshot(
    price: float, hist: float, stack: dict, day_change_pct: float = -2.0
) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=1e6,
            timestamp=NOW,
            previous_close=round(price / (1 + day_change_pct / 100), 4),
        ),
        indicators=TechnicalIndicators(
            atr_14=ATR,
            macd_line=hist,
            macd_signal=0.0,
            macd_histogram=hist,
            relative_volume=None,
            **stack,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="fixture"
        ),
        daily_change_pct=day_change_pct,
    )


def _meta(price: float, hist: float, stack: dict, decay: float = 1.0, **kw) -> dict:
    sig = MomentumStrategy(fast_dissent_decay=decay).compute_signal(
        _snapshot(price, hist, stack, **kw)
    )
    return {**sig.metadata, "value": sig.value}


def _old_fast_dissent(p: float | None, h: float | None, ma: float, m: float) -> bool:
    """The rule as it stood before fast_read, verbatim."""
    return (
        ma != 0.0
        and p is not None
        and h is not None
        and p * ma < 0
        and abs(p) >= m
        and h * ma < 0
        and abs(h) >= MACD_NEUTRAL_ATR
    )


class TestTheFastPair:
    @pytest.mark.parametrize(
        ("p", "h", "expected"),
        [
            (-1.125, -0.3, -1),
            (1.125, 0.3, 1),
            (-1.125, 0.3, 0),  # the two disagree
            (-0.2, -0.3, 0),  # price under EMA 9 by less than the margin
            (-1.125, -0.05, 0),  # the MACD inside its neutral band
            (-0.25, -MACD_NEUTRAL_ATR, -1),  # both exactly at their bands
            (None, -0.3, 0),
            (-1.125, None, 0),
        ],
    )
    def test_its_direction(self, p, h, expected: int) -> None:
        assert fast_pair_direction(p, h, 0.25) == expected

    def test_price_at_ema9_points_nowhere_even_with_no_margin(self) -> None:
        assert fast_pair_direction(0.0, -0.3, 0.0) == 0

    def test_fast_dissent_keeps_its_truth_table(self) -> None:
        """Every combination, the band edges included, against the rule as it stood."""
        grid = itertools.product(
            [None, -1.2, -0.25, -0.2499, -0.0001, 0.0, 0.0001, 0.2499, 0.25, 1.2],
            [None, -0.5, -0.1, -0.0999, 0.0, 0.0999, 0.1, 0.5],
            [-0.6, -1e-9, 0.0, 1e-9, 0.6],
            [0.0, 0.25, 1.0],
        )
        differ = []
        for p, h, ma, m in grid:
            read = fast_pair_direction(p, h, m)
            if (read != 0 and read * ma < 0) != _old_fast_dissent(p, h, ma, m):
                differ.append((p, h, ma, m))
        assert differ == []


class TestTheVote:
    def test_after_the_ma_term_crosses_zero_the_pair_still_reads_down(self) -> None:
        meta = _meta(84.0, -1.2, STALE)
        assert meta["ma_signal"] < 0, "the stale SMA cross has cancelled the price part"
        assert meta["price_vs_ema9_atr"] == pytest.approx(-1.125)
        assert meta["macd_hist_atr"] == pytest.approx(-0.3)
        assert meta["fast_read"] == -1
        assert meta["fast_dissent"] is False, "no dissent: both point down"
        assert meta["value"] < 0
        assert meta["fast_read_agrees_value"] is True

    def test_against_a_long_ma_term_it_is_the_old_dissent(self) -> None:
        meta = _meta(95.0, -0.6, BULL)
        assert meta["ma_signal"] > 0
        assert meta["fast_read"] == -1
        assert meta["fast_dissent"] is True
        assert meta["value"] > 0, "off by default: the long read stands, halved"
        assert meta["fast_read_agrees_value"] is False

    def test_with_the_decay_on_the_vote_follows_the_pair(self) -> None:
        meta = _meta(95.0, -0.6, BULL, decay=0.0)
        assert meta["fast_dissent_applied"] is True
        assert meta["value"] < 0
        assert meta["fast_read_agrees_value"] is True

    def test_a_neutral_macd_reads_no_direction(self) -> None:
        meta = _meta(105.0, -0.2, BULL, day_change_pct=2.0)
        assert meta["macd_neutral"] is True
        assert meta["fast_read"] == 0
        assert meta["fast_dissent"] is False
        assert meta["fast_read_agrees_value"] is None

    def test_a_rising_pair_with_a_long_stack(self) -> None:
        meta = _meta(105.0, 0.6, BULL, day_change_pct=2.0)
        assert meta["fast_read"] == 1
        assert meta["fast_dissent"] is False
        assert meta["fast_read_agrees_value"] is True


class TestTheMaParts:
    def test_the_parts_sum_to_the_signal_exactly(self) -> None:
        rng = random.Random(7)
        for _ in range(500):
            close, e9, e21, s20, s50 = (rng.uniform(50, 150) for _ in range(5))
            atr = rng.choice([None, 0.0, rng.uniform(0.5, 10)])
            old = (  # the blend as written before the parts were split out
                0.4 * math.tanh((e9 - e21) / (0.75 * atr if atr else close * 0.005))
                + 0.3 * math.tanh((close - s20) / (0.75 * atr if atr else close * 0.005))
                + 0.3 * math.tanh((s20 - s50) / (1.5 * atr if atr else close * 0.010))
            )
            parts = moving_average_parts(close, e9, e21, s20, s50, atr)
            assert parts.total == old
            assert moving_average_signal(close, e9, e21, s20, s50, atr) == old

    def test_no_sma50_reads_zero(self) -> None:
        assert moving_average_parts(100.0, 99.0, 98.0, 97.0, 0.0) == MovingAverageParts(0, 0, 0)
        assert moving_average_signal(100.0, 99.0, 98.0, 97.0, 0.0) == 0.0

    def test_the_vote_shows_the_stale_cross_cancelling_the_price(self) -> None:
        meta = _meta(84.0, -1.2, STALE)
        parts = meta["ma_signal_parts"]
        assert set(parts) == {"ema_9_vs_21", "price_vs_sma_20", "sma_20_vs_50"}
        assert parts["sma_20_vs_50"] > 0.25
        assert parts["price_vs_sma_20"] < -0.25
        assert sum(parts.values()) == pytest.approx(meta["ma_signal"], abs=2e-4)

    def test_no_moving_averages_no_parts(self) -> None:
        assert _meta(84.0, -1.2, {**STALE, "sma_50": None})["ma_signal_parts"] is None


class TestTheRecord:
    def test_the_reads_reach_the_channel_votes_as_reported(self) -> None:
        down = _meta(84.0, -1.2, STALE)
        flat = _meta(105.0, -0.2, BULL, day_change_pct=2.0)
        votes = channel_votes_from_sub_signals(
            [
                {"name": "momentum", "value": down["value"], "weight": 0.2, "metadata": down},
                {"name": "other", "value": flat["value"], "weight": 0.2, "metadata": flat},
                {"name": "gap", "value": 0.0, "weight": 0.0, "metadata": {"reason": "x"}},
            ]
        )
        assert votes["momentum"]["fast_read"] == -1
        assert votes["momentum"]["fast_read_agrees_value"] is True
        assert votes["other"]["fast_read"] == 0
        assert votes["other"]["fast_read_agrees_value"] is None
        assert set(votes["gap"]) == {"value", "weight", "applicable", "reason"}


# ── trend_persistence: how far past the gate ─────────────────────────────


def _grind(last: float) -> MarketSnapshot:
    """Six down days from 100 to ``last`` on an ATR of 5, under a bear stack."""
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, last]
    candles = [
        OHLCV(
            timestamp=datetime(2026, 3, 2 + i, 21, 0, tzinfo=UTC),
            open=c + 0.5,
            high=c + 1.0,
            low=c - 1.0,
            close=c,
            volume=1e6,
        )
        for i, c in enumerate(closes)
    ]
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ", bid=last - 0.01, ask=last + 0.01, last=last, volume=1e6, timestamp=NOW
        ),
        indicators=TechnicalIndicators(
            atr_14=5.0, ema_9=last + 0.5, ema_21=last + 1.5, sma_20=last + 2.5, sma_50=last + 3.5
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.6, reasoning="fixture"
        ),
        daily_candles=candles,
        daily_change_pct=-0.5,
    )


class TestTheGateMargin:
    def test_just_past_the_gate_triggers_with_almost_nothing(self) -> None:
        # (92.45 - 100) / 5 = -1.51 ATRs against a gate of 1.5.
        meta = TrendPersistenceStrategy().compute_signal(_grind(92.45)).metadata
        assert meta["bearish_trigger"] is True
        assert meta["gate_margin_atr"] == pytest.approx(0.01)
        assert meta["depth"] == pytest.approx(math.tanh(0.01 / 2.0), abs=1e-4)

    def test_short_of_the_gate_is_negative(self) -> None:
        signal = TrendPersistenceStrategy().compute_signal(_grind(93.0))
        assert signal.metadata["bearish_trigger"] is False
        assert signal.metadata["gate_margin_atr"] == pytest.approx(-0.1)
        assert signal.value == 0.0

    def test_the_vote_is_unchanged(self) -> None:
        signal = TrendPersistenceStrategy().compute_signal(_grind(85.0))
        meta = signal.metadata
        assert meta["gate_margin_atr"] == pytest.approx(1.5)
        expected = -0.75 * math.tanh(1.5 / 2.0) * meta["quality"]
        assert signal.value == pytest.approx(expected)
