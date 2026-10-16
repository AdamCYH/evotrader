"""A channel can read the daily MA stack where the regime classifier reads it.

Every channel read the stack one way: price above EMA9 above EMA21 AND SMA20
above SMA50 for bull, the mirror for bear. The SMA20/SMA50 cross lags a V-turn
by weeks, so after a rally a day that falls through every fast average still
reads ``no_stack``. The regime classifier already overrides that stale cross by
where price sits (its price-location override) and tags the tape bearish, while
the channels that key on the stack cannot see it: the with-trend sell leg of
the VWAP fade, mean_reversion's trend guard, both reclaim channels.

``stack_mode: location`` reads the stack the classifier's way;
``stack_mode: slow`` stays the default, so every stored vote is unchanged.
Made-up ticker and prices.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evotrader.algorithms import units
from evotrader.algorithms.strategies.gap_fail_continuation import GapFailContinuationStrategy
from evotrader.algorithms.strategies.intraday_vwap_zscore import IntradayVwapZscoreStrategy
from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.algorithms.strategies.trend_persistence import TrendPersistenceStrategy
from evotrader.algorithms.strategies.vwap_reclaim_continuation import (
    VwapReclaimContinuationStrategy,
)
from evotrader.algorithms.strategies.vwap_reclaim_fade import VwapReclaimFadeStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from tests.unit.test_vwap_reclaim_fade import NOW, VWAP, _bars, _bull_dip, _mirror


class _Ind:
    def __init__(self, ema_9, ema_21, sma_20, sma_50) -> None:
        self.ema_9, self.ema_21, self.sma_20, self.sma_50 = ema_9, ema_21, sma_20, sma_50


def _after_rally(price: float) -> dict[str, float]:
    """Price under every fast average while SMA20 still sits far above SMA50."""
    return {"ema_9": price + 3, "ema_21": price + 4, "sma_20": price + 8, "sma_50": price - 20}


# ── The helper ────────────────────────────────────────────────────────────


class TestTheHelper:
    def test_after_a_rally_only_location_sees_the_downtrend(self) -> None:
        ind = _Ind(**_after_rally(90.0))
        assert units.stack_context(90.0, ind, "slow") == "no_stack"
        assert units.stack_context(90.0, ind, "location") == "bear"
        assert units.stack_context(90.0, ind) == "no_stack", "slow is the default"
        assert units.sma_cross(ind) == "bull"

    def test_a_full_stack_reads_the_same_both_ways(self) -> None:
        bull = _Ind(108.0, 106.0, 104.0, 90.0)
        bear = _Ind(92.0, 94.0, 96.0, 110.0)
        for mode in units.STACK_MODES:
            assert units.stack_context(110.0, bull, mode) == "bull"
            assert units.stack_context(90.0, bear, mode) == "bear"

    def test_price_between_the_averages_is_no_stack(self) -> None:
        mixed = _Ind(98.0, 102.0, 104.0, 90.0)
        assert units.stack_context(100.0, mixed, "location") == "no_stack"
        assert units.stack_context(100.0, mixed, "slow") == "no_stack"

    @pytest.mark.parametrize("missing", ["ema_9", "ema_21", "sma_20", "sma_50"])
    def test_a_missing_average_is_no_stack_in_both_modes(self, missing: str) -> None:
        values = _after_rally(90.0)
        values[missing] = None
        for mode in units.STACK_MODES:
            assert units.stack_context(90.0, _Ind(**values), mode) == "no_stack"
        assert units.stack_context(None, _Ind(**_after_rally(90.0)), "location") == "no_stack"

    def test_the_mode_is_validated(self) -> None:
        assert units.validate_stack_mode("slow") == []
        assert units.validate_stack_mode("location") == []
        assert units.validate_stack_mode("fast") != []

    def test_the_regime_classifier_reads_the_same_rule(self) -> None:
        from evotrader.indicators import regime

        assert regime.location_stack is units.location_stack


# ── The VWAP fade ─────────────────────────────────────────────────────────

_ATR = 8.0


def _vwap_snapshot(price: float, vwap: float, mas: dict[str, float]) -> MarketSnapshot:
    ts = datetime(2026, 3, 11, 15, 30, tzinfo=UTC)
    closes = [vwap + 0.05 * i % 0.2 for i in range(24)]
    candles = [
        OHLCV(
            timestamp=datetime(2026, 3, 11, 14, (i * 5) % 60, tzinfo=UTC),
            open=c,
            high=c + 0.2,
            low=c - 0.2,
            close=c,
            volume=50000.0,
        )
        for i, c in enumerate(closes)
    ]
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=ts,
        quote=Quote(
            ticker="XYZ", bid=price - 0.01, ask=price + 0.01, last=price, volume=1e6, timestamp=ts
        ),
        indicators=TechnicalIndicators(
            vwap=vwap,
            vwap_anchor="current_session",
            atr_14=_ATR,
            relative_volume=0.8,
            **mas,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.5, reasoning="fixture"
        ),
        recent_candles=candles,
    )


def _fade(stack_mode: str) -> IntradayVwapZscoreStrategy:
    return IntradayVwapZscoreStrategy(
        countertrend_dampener=0.5, with_trend_trigger_scale=0.7, stack_mode=stack_mode
    )


class TestTheVwapFade:
    def test_the_dip_buy_after_a_rally_is_counter_trend_under_location(self) -> None:
        snap = _vwap_snapshot(97.0, 100.0, _after_rally(97.0))
        slow = _fade("slow").compute_signal(snap)
        location = _fade("location").compute_signal(snap)

        assert slow.metadata["leg"] == "no_stack"
        assert slow.value > 0, "it buys the dip at full strength"
        assert location.metadata["stack_context"] == "bear"
        assert location.metadata["leg"] == "counter_trend"
        assert location.value == pytest.approx(slow.value * 0.5)
        for sig, mode in ((slow, "slow"), (location, "location")):
            assert (sig.metadata["stack_mode"], sig.metadata["sma_cross"]) == (mode, "bull")

    def test_the_sell_above_vwap_becomes_the_with_trend_leg(self) -> None:
        snap = _vwap_snapshot(101.2, 100.0, _after_rally(101.2))
        slow = _fade("slow").compute_signal(snap)
        location = _fade("location").compute_signal(snap)

        assert slow.metadata["leg"] == "no_stack"
        assert location.metadata["leg"] == "with_trend"
        assert location.metadata["effective_entry_z"] == pytest.approx(
            slow.metadata["effective_entry_z"] * 0.7
        )

    def test_the_default_is_slow(self) -> None:
        snap = _vwap_snapshot(97.0, 100.0, _after_rally(97.0))
        assert IntradayVwapZscoreStrategy().compute_signal(snap).metadata["stack_mode"] == "slow"


# ── mean_reversion's trend guard ──────────────────────────────────────────


def _oversold(price: float) -> MarketSnapshot:
    ts = datetime(2026, 3, 11, 15, 30, tzinfo=UTC)
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=ts,
        quote=Quote(ticker="XYZ", bid=price, ask=price, last=price, volume=1e6, timestamp=ts),
        indicators=TechnicalIndicators(
            rsi_14=24.0,
            ibs=0.1,
            bollinger_upper=price + 30,
            bollinger_middle=price + 15,
            bollinger_lower=price - 1,
            **_after_rally(price),
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.5, reasoning="fixture"
        ),
    )


def test_mean_reversion_decays_the_buy_in_a_location_bear_stack() -> None:
    snap = _oversold(90.0)
    slow = MeanReversionStrategy(trend_fade_decay=0.2).compute_signal(snap)
    location = MeanReversionStrategy(trend_fade_decay=0.2, stack_mode="location").compute_signal(
        snap
    )
    assert (slow.metadata["trend_stack"], slow.metadata["trend_guard_applied"]) == ("none", False)
    assert (location.metadata["trend_stack"], location.metadata["trend_guard_applied"]) == (
        "bear",
        True,
    )
    assert slow.value > 0
    assert location.value == pytest.approx(slow.value * 0.2)
    assert location.metadata["sma_cross"] == "bull"


# ── The reclaim channels ──────────────────────────────────────────────────


def _reclaim_snapshot(price: float, closes: list[float]) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="XYZ",
        timestamp=NOW,
        quote=Quote(
            ticker="XYZ", bid=price - 0.01, ask=price + 0.01, last=price, volume=1e6, timestamp=NOW
        ),
        indicators=TechnicalIndicators(
            vwap=VWAP,
            vwap_anchor="current_session",
            atr_14=2.0,
            session_relative_volume=0.8,
            relative_volume=0.8,
            **_after_rally(price),
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.5, reasoning="fixture"
        ),
        recent_candles=_bars(closes),
    )


@pytest.mark.parametrize("channel", [VwapReclaimFadeStrategy, VwapReclaimContinuationStrategy])
def test_a_reclaim_channel_sees_the_location_bear_stack(channel) -> None:
    snap = _reclaim_snapshot(VWAP - 0.3, _mirror(_bull_dip()))
    slow = channel().compute_signal(snap).metadata
    location = channel(stack_mode="location").compute_signal(snap).metadata

    assert slow["out_of_scope_reason"] == "no_ma_stack"
    assert (location["bear_stack"], location["stack_mode"]) == (True, "location")
    assert location.get("out_of_scope_reason") != "no_ma_stack"


# ── Both readings where a rule keys on them ───────────────────────────────


def test_trend_persistence_reports_both_readings() -> None:
    closes = [100.0, 99.0, 98.0, 97.5, 96.0, 95.0, 94.0, 93.0]
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
    ts = datetime(2026, 3, 11, 15, 30, tzinfo=UTC)
    snap = MarketSnapshot(
        ticker="XYZ",
        timestamp=ts,
        quote=Quote(ticker="XYZ", bid=92.0, ask=92.0, last=92.0, volume=1e6, timestamp=ts),
        indicators=TechnicalIndicators(atr_14=2.0, **_after_rally(92.0)),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR, confidence=0.5, reasoning="fixture"
        ),
        daily_candles=candles,
    )
    meta = TrendPersistenceStrategy().compute_signal(snap).metadata
    assert (meta["bear_stack_slow"], meta["bear_stack_location"]) == (False, True)
    assert (meta["bear_stack"], meta["stack_mode"]) == (False, "slow"), "the gate reads slow"
    located = TrendPersistenceStrategy(stack_mode="location").compute_signal(snap).metadata
    assert located["bear_stack"] is True


# ── Every channel takes the parameter ─────────────────────────────────────


@pytest.mark.parametrize(
    "channel",
    [
        IntradayVwapZscoreStrategy,
        MeanReversionStrategy,
        VwapReclaimFadeStrategy,
        VwapReclaimContinuationStrategy,
        TrendPersistenceStrategy,
        GapFailContinuationStrategy,
    ],
)
def test_every_channel_takes_and_checks_stack_mode(channel) -> None:
    strategy = channel()
    assert strategy.get_parameters()["stack_mode"] == "slow"
    strategy.set_parameters({"stack_mode": "location"})
    assert strategy.get_parameters()["stack_mode"] == "location"
    assert strategy.validate_parameters({"stack_mode": "location"}) == []
    assert strategy.validate_parameters({"stack_mode": "sideways"}) != []
