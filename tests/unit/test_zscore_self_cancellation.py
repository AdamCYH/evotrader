"""Regression tests: Z-score self-cancellation fix and participation accounting.

Guards against:
1. VWAP z-score computing z = (deviation - mu) / sd which causes persistent
   dislocations to cancel themselves (mu absorbs the dislocation → z → 0).
2. Low-participation attenuation counting silent channels (value=0.0,
   applicable=True, reason='within_band') as participating.

See: data/evolution/reviews/20260812_033555_20260812_exit_deadlock_and_zscore_self_cancellation.md
"""

from __future__ import annotations

from datetime import datetime

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

# ── Helpers ──────────────────────────────────────────────────────


def _make_vwap_snapshot(
    price: float,
    vwap: float,
    atr: float,
    candle_closes: list[float],
    vwap_anchor: str = "current_session",
    relative_volume: float = 1.0,
) -> MarketSnapshot:
    """Build a MarketSnapshot with controlled VWAP z-score inputs."""
    candles = []
    for i, close in enumerate(candle_closes):
        total_minutes = i * 5
        hour = 10 + total_minutes // 60
        minute = total_minutes % 60
        candles.append(
            OHLCV(
                timestamp=datetime(2026, 8, 12, hour, minute),
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=50000.0,
            )
        )

    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 8, 12, 14, 0),
        quote=Quote(
            ticker="QQQ",
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            volume=60e6,
            timestamp=datetime(2026, 8, 12, 14, 0),
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


# ══════════════════════════════════════════════════════════════════
# Finding #2: Z-Score Self-Cancellation
# ══════════════════════════════════════════════════════════════════


class TestZscoreSelfCancellation:
    """Verify the mu-subtraction bug is fixed."""

    def test_persistent_dislocation_produces_nonzero_z(self) -> None:
        """The core regression: price persistently above VWAP must NOT
        produce z ≈ 0.  With the old code, mu → deviation → z → 0.
        """
        vwap = 700.0
        atr = 3.0
        # Price ~1.5 ATR above VWAP for the entire window
        dislocation_price = vwap + 1.5 * atr  # 704.5
        candle_closes = [dislocation_price] * 25

        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.1,
            z_scale=0.5,
            zscore_window=20,
            min_std=0.15,
        )
        snapshot = _make_vwap_snapshot(
            price=dislocation_price,
            vwap=vwap,
            atr=atr,
            candle_closes=candle_closes,
        )
        signal = strategy.compute_signal(snapshot)

        # With the old formula: z = (deviation - mu) / sd
        #   deviation = 1.5, mu ≈ 1.5, z ≈ 0 → silent
        # With the fix: z = deviation / sd
        #   deviation = 1.5, sd is small (flat), z >> entry_z → signal fires
        z = signal.metadata.get("z", 0.0)
        assert abs(z) > 1.0, (
            f"Persistent dislocation should produce |z| > 1.0, got {z}. "
            "mu-subtraction may still be active."
        )

    def test_varying_deviations_still_produce_signal(self) -> None:
        """A window with genuine variation should still produce z-scores
        that reflect the current deviation magnitude.
        """
        vwap = 700.0
        atr = 3.0
        # Build a window where most bars are near VWAP, but current price is far above
        normal_closes = [vwap + 0.1 * atr * (i % 5 - 2) for i in range(20)]
        extreme_price = vwap + 2.5 * atr  # 707.5, well above

        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.1,
            z_scale=0.5,
            zscore_window=20,
            min_std=0.15,
        )
        snapshot = _make_vwap_snapshot(
            price=extreme_price,
            vwap=vwap,
            atr=atr,
            candle_closes=normal_closes,
        )
        signal = strategy.compute_signal(snapshot)

        z = signal.metadata.get("z", 0.0)
        assert abs(z) > 1.5, f"Extreme deviation should produce large |z|, got {z}"
        # Signal should be negative (fading the overextension above VWAP)
        assert signal.value < 0.0, "Should fade overextension above VWAP"

    def test_staleness_decay_works_without_mu(self) -> None:
        """Staleness decay should still fire: if many trailing bars are
        beyond entry_z, freshness decays toward zero.
        """
        vwap = 700.0
        atr = 3.0
        # All bars at extreme deviation → stale
        extreme_closes = [vwap + 2.0 * atr] * 25

        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.1,
            z_scale=0.5,
            staleness_decay=0.65,
            zscore_window=20,
            min_std=0.15,
        )
        snapshot = _make_vwap_snapshot(
            price=vwap + 2.0 * atr,
            vwap=vwap,
            atr=atr,
            candle_closes=extreme_closes,
        )
        signal = strategy.compute_signal(snapshot)

        freshness = signal.metadata.get("freshness", 1.0)
        stale_bars = signal.metadata.get("stale_bars", 0)
        assert stale_bars > 0, "Should detect stale bars beyond threshold"
        assert freshness < 0.5, f"Staleness should decay freshness, got {freshness}"

    def test_within_band_z_metadata_accurate(self) -> None:
        """When z is within the entry band, the reported z should still
        reflect the dispersion-only calculation.
        """
        vwap = 700.0
        atr = 3.0
        # Slight deviation, within band
        mild_closes = [vwap + 0.2 * atr] * 25

        strategy = IntradayVwapZscoreStrategy(
            entry_z=1.9,
            zscore_window=20,
            min_std=0.15,
        )
        snapshot = _make_vwap_snapshot(
            price=vwap + 0.2 * atr,
            vwap=vwap,
            atr=atr,
            candle_closes=mild_closes,
        )
        signal = strategy.compute_signal(snapshot)

        assert signal.value == 0.0, "Should be within band"
        assert signal.metadata["reason"] == "within_band"
        # The z-score should NOT be ~0 just because deviation ≈ mu
        # With dispersion-only: z = deviation / sd, and sd is small (flat data)
        # so z should be substantial even for a mild deviation
        z = signal.metadata.get("z", 0.0)
        assert abs(z) > 0.5, f"Even mild persistent deviation should report nonzero z, got {z}"


# ══════════════════════════════════════════════════════════════════
# Finding #3: Participation Accounting
# ══════════════════════════════════════════════════════════════════


class _StubStrategy:
    """Minimal strategy stub for testing composite participation logic."""

    def __init__(
        self, name: str, signal_value: float, applicable: bool = True, reason: str = ""
    ) -> None:
        self._name = name
        self._value = signal_value
        self._applicable = applicable
        self._reason = reason

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "v001"

    @property
    def description(self) -> str:
        return f"stub:{self._name}"

    def compute_signal(self, snapshot):
        from evotrader.models.signals import AlgoSignal

        meta: dict = {"applicable": self._applicable}
        if self._reason:
            meta["reason"] = self._reason
        return AlgoSignal(
            name=self._name,
            value=self._value,
            weight=1.0,
            metadata=meta,
        )

    def get_parameters(self):
        return {}

    def set_parameters(self, params):
        pass

    def validate_parameters(self, params):
        return []


def _make_simple_snapshot() -> MarketSnapshot:
    """Minimal snapshot for composite participation tests."""
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 8, 12, 14, 0),
        quote=Quote(
            ticker="QQQ",
            bid=700.0,
            ask=700.02,
            last=700.01,
            volume=60e6,
            timestamp=datetime(2026, 8, 12, 14, 0),
        ),
        indicators=TechnicalIndicators(
            rsi_14=50.0,
            atr_14=3.0,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.7,
            reasoning="test",
        ),
    )


class TestParticipationAccounting:
    """Verify that within_band channels are not counted as participating."""

    def test_silent_channels_do_not_inflate_participation(self) -> None:
        """When most channels return 0.0 with applicable=True (within_band),
        the participation ratio should reflect only non-zero signals, and
        the attenuation guard should activate.
        """
        # 9 strategies: 2 emit non-zero, 7 return 0.0 with applicable=True
        stubs = {
            "momentum": _StubStrategy("momentum", 0.5),
            "mean_reversion": _StubStrategy("mean_reversion", 0.3),
            "gap": _StubStrategy("gap", 0.0, applicable=True, reason="within_band"),
            "vwap": _StubStrategy("vwap", 0.0, applicable=True, reason="within_band"),
            "event": _StubStrategy("event", 0.0, applicable=True, reason="within_band"),
            "range_break": _StubStrategy("range_break", 0.0, applicable=True, reason="within_band"),
            "options": _StubStrategy("options", 0.0, applicable=True, reason="within_band"),
            "swing": _StubStrategy("swing", 0.0, applicable=True, reason="within_band"),
            "trend": _StubStrategy("trend", 0.0, applicable=True, reason="within_band"),
        }
        weights = {name: 1.0 / len(stubs) for name in stubs}

        composite = CompositeStrategy(
            sub_strategies=stubs,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_simple_snapshot()

        # Without the fix: participation = 9/9 = 100% → no attenuation
        # With the fix: participation = 2/9 ≈ 22% < 40% → attenuation kicks in
        signal = composite.compute_signal(snapshot)

        # The attenuation should reduce the composite value
        # Unattenuated composite with renorm over 2 voting: ~0.4
        # Attenuated by participation/0.4 = (2/9)/0.4 ≈ 0.555 → ~0.22
        assert abs(signal.value) < 0.3, (
            f"Low-participation attenuation should reduce composite, got {signal.value}"
        )

    def test_all_channels_voting_no_attenuation(self) -> None:
        """When all channels emit non-zero signals, no attenuation should occur."""
        stubs = {
            "a": _StubStrategy("a", 0.5),
            "b": _StubStrategy("b", 0.3),
            "c": _StubStrategy("c", 0.2),
        }
        weights = {name: 1.0 / len(stubs) for name in stubs}

        composite = CompositeStrategy(
            sub_strategies=stubs,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_simple_snapshot()
        signal = composite.compute_signal(snapshot)

        # participation = 3/3 = 100% → no attenuation
        # expected: weighted avg = (0.5 + 0.3 + 0.2) / 3 ≈ 0.333
        expected = (0.5 + 0.3 + 0.2) / 3
        assert abs(signal.value - expected) < 0.01, f"Expected ~{expected:.3f}, got {signal.value}"

    def test_abstaining_channels_excluded_and_attenuated(self) -> None:
        """Channels with applicable=True but voting 0.0 (silent within band)
        are in the denominator and low-participation attenuation applies.
        """
        stubs = {
            "a": _StubStrategy("a", 0.8),
            "b": _StubStrategy("b", 0.0, applicable=True),
            "c": _StubStrategy("c", 0.0, applicable=True),
            "d": _StubStrategy("d", 0.0, applicable=True),
            "e": _StubStrategy("e", 0.0, applicable=True),
        }
        weights = {name: 1.0 / len(stubs) for name in stubs}

        composite = CompositeStrategy(
            sub_strategies=stubs,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_simple_snapshot()
        signal = composite.compute_signal(snapshot)

        # 5 applicable, 1 voting → participation = 1/5 = 20%
        # Attenuation: 0.2/0.4 = 0.5 → composite halved
        assert abs(signal.value) < 0.5, f"Low participation should attenuate, got {signal.value}"

    def test_non_applicable_channels_do_not_dilute_denominator(self) -> None:
        """Channels with applicable=False (e.g. missing options/gap context)
        do not dilute the participation denominator.
        """
        stubs = {
            "a": _StubStrategy("a", 0.8),
            "b": _StubStrategy("b", 0.0, applicable=False),
            "c": _StubStrategy("c", 0.0, applicable=False),
            "d": _StubStrategy("d", 0.0, applicable=False),
            "e": _StubStrategy("e", 0.0, applicable=False),
        }
        weights = {name: 1.0 / len(stubs) for name in stubs}

        composite = CompositeStrategy(
            sub_strategies=stubs,
            weights=weights,
            regime_adaptive=False,
            renormalize_on_abstain=True,
        )
        snapshot = _make_simple_snapshot()
        signal = composite.compute_signal(snapshot)

        # 1 applicable, 1 voting → participation = 1/1 = 100% (no attenuation haircut)
        assert abs(signal.value - 0.8) < 1e-4, (
            f"Non-applicable channels should not dilute denominator, got {signal.value}"
        )
