"""Tests for exposure-sizing strategies."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from evotrader.algorithms.exposure import VolatilityTargetStrategy
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import ExposureTarget


def _snapshot(closes: list[float], price: float | None = None) -> MarketSnapshot:
    """Snapshot whose daily_candles carry the given close series."""
    base = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [
        OHLCV(
            timestamp=base + timedelta(days=i),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=1_000_000.0,
        )
        for i, c in enumerate(closes)
    ]
    last = price if price is not None else (closes[-1] if closes else 100.0)
    ts = base + timedelta(days=len(closes))
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=ts,
        quote=Quote(
            ticker="QQQ", bid=last - 0.01, ask=last + 0.01, last=last, volume=1e6, timestamp=ts
        ),
        indicators=TechnicalIndicators(atr_14=2.0),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND, confidence=0.8, reasoning="test"
        ),
        daily_candles=candles,
    )


def _series(daily_vol: float, n: int = 80, start: float = 100.0) -> list[float]:
    """Deterministic close series with a known daily volatility."""
    closes, px = [start], start
    for i in range(1, n):
        px *= 1 + (daily_vol if i % 2 else -daily_vol)
        closes.append(px)
    return closes


class TestConstruction:
    def test_rejects_non_positive_target(self) -> None:
        with pytest.raises(ValueError, match="target_volatility"):
            VolatilityTargetStrategy(target_volatility=0.0)

    def test_rejects_inverted_bounds(self) -> None:
        with pytest.raises(ValueError, match="min_exposure"):
            VolatilityTargetStrategy(min_exposure=1.5, max_exposure=0.5)

    def test_rejects_negative_band(self) -> None:
        with pytest.raises(ValueError, match="rebalance_band"):
            VolatilityTargetStrategy(rebalance_band=-0.1)


class TestRealizedVolatility:
    def test_measures_known_volatility(self) -> None:
        s = VolatilityTargetStrategy(lookback_days=60)
        daily = 0.01
        rv = s.realized_volatility(_snapshot(_series(daily, n=80)))
        assert rv is not None
        assert rv == pytest.approx(daily * math.sqrt(252), rel=0.15)

    def test_returns_none_without_enough_history(self) -> None:
        s = VolatilityTargetStrategy(min_observations=20)
        assert s.realized_volatility(_snapshot(_series(0.01, n=5))) is None

    def test_returns_none_for_flat_series(self) -> None:
        s = VolatilityTargetStrategy()
        assert s.realized_volatility(_snapshot([100.0] * 80)) is None


class TestExposureSizing:
    def test_calm_market_increases_exposure(self) -> None:
        """Low volatility should size up, since risk per dollar is lower."""
        s = VolatilityTargetStrategy(target_volatility=0.20, rebalance_band=0.0)
        calm = s.compute_exposure(_snapshot(_series(0.005)), current_exposure=1.0)
        assert calm.value > 1.0
        assert calm.realized_volatility is not None and calm.realized_volatility < 0.20

    def test_turbulent_market_reduces_exposure(self) -> None:
        s = VolatilityTargetStrategy(target_volatility=0.20, rebalance_band=0.0)
        wild = s.compute_exposure(_snapshot(_series(0.04)), current_exposure=1.0)
        assert wild.value < 1.0
        assert wild.realized_volatility is not None and wild.realized_volatility > 0.20

    def test_exposure_is_inverse_to_volatility(self) -> None:
        s = VolatilityTargetStrategy(rebalance_band=0.0, min_exposure=0.0, max_exposure=3.0)
        calm = s.compute_exposure(_snapshot(_series(0.005)), 1.0).value
        mid = s.compute_exposure(_snapshot(_series(0.012)), 1.0).value
        wild = s.compute_exposure(_snapshot(_series(0.030)), 1.0).value
        assert calm > mid > wild

    def test_respects_max_exposure(self) -> None:
        s = VolatilityTargetStrategy(max_exposure=1.2, rebalance_band=0.0)
        r = s.compute_exposure(_snapshot(_series(0.001)), 1.0)
        assert r.value == pytest.approx(1.2)
        assert r.capped_by == "max_exposure"

    def test_respects_min_exposure(self) -> None:
        s = VolatilityTargetStrategy(min_exposure=0.4, rebalance_band=0.0)
        r = s.compute_exposure(_snapshot(_series(0.10)), 1.0)
        assert r.value == pytest.approx(0.4)
        assert r.capped_by == "min_exposure"

    def test_never_goes_short(self) -> None:
        """Exposure sizing is long-only by construction; direction is not its job."""
        s = VolatilityTargetStrategy(min_exposure=0.0, rebalance_band=0.0)
        r = s.compute_exposure(_snapshot(_series(0.25)), 1.0)
        assert r.value >= 0.0


class TestRebalanceBand:
    def test_small_drift_holds_position(self) -> None:
        s = VolatilityTargetStrategy(rebalance_band=0.50)  # very wide band
        snap = _snapshot(_series(0.012))
        r = s.compute_exposure(snap, current_exposure=1.0)
        assert r.value == pytest.approx(1.0)
        assert r.capped_by == "rebalance_band"
        assert r.metadata["rebalanced"] is False

    def test_large_drift_triggers_rebalance(self) -> None:
        s = VolatilityTargetStrategy(rebalance_band=0.01)
        r = s.compute_exposure(_snapshot(_series(0.004)), current_exposure=0.5)
        assert r.value != pytest.approx(0.5)
        assert r.metadata["rebalanced"] is True

    def test_wider_band_produces_fewer_changes(self) -> None:
        snaps = [_snapshot(_series(v)) for v in (0.006, 0.010, 0.014, 0.018, 0.022)]

        def count(band: float) -> int:
            s = VolatilityTargetStrategy(rebalance_band=band)
            cur, n = 1.0, 0
            for snap in snaps:
                nxt = s.compute_exposure(snap, cur).value
                if nxt != cur:
                    n += 1
                cur = nxt
            return n

        assert count(0.30) < count(0.001)


class TestInsufficientHistory:
    def test_holds_current_exposure_rather_than_guessing(self) -> None:
        s = VolatilityTargetStrategy()
        r = s.compute_exposure(_snapshot(_series(0.01, n=4)), current_exposure=0.75)
        assert r.value == pytest.approx(0.75)
        assert r.capped_by == "insufficient_history"
        assert r.realized_volatility is None

    def test_clamps_held_exposure_to_bounds(self) -> None:
        s = VolatilityTargetStrategy(max_exposure=1.0)
        r = s.compute_exposure(_snapshot([], price=100.0), current_exposure=2.5)
        assert r.value == pytest.approx(1.0)


class TestExposureTargetModel:
    def test_flat_and_levered_flags(self) -> None:
        flat = ExposureTarget(value=0.0, target_volatility=0.2)
        lev = ExposureTarget(value=1.4, target_volatility=0.2)
        plain = ExposureTarget(value=1.0, target_volatility=0.2)
        assert flat.is_flat and not flat.is_levered
        assert lev.is_levered and not lev.is_flat
        assert not plain.is_flat and not plain.is_levered

    def test_rejects_negative_exposure(self) -> None:
        with pytest.raises(ValueError):
            ExposureTarget(value=-0.5, target_volatility=0.2)
