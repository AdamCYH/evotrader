"""Regression tests for Code Review: 20260904_210922_vwap_zscore_staleness_annihilation_and_fake_zscore.

Guards against:
1. Exponential self-annihilation: freshness = decay ** stale_bars unbounded over window.
2. entry_z double-purposed as firing gate and staleness persistence threshold.
3. Off-by-one error: current bar counting itself as stale (brand-new extreme penalized).
4. Lack of diagnostic visibility into the min_std dispersion floor (sd_raw / sd_floored).
5. Near-zero emissions inflating composite participation and corroboration.

See: data/evolution/reviews/20260904_210922_vwap_zscore_staleness_annihilation_and_fake_zscore.md
"""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.intraday_vwap_zscore import (
    IntradayVwapZscoreStrategy,
)
from evotrader.models.market import (
    OHLCV,
    MarketRegime,
    MarketSnapshot,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)
from evotrader.models.signals import AlgoSignal


def _make_snapshot(
    price: float,
    vwap: float,
    atr: float,
    candle_closes: list[float],
    vwap_anchor: str = "current_session",
    relative_volume: float = 1.0,
) -> MarketSnapshot:
    candles = [
        OHLCV(
            timestamp=datetime(2026, 9, 4, 10, i * 5 % 60),
            open=c - 0.1,
            high=c + 0.2,
            low=c - 0.2,
            close=c,
            volume=50000.0,
        )
        for i, c in enumerate(candle_closes)
    ]
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 9, 4, 14, 0),
        quote=Quote(
            ticker="QQQ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=60e6,
            timestamp=datetime(2026, 9, 4, 14, 0),
        ),
        indicators=TechnicalIndicators(
            vwap=vwap,
            vwap_anchor=vwap_anchor,
            atr_14=atr,
            relative_volume=relative_volume,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.7,
            reasoning="test",
        ),
        recent_candles=candles,
    )


class TestVwapZscoreStalenessReview:
    """Tests guarding against findings in code review 20260904_210922."""

    def test_max_stale_bars_caps_exponent(self) -> None:
        """Finding 1: stale_bars must be bounded by max_stale_bars so freshness
        does not collapse to ~0 (0.65**20 = 0.00018).
        """
        vwap = 700.0
        atr = 3.0
        # 25 bars with extreme dislocation
        candle_closes = [vwap + 2.0 * atr] * 25
        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.1,
            staleness_decay=0.65,
            max_stale_bars=6,
            min_std=0.15,
        )
        snapshot = _make_snapshot(
            price=vwap + 2.0 * atr,
            vwap=vwap,
            atr=atr,
            candle_closes=candle_closes,
        )
        sig = strategy.compute_signal(snapshot)

        assert sig.metadata["stale_bars"] == 6
        expected_freshness = round(0.65**6, 4)
        assert sig.metadata["freshness"] == pytest.approx(expected_freshness, abs=1e-4)
        # Freshness is ~0.0754, NOT 0.00018
        assert sig.metadata["freshness"] > 0.05
        assert abs(sig.value) > 0.01

    def test_decoupled_stale_threshold_z(self) -> None:
        """Finding 2: stale_threshold_z can be set higher than entry_z so reachability
        does not automatically inflate stale_bars.
        """
        vwap = 700.0
        atr = 3.0
        # z = dev / 0.15. If dev = 0.20, z = 0.20 / 0.15 = 1.333
        # price = 700 + 0.20 * 3.0 = 700.60
        dev = 0.20
        disloc_price = vwap + dev * atr
        candle_closes = [disloc_price] * 20

        # entry_z = 1.1 (reachable), but stale_threshold_z = 1.5
        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.1,
            stale_threshold_z=1.5,
            min_std=0.15,
        )
        snapshot = _make_snapshot(
            price=disloc_price,
            vwap=vwap,
            atr=atr,
            candle_closes=candle_closes,
        )
        sig = strategy.compute_signal(snapshot)

        # z is ~1.33, which is >= entry_z (1.1) so signal fires
        assert sig.value != 0.0
        # But z (1.33) is < stale_threshold_z (1.5), so prior bars do not count as stale!
        assert sig.metadata["stale_bars"] == 0
        assert sig.metadata["freshness"] == 1.0

    def test_current_bar_excluded_from_stale_count(self) -> None:
        """Finding 3: Brand new extreme appearing on the current bar must score
        stale_bars = 0 and freshness = 1.0.
        """
        vwap = 700.0
        atr = 3.0
        # Prior 19 bars are right at VWAP (z = 0)
        normal_closes = [vwap] * 19
        # Current bar spikes to 2.0 ATR
        extreme_price = vwap + 2.0 * atr
        all_closes = normal_closes + [extreme_price]

        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.1,
            staleness_decay=0.65,
            min_std=0.15,
        )
        snapshot = _make_snapshot(
            price=extreme_price,
            vwap=vwap,
            atr=atr,
            candle_closes=all_closes,
        )
        sig = strategy.compute_signal(snapshot)

        assert sig.metadata["stale_bars"] == 0
        assert sig.metadata["freshness"] == 1.0

    def test_dispersion_floor_diagnostics(self) -> None:
        """Finding 4: Expose sd_raw and sd_floored in metadata so agents can see
        whether min_std is binding.
        """
        vwap = 700.0
        atr = 3.0
        # Flat closes -> sd_raw is ~0 -> sd_floored is True
        flat_closes = [vwap + 1.0 * atr] * 20
        strategy = IntradayVwapZscoreStrategy(entry_z=1.1, min_std=0.15)
        snapshot = _make_snapshot(
            price=vwap + 1.0 * atr,
            vwap=vwap,
            atr=atr,
            candle_closes=flat_closes,
        )
        sig = strategy.compute_signal(snapshot)

        assert "sd_raw" in sig.metadata
        assert "sd_floored" in sig.metadata
        assert sig.metadata["sd_floored"] is True
        assert sig.metadata["sd_raw"] < 0.15

        # Also test within_band includes the diagnostics
        within_snapshot = _make_snapshot(
            price=vwap + 0.05 * atr,
            vwap=vwap,
            atr=atr,
            candle_closes=[vwap + 0.05 * atr] * 20,
        )
        wb_sig = strategy.compute_signal(within_snapshot)
        assert wb_sig.value == 0.0
        assert wb_sig.metadata["reason"] == "within_band"
        assert wb_sig.metadata["sd_floored"] is True
        assert "sd_raw" in wb_sig.metadata

        # Volatile closes -> sd_raw > 0.15 -> sd_floored is False
        volatile_closes = [vwap + (2.0 if i % 2 == 0 else -2.0) * atr for i in range(20)]
        vol_snapshot = _make_snapshot(
            price=vwap + 2.0 * atr,
            vwap=vwap,
            atr=atr,
            candle_closes=volatile_closes,
        )
        vol_sig = strategy.compute_signal(vol_snapshot)
        assert vol_sig.metadata["sd_floored"] is False
        assert vol_sig.metadata["sd_raw"] > 0.15

    def test_parameter_round_trip_and_validation(self) -> None:
        """Verify get/set/validate_parameters handle max_stale_bars and stale_threshold_z."""
        strategy = IntradayVwapZscoreStrategy(
            max_stale_bars=8,
            stale_threshold_z=2.2,
        )
        params = strategy.get_parameters()
        assert params["max_stale_bars"] == 8
        assert params["stale_threshold_z"] == 2.2

        strategy.set_parameters({"max_stale_bars": 5, "stale_threshold_z": 1.7})
        assert strategy._max_stale_bars == 5
        assert strategy._stale_threshold_z == 1.7

        errors = strategy.validate_parameters({"max_stale_bars": -1, "stale_threshold_z": -0.5})
        assert len(errors) == 2


class _Stub:
    def __init__(self, name: str, value: float, applicable: bool = True):
        self.name = name
        self.value = value
        self.applicable = applicable

    def compute_signal(self, _):
        return AlgoSignal(
            name=self.name,
            value=self.value,
            weight=1.0,
            metadata={"applicable": self.applicable},
        )


class TestCompositeNearZeroParticipation:
    """Finding 5: Near-zero signals (< 1e-3) must not count as participation."""

    def test_near_zero_signal_treated_as_non_voting(self) -> None:
        # A sub-strategy emits 0.000164 (annihilated signal)
        stubs = {
            "normal": _Stub("normal", 0.6),
            "annihilated": _Stub("annihilated", 0.000164),
            "dark": _Stub("dark", 0.0, applicable=True),
        }
        weights = {"normal": 0.5, "annihilated": 0.25, "dark": 0.25}
        composite = CompositeStrategy(
            sub_strategies=stubs,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_snapshot(700.0, 700.0, 3.0, [700.0] * 20)
        signal = composite.compute_signal(snapshot)

        # live_signal_count should only be 1 (normal), NOT 2
        assert signal.metadata["live_signal_count"] == 1
        # annihilated should be in near_zero_signals
        assert "annihilated" in signal.metadata.get("near_zero_signals", "")
        # authoring signal should be normal
        assert signal.metadata.get("authoring_signal") == "normal"
