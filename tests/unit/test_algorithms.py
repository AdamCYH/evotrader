"""Tests for trading algorithm strategies."""

from __future__ import annotations

from datetime import datetime

import pytest

from evotrader.algorithms.composite import CompositeStrategy
from evotrader.algorithms.strategies.gap import GapStrategy
from evotrader.algorithms.strategies.mean_reversion import MeanReversionStrategy
from evotrader.algorithms.strategies.momentum import MomentumStrategy
from evotrader.algorithms.strategies.options_positioning import (
    OptionsPositioningStrategy,
)
from evotrader.models.market import (
    MarketRegime,
    MarketSnapshot,
    OptionsContext,
    Quote,
    RegimeClassification,
    TechnicalIndicators,
)


@pytest.fixture
def bullish_snapshot() -> MarketSnapshot:
    """Snapshot with bullish indicators."""
    return MarketSnapshot(
        ticker="SPY",
        timestamp=datetime(2026, 6, 18, 14, 30),
        quote=Quote(
            ticker="SPY",
            bid=455.0,
            ask=455.02,
            last=455.01,
            volume=60e6,
            timestamp=datetime(2026, 6, 18, 14, 30),
        ),
        indicators=TechnicalIndicators(
            rsi_14=35.0,  # Mildly oversold
            macd_line=0.8,
            macd_signal=0.3,
            macd_histogram=0.5,
            bollinger_upper=460.0,
            bollinger_middle=452.0,
            bollinger_lower=444.0,
            ema_9=454.5,
            ema_21=453.0,
            sma_20=452.0,
            sma_50=448.0,
            vwap=453.0,
            ibs=0.25,  # Oversold IBS
            atr_14=3.5,
            volume_sma_20=50e6,
            relative_volume=1.2,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.7,
            reasoning="ADX below 25",
        ),
        daily_change_pct=0.2,
        gap_pct=0.1,
    )


@pytest.fixture
def bearish_snapshot() -> MarketSnapshot:
    """Snapshot with bearish indicators."""
    return MarketSnapshot(
        ticker="SPY",
        timestamp=datetime(2026, 6, 18, 14, 30),
        quote=Quote(
            ticker="SPY",
            bid=440.0,
            ask=440.02,
            last=440.01,
            volume=70e6,
            timestamp=datetime(2026, 6, 18, 14, 30),
        ),
        indicators=TechnicalIndicators(
            rsi_14=78.0,  # Overbought
            macd_line=-0.5,
            macd_signal=0.1,
            macd_histogram=-0.6,
            bollinger_upper=445.0,
            bollinger_middle=440.0,
            bollinger_lower=435.0,
            ema_9=439.0,
            ema_21=441.0,
            sma_20=442.0,
            sma_50=445.0,
            vwap=442.0,
            ibs=0.85,  # Overbought IBS
            atr_14=4.0,
            volume_sma_20=50e6,
            relative_volume=1.5,
        ),
        regime=RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR,
            confidence=0.8,
            reasoning="SMA20 < SMA50",
        ),
        daily_change_pct=-0.5,
        gap_pct=-0.8,
    )


class TestMomentumStrategy:
    def test_bullish_signal(self, bullish_snapshot: MarketSnapshot) -> None:
        strategy = MomentumStrategy()
        signal = strategy.compute_signal(bullish_snapshot)
        assert signal.name == "momentum"
        assert signal.value > 0  # Should be bullish

    def test_bearish_signal(self, bearish_snapshot: MarketSnapshot) -> None:
        strategy = MomentumStrategy()
        signal = strategy.compute_signal(bearish_snapshot)
        assert signal.value < 0  # Should be bearish

    def test_signal_bounded(self, bullish_snapshot: MarketSnapshot) -> None:
        strategy = MomentumStrategy()
        signal = strategy.compute_signal(bullish_snapshot)
        assert -1.0 <= signal.value <= 1.0

    def test_parameter_roundtrip(self) -> None:
        strategy = MomentumStrategy(ema_short=5, ema_long=15)
        params = strategy.get_parameters()
        assert params["ema_short"] == 5
        new_strategy = MomentumStrategy()
        new_strategy.set_parameters(params)
        assert new_strategy.get_parameters()["ema_short"] == 5

    def test_divergence_decay_activates_on_sign_mismatch(self) -> None:
        """When daily-MA signal is bearish but intraday is strongly bullish,
        the signal should be decayed (7/17-7/21 saturation fix)."""
        snapshot = MarketSnapshot(
            ticker="QQQ",
            timestamp=datetime(2026, 7, 21, 14, 0),
            quote=Quote(
                ticker="QQQ",
                bid=500.0,
                ask=500.02,
                last=500.01,
                volume=60e6,
                timestamp=datetime(2026, 7, 21, 14, 0),
            ),
            indicators=TechnicalIndicators(
                # Bearish MA setup: EMA9 < EMA21, SMA20 < SMA50
                ema_9=498.0,
                ema_21=501.0,
                sma_20=499.0,
                sma_50=503.0,
                macd_line=-1.0,
                macd_signal=-0.5,
                macd_histogram=-0.5,
                atr_14=5.0,
                volume_sma_20=50e6,
                relative_volume=1.2,
            ),
            regime=RegimeClassification(
                regime=MarketRegime.RANGE_BOUND,
                confidence=0.7,
                reasoning="test",
            ),
            # Strong bullish intraday move contradicts bearish MAs
            daily_change_pct=1.5,
        )
        strategy_no_decay = MomentumStrategy(
            divergence_day_change_pct=100.0,  # Threshold too high to trigger
        )
        strategy_with_decay = MomentumStrategy(
            intraday_divergence_decay=0.5,
            divergence_day_change_pct=1.0,  # 1% threshold — triggers on 1.5%
        )

        sig_no_decay = strategy_no_decay.compute_signal(snapshot)
        sig_with_decay = strategy_with_decay.compute_signal(snapshot)

        # Both should be negative (bearish MAs), but decayed version
        # should be closer to zero.
        assert sig_no_decay.value < 0
        assert sig_with_decay.value < 0
        assert abs(sig_with_decay.value) < abs(sig_no_decay.value)
        assert sig_with_decay.metadata["divergence_applied"] is True
        assert sig_no_decay.metadata["divergence_applied"] is False

    def test_no_divergence_decay_when_same_sign(
        self,
        bullish_snapshot: MarketSnapshot,
    ) -> None:
        """When MA signal and daily change agree, no decay should apply."""
        strategy = MomentumStrategy(
            intraday_divergence_decay=0.5,
            divergence_day_change_pct=0.1,
        )
        signal = strategy.compute_signal(bullish_snapshot)
        # bullish_snapshot has positive MAs and positive daily_change_pct
        assert signal.metadata["divergence_applied"] is False

    def test_no_divergence_decay_below_threshold(self) -> None:
        """Small intraday moves should not trigger divergence decay."""
        snapshot = MarketSnapshot(
            ticker="QQQ",
            timestamp=datetime(2026, 7, 21, 14, 0),
            quote=Quote(
                ticker="QQQ",
                bid=500.0,
                ask=500.02,
                last=500.01,
                volume=60e6,
                timestamp=datetime(2026, 7, 21, 14, 0),
            ),
            indicators=TechnicalIndicators(
                ema_9=498.0,
                ema_21=501.0,
                sma_20=499.0,
                sma_50=503.0,
                macd_line=-1.0,
                macd_signal=-0.5,
                macd_histogram=-0.5,
                relative_volume=1.2,
                volume_sma_20=50e6,
            ),
            regime=RegimeClassification(
                regime=MarketRegime.RANGE_BOUND,
                confidence=0.7,
                reasoning="test",
            ),
            # Tiny positive move — below threshold
            daily_change_pct=0.2,
        )
        strategy = MomentumStrategy(
            intraday_divergence_decay=0.5,
            divergence_day_change_pct=1.0,
        )
        signal = strategy.compute_signal(snapshot)
        assert signal.metadata["divergence_applied"] is False

    def test_volume_signal_computed_once(
        self,
        bullish_snapshot: MarketSnapshot,
    ) -> None:
        """Metadata should contain both raw and applied volume multipliers."""
        strategy = MomentumStrategy()
        signal = strategy.compute_signal(bullish_snapshot)
        assert "volume_multiplier_raw" in signal.metadata
        assert "volume_multiplier_applied" in signal.metadata

    def test_divergence_params_roundtrip(self) -> None:
        """New divergence params should survive get/set roundtrip."""
        strategy = MomentumStrategy(
            intraday_divergence_decay=0.3,
            divergence_day_change_pct=2.0,
        )
        params = strategy.get_parameters()
        assert params["intraday_divergence_decay"] == 0.3
        assert params["divergence_day_change_pct"] == 2.0

        new_strategy = MomentumStrategy()
        new_strategy.set_parameters(params)
        assert new_strategy.get_parameters()["intraday_divergence_decay"] == 0.3

    def test_divergence_param_validation(self) -> None:
        """Divergence params should be validated."""
        strategy = MomentumStrategy()
        errors = strategy.validate_parameters({"intraday_divergence_decay": 1.5})
        assert any("intraday_divergence_decay" in e for e in errors)

        errors = strategy.validate_parameters({"divergence_day_change_pct": -0.01})
        assert any("divergence_day_change_pct" in e for e in errors)


class TestMeanReversionStrategy:
    def test_oversold_buy_signal(self, bullish_snapshot: MarketSnapshot) -> None:
        strategy = MeanReversionStrategy()
        signal = strategy.compute_signal(bullish_snapshot)
        assert signal.name == "mean_reversion"
        # RSI (35) and IBS (0.25) are in oversold territory → positive signals
        assert signal.metadata["rsi_signal"] > 0
        assert signal.metadata["ibs_signal"] > 0

    def test_overbought_sell_signal(self, bearish_snapshot: MarketSnapshot) -> None:
        strategy = MeanReversionStrategy()
        signal = strategy.compute_signal(bearish_snapshot)
        assert signal.value < 0  # RSI overbought + IBS overbought → sell

    def test_validation(self) -> None:
        strategy = MeanReversionStrategy()
        errors = strategy.validate_parameters({"rsi_oversold": 80, "rsi_overbought": 20})
        assert len(errors) > 0  # oversold must be < overbought


class TestGapStrategy:
    def test_no_significant_gap(self, bullish_snapshot: MarketSnapshot) -> None:
        strategy = GapStrategy()
        signal = strategy.compute_signal(bullish_snapshot)
        assert abs(signal.value) < 0.2  # Small gap = weak signal

    def test_large_gap_down_fade(self) -> None:
        """Large gap down should produce a buy (fade) signal."""
        snapshot = MarketSnapshot(
            ticker="SPY",
            timestamp=datetime(2026, 6, 18, 9, 45),
            quote=Quote(
                ticker="SPY",
                bid=445.0,
                ask=445.02,
                last=445.01,
                volume=80e6,
                timestamp=datetime(2026, 6, 18, 9, 45),
            ),
            indicators=TechnicalIndicators(),
            regime=RegimeClassification(
                regime=MarketRegime.RANGE_BOUND,
                confidence=0.7,
                reasoning="test",
            ),
            gap_pct=-1.5,  # 1.5% gap down
            daily_change_pct=-1.0,
        )
        strategy = GapStrategy()
        signal = strategy.compute_signal(snapshot)
        assert signal.value > 0  # Fade the gap down → buy


@pytest.fixture
def composite_strategy() -> CompositeStrategy:
    """Fixture to build a composite strategy with standard sub-strategies."""
    return CompositeStrategy(
        sub_strategies={
            "momentum": MomentumStrategy(),
            "mean_reversion": MeanReversionStrategy(),
            "gap": GapStrategy(),
        },
        weights={
            "momentum": 0.35,
            "mean_reversion": 0.40,
            "gap": 0.25,
        },
    )


class TestCompositeStrategy:
    def test_produces_signal(
        self, composite_strategy: CompositeStrategy, bullish_snapshot: MarketSnapshot
    ) -> None:
        signal = composite_strategy.compute_signal(bullish_snapshot)
        assert signal.name == "composite"
        assert -1.0 <= signal.value <= 1.0

    def test_detailed_signal(
        self, composite_strategy: CompositeStrategy, bullish_snapshot: MarketSnapshot
    ) -> None:
        detailed = composite_strategy.compute_detailed_signal(bullish_snapshot)
        assert len(detailed.signals) == 3
        assert detailed.algo_version.startswith("composite")

    def test_regime_adaptive_weights(self, composite_strategy: CompositeStrategy) -> None:
        # Trending bull should emphasise momentum
        weights = composite_strategy._get_regime_weights(MarketRegime.TRENDING_BULL)
        assert weights["momentum"] > weights["mean_reversion"]
        # Range-bound: with the 5-strategy defaults normalized to 3,
        # momentum (0.35) > mean_reversion (0.25), but MR > gap (0.20).
        weights = composite_strategy._get_regime_weights(MarketRegime.RANGE_BOUND)
        assert weights["mean_reversion"] > weights["gap"]

    def test_weight_validation(self, composite_strategy: CompositeStrategy) -> None:
        errors = composite_strategy.validate_parameters(
            {"momentum_weight": 0.5, "mean_reversion_weight": 0.3, "gap_weight": 0.1}
        )
        assert len(errors) > 0  # Doesn't sum to 1.0

    def test_parameter_evolution(self, composite_strategy: CompositeStrategy) -> None:
        composite_strategy.set_parameters(
            {
                "momentum_weight": 0.30,
                "mean_reversion_weight": 0.45,
                "gap_weight": 0.25,
            }
        )
        params = composite_strategy.get_parameters()
        assert params["momentum_weight"] == 0.30
        assert params["mean_reversion_weight"] == 0.45


# ---------------------------------------------------------------------------
# Options Positioning Strategy Tests
# ---------------------------------------------------------------------------


def _make_snapshot_with_options(
    opt: OptionsContext | None = None,
    daily_change_pct: float | None = None,
) -> MarketSnapshot:
    """Helper to create a minimal snapshot with optional options context."""
    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 7, 21, 14, 0),
        quote=Quote(
            ticker="QQQ",
            bid=500.0,
            ask=500.02,
            last=500.01,
            volume=60e6,
            timestamp=datetime(2026, 7, 21, 14, 0),
        ),
        indicators=TechnicalIndicators(),
        regime=RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.7,
            reasoning="test",
        ),
        daily_change_pct=daily_change_pct,
        options_context=opt,
    )


class TestOptionsPositioningStrategy:
    def test_abstains_without_options_data(self) -> None:
        """No options_context → neutral with applicable=False."""
        strategy = OptionsPositioningStrategy()
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=None))
        assert signal.value == 0.0
        assert signal.metadata["applicable"] is False

    def test_abstains_with_empty_options(self) -> None:
        """Options context present but pc_volume_ratio is None → abstain."""
        strategy = OptionsPositioningStrategy()
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=OptionsContext()))
        assert signal.value == 0.0
        assert signal.metadata["applicable"] is False

    def test_contrarian_long_on_extreme_put_skew(self) -> None:
        """Extreme put skew with no event → positive contrarian signal."""
        strategy = OptionsPositioningStrategy(
            pcr_baseline=1.0,
            pcr_std=0.5,
            extreme_z=2.0,
        )
        opt = OptionsContext(pc_volume_ratio=3.0)  # z = (3-1)/0.5 = 4.0
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=opt))
        assert signal.value > 0  # Contrarian LONG
        assert not signal.metadata["event_pending"]

    def test_contrarian_short_on_extreme_call_skew(self) -> None:
        """Extreme call skew with no event → negative contrarian signal."""
        strategy = OptionsPositioningStrategy(
            pcr_baseline=1.0,
            pcr_std=0.5,
            extreme_z=2.0,
        )
        opt = OptionsContext(pc_volume_ratio=0.0)  # z = (0-1)/0.5 = -2.0
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=opt))
        assert signal.value < 0  # Contrarian SHORT

    def test_neutral_during_event_pending_with_elevated_pcr(self) -> None:
        """Elevated P/C pre-event = hedging noise → neutral."""
        strategy = OptionsPositioningStrategy(event_window_hours=36.0)
        opt = OptionsContext(
            pc_volume_ratio=2.5,  # Elevated
            event_hours_away=12.0,  # Event pending
        )
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=opt))
        assert signal.value == 0.0
        assert signal.metadata["event_pending"] is True

    def test_mild_confirmation_signal(self) -> None:
        """Mild P/C readings → small confirmation lean."""
        strategy = OptionsPositioningStrategy(
            pcr_baseline=1.0,
            pcr_std=0.5,
        )
        # pcr_z = (1.3 - 1.0) / 0.5 = 0.6 (mild positive)
        opt = OptionsContext(pc_volume_ratio=1.3)
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=opt))
        # Confirmation mode: lean WITH positioning.
        # Positive pcr_z → negative confirmation (puts dominate = bearish confirm)
        assert -0.25 <= signal.value <= 0.25
        assert signal.value < 0  # Mild bearish confirmation

    def test_post_event_boost(self) -> None:
        """Post-event P/C collapse → amplified long lean."""
        strategy = OptionsPositioningStrategy(
            post_event_hours=48.0,
            unwind_threshold=0.5,
            post_event_kicker=0.3,
        )
        opt = OptionsContext(
            pc_volume_ratio=1.2,
            event_hours_since=6.0,  # Event just cleared
            pc_ratio_change=-0.8,  # P/C collapsing (hedges unwinding)
        )
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=opt))
        assert signal.value > 0  # Boosted long lean
        assert signal.metadata["post_event_boost"] is True

    def test_signal_bounded(self) -> None:
        """All outputs must be in [-1.0, +1.0]."""
        strategy = OptionsPositioningStrategy()
        opt = OptionsContext(pc_volume_ratio=10.0)  # Extreme
        signal = strategy.compute_signal(_make_snapshot_with_options(opt=opt))
        assert -1.0 <= signal.value <= 1.0

    def test_parameter_roundtrip(self) -> None:
        """Parameters should survive get/set roundtrip."""
        strategy = OptionsPositioningStrategy(
            pcr_baseline=1.2,
            extreme_z=1.5,
        )
        params = strategy.get_parameters()
        assert params["pcr_baseline"] == 1.2
        assert params["extreme_z"] == 1.5

        new_strategy = OptionsPositioningStrategy()
        new_strategy.set_parameters(params)
        assert new_strategy.get_parameters()["pcr_baseline"] == 1.2

    def test_validate_rejects_invalid_params(self) -> None:
        """Validation should catch invalid parameter values."""
        strategy = OptionsPositioningStrategy()

        errors = strategy.validate_parameters({"pcr_std": -0.5})
        assert any("pcr_std" in e for e in errors)

        errors = strategy.validate_parameters({"extreme_z": 0})
        assert any("extreme_z" in e for e in errors)

        errors = strategy.validate_parameters({"contrarian_scale": 1.5})
        assert any("contrarian_scale" in e for e in errors)

        errors = strategy.validate_parameters({"post_event_kicker": -0.1})
        assert any("post_event_kicker" in e for e in errors)

    def test_validate_accepts_valid_params(self) -> None:
        """Valid parameters should pass validation with no errors."""
        strategy = OptionsPositioningStrategy()
        errors = strategy.validate_parameters(strategy.get_parameters())
        assert errors == []


# ---------------------------------------------------------------------------
# Swing Failure Reversal Strategy Tests
# ---------------------------------------------------------------------------


def _make_sfr_snapshot(
    candles: list | None = None,
    vwap: float | None = None,
    vwap_anchor: str | None = "current_session",
    atr: float | None = 3.5,
    relative_volume: float | None = 1.0,
) -> MarketSnapshot:
    """Helper to create a snapshot for swing failure reversal tests."""

    return MarketSnapshot(
        ticker="QQQ",
        timestamp=datetime(2026, 7, 27, 14, 0),
        quote=Quote(
            ticker="QQQ",
            bid=685.0,
            ask=685.02,
            last=685.01,
            volume=60e6,
            timestamp=datetime(2026, 7, 27, 14, 0),
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
        recent_candles=candles or [],
    )


def _make_candles(
    n: int,
    base_price: float = 685.0,
    flush_idx: int | None = None,
    flush_depth: float = 5.0,
    recovery: bool = True,
) -> list:
    """Build a list of OHLCV candles with an optional flush and recovery.

    Args:
        n: Number of candles.
        base_price: Baseline price level.
        flush_idx: Index of the flush (lowest low) bar. None = no flush.
        flush_depth: How far the flush drops below base_price.
        recovery: If True, bars after the flush recover above flush high.
    """
    from evotrader.models.market import OHLCV

    candles = []
    for i in range(n):
        # Compute timestamp avoiding minute overflow
        total_minutes = i * 5
        hour = 10 + total_minutes // 60
        minute = total_minutes % 60
        ts = datetime(2026, 7, 27, hour, minute)
        if flush_idx is not None and i == flush_idx:
            # Flush bar: deep low, close near mid-range
            candles.append(
                OHLCV(
                    timestamp=ts,
                    open=base_price - 1.0,
                    high=base_price - 0.5,
                    low=base_price - flush_depth,
                    close=base_price - 2.0,
                    volume=100000.0,
                )
            )
        elif flush_idx is not None and i > flush_idx and recovery:
            # Recovery bars: higher lows, closing above flush high
            candles.append(
                OHLCV(
                    timestamp=ts,
                    open=base_price - 1.5,
                    high=base_price + 0.5,
                    low=base_price - flush_depth + 1.0 + (i - flush_idx) * 0.5,
                    close=base_price,
                    volume=80000.0,
                )
            )
        else:
            # Normal bars
            candles.append(
                OHLCV(
                    timestamp=ts,
                    open=base_price - 0.5,
                    high=base_price + 0.5,
                    low=base_price - 1.0,
                    close=base_price,
                    volume=80000.0,
                )
            )
    return candles


class TestSwingFailureReversalStrategy:
    def test_abstains_without_candles(self) -> None:
        """No candles -> applicable=False."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        strategy = SwingFailureReversalStrategy()
        signal = strategy.compute_signal(_make_sfr_snapshot(candles=[]))
        assert signal.value == 0.0
        assert signal.metadata["applicable"] is False
        assert signal.metadata["reason"] == "insufficient_data"

    def test_abstains_without_atr(self) -> None:
        """Missing ATR -> applicable=False."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        candles = _make_candles(20)
        strategy = SwingFailureReversalStrategy()
        signal = strategy.compute_signal(
            _make_sfr_snapshot(
                candles=candles,
                vwap=685.0,
                atr=None,
            )
        )
        assert signal.value == 0.0
        assert signal.metadata["applicable"] is False

    def test_abstains_on_stale_vwap_anchor(self) -> None:
        """Non-current-session VWAP anchor -> applicable=False."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        candles = _make_candles(20)
        strategy = SwingFailureReversalStrategy()
        signal = strategy.compute_signal(
            _make_sfr_snapshot(
                candles=candles,
                vwap=685.0,
                vwap_anchor="prior_session",
            )
        )
        assert signal.value == 0.0
        assert signal.metadata["applicable"] is False
        assert signal.metadata["reason"] == "stale_vwap_anchor"

    def test_abstains_at_session_low_no_higher_low(self) -> None:
        """Price at low with continued lower lows -> no signal."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        # Flush at bar 15, but last bar makes a new lower low (no recovery)
        candles = _make_candles(20, flush_idx=15, flush_depth=6.0, recovery=False)
        # Override last candle to make a new low
        from evotrader.models.market import OHLCV

        candles[-1] = OHLCV(
            timestamp=datetime(2026, 7, 27, 11, 35),
            open=680.0,
            high=681.0,
            low=678.0,
            close=679.0,
            volume=100000.0,
        )
        strategy = SwingFailureReversalStrategy()
        signal = strategy.compute_signal(
            _make_sfr_snapshot(
                candles=candles,
                vwap=685.0,
            )
        )
        assert signal.value == 0.0

    def test_fires_on_confirmed_reversal(self) -> None:
        """Higher low + reclaim -> positive signal."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        # Flush at bar 15 with recovery: higher lows, close above flush high
        candles = _make_candles(
            20,
            base_price=685.0,
            flush_idx=15,
            flush_depth=6.0,
            recovery=True,
        )
        strategy = SwingFailureReversalStrategy()
        signal = strategy.compute_signal(
            _make_sfr_snapshot(
                candles=candles,
                vwap=685.0,
            )
        )
        assert signal.value > 0.0  # Long signal
        assert signal.metadata["applicable"] is True
        assert signal.metadata["reason"] == "confirmed_reversal"
        assert signal.metadata["higher_low"] is True
        assert signal.metadata["reclaimed"] is True

    def test_signal_bounded(self) -> None:
        """Output must be in [0, 1.0] (long-only)."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        candles = _make_candles(
            20,
            base_price=685.0,
            flush_idx=15,
            flush_depth=6.0,
            recovery=True,
        )
        strategy = SwingFailureReversalStrategy()
        signal = strategy.compute_signal(
            _make_sfr_snapshot(
                candles=candles,
                vwap=685.0,
            )
        )
        assert 0.0 <= signal.value <= 1.0

    def test_parameter_roundtrip(self) -> None:
        """Parameters should survive get/set roundtrip."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        strategy = SwingFailureReversalStrategy(
            lookback_bars=25,
            min_stretch_atr=1.5,
        )
        params = strategy.get_parameters()
        assert params["lookback_bars"] == 25
        assert params["min_stretch_atr"] == 1.5

        new_strategy = SwingFailureReversalStrategy()
        new_strategy.set_parameters(params)
        assert new_strategy.get_parameters()["lookback_bars"] == 25

    def test_validate_rejects_invalid_params(self) -> None:
        """Validation should catch invalid parameter values."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        strategy = SwingFailureReversalStrategy()

        errors = strategy.validate_parameters({"lookback_bars": 1})
        assert any("lookback_bars" in e for e in errors)

        errors = strategy.validate_parameters({"confirm_decay": 1.5})
        assert any("confirm_decay" in e for e in errors)

        errors = strategy.validate_parameters({"min_confirm_bars": 5, "max_confirm_bars": 3})
        assert any("min_confirm_bars" in e for e in errors)

    def test_validate_accepts_valid_params(self) -> None:
        """Valid parameters should pass validation with no errors."""
        from evotrader.algorithms.strategies.swing_failure_reversal import (
            SwingFailureReversalStrategy,
        )

        strategy = SwingFailureReversalStrategy()
        errors = strategy.validate_parameters(strategy.get_parameters())
        assert errors == []


# ---------------------------------------------------------------------------
# Mean Reversion: VWAP Component Removal Verification
# ---------------------------------------------------------------------------


class TestMeanReversionNoVwap:
    def test_vwap_weight_not_in_params(self) -> None:
        """vwap_weight should no longer exist in parameters."""
        strategy = MeanReversionStrategy()
        params = strategy.get_parameters()
        assert "vwap_weight" not in params

    def test_internal_weights_sum_to_one(self) -> None:
        """rsi + bollinger + ibs weights should sum to 1.0."""
        strategy = MeanReversionStrategy()
        params = strategy.get_parameters()
        total = params["rsi_weight"] + params["bollinger_weight"] + params["ibs_weight"]
        assert abs(total - 1.0) < 0.01

    def test_validation_uses_three_weights(self) -> None:
        """Weight validation should check three components, not four."""
        strategy = MeanReversionStrategy()
        errors = strategy.validate_parameters(
            {
                "rsi_weight": 0.60,
                "bollinger_weight": 0.20,
                "ibs_weight": 0.20,
            }
        )
        assert errors == []

    def test_signal_still_works(self, bullish_snapshot: MarketSnapshot) -> None:
        """Signal should still compute correctly without VWAP."""
        strategy = MeanReversionStrategy()
        signal = strategy.compute_signal(bullish_snapshot)
        assert signal.name == "mean_reversion"
        assert -1.0 <= signal.value <= 1.0
        # vwap_signal should NOT be in metadata
        assert "vwap_signal" not in signal.metadata


# ---------------------------------------------------------------------------
# Composite: Normalized Conviction & Authoring Signal Tests
# ---------------------------------------------------------------------------


class TestCompositeNormalizedConviction:
    def test_normalized_conviction_emitted(
        self,
        bullish_snapshot: MarketSnapshot,
    ) -> None:
        """Composite metadata should contain normalized_conviction."""
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
                "gap": GapStrategy(),
            },
            weights={
                "momentum": 0.35,
                "mean_reversion": 0.40,
                "gap": 0.25,
            },
        )
        signal = composite.compute_signal(bullish_snapshot)
        assert "normalized_conviction" in signal.metadata

    def test_authoring_signal_emitted(
        self,
        bullish_snapshot: MarketSnapshot,
    ) -> None:
        """Composite metadata should contain authoring_signal when signals vote."""
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
                "gap": GapStrategy(),
            },
            weights={
                "momentum": 0.35,
                "mean_reversion": 0.40,
                "gap": 0.25,
            },
        )
        signal = composite.compute_signal(bullish_snapshot)
        # At least some sub-signals should be non-zero on a bullish snapshot
        if signal.metadata.get("live_signal_count", 0) > 0:
            assert "authoring_signal" in signal.metadata
            assert "authoring_contribution" in signal.metadata

    def test_normalized_conviction_bounded(
        self,
        bullish_snapshot: MarketSnapshot,
    ) -> None:
        """normalized_conviction should be in reasonable range."""
        composite = CompositeStrategy(
            sub_strategies={
                "momentum": MomentumStrategy(),
                "mean_reversion": MeanReversionStrategy(),
                "gap": GapStrategy(),
            },
            weights={
                "momentum": 0.35,
                "mean_reversion": 0.40,
                "gap": 0.25,
            },
        )
        signal = composite.compute_signal(bullish_snapshot)
        nc = signal.metadata["normalized_conviction"]
        assert -1.5 <= nc <= 1.5  # Allow slight float rounding


class TestSignalInversion:
    """Inverting a sub-signal must flip its contribution without touching weights.

    Weights stay positive because the renormalization path sums them and caps
    each survivor at MAX_AMPLIFICATION x its configured weight; a negative
    weight would corrupt both.
    """

    def _snapshot(self):
        from datetime import UTC, datetime

        from evotrader.models.market import (
            MarketRegime,
            MarketSnapshot,
            Quote,
            RegimeClassification,
            TechnicalIndicators,
        )

        ts = datetime.now(UTC)
        return MarketSnapshot(
            ticker="QQQ",
            timestamp=ts,
            quote=Quote(ticker="QQQ", bid=99.99, ask=100.01, last=100.0, volume=1e6, timestamp=ts),
            indicators=TechnicalIndicators(atr_14=2.0, rsi_14=55.0),
            regime=RegimeClassification(
                regime=MarketRegime.RANGE_BOUND, confidence=0.8, reasoning="test"
            ),
        )

    def _composite(self, inverted=None):
        from evotrader.algorithms.base import TradingAlgorithm
        from evotrader.algorithms.composite import CompositeStrategy
        from evotrader.models.signals import AlgoSignal

        class Fixed(TradingAlgorithm):
            def __init__(self, nm: str, val: float) -> None:
                self._nm, self._val = nm, val

            @property
            def name(self) -> str:
                return self._nm

            @property
            def version(self) -> str:
                return "v1"

            def compute_signal(self, snapshot) -> AlgoSignal:
                return AlgoSignal(name=self._nm, value=self._val, weight=1.0)

        return CompositeStrategy(
            sub_strategies={"alpha": Fixed("alpha", 0.6), "beta": Fixed("beta", 0.2)},
            weights={"alpha": 0.5, "beta": 0.5},
            regime_adaptive=False,
            inverted_strategies=inverted,
        )

    def test_uninverted_baseline(self) -> None:
        v = self._composite().compute_signal(self._snapshot()).value
        assert v == pytest.approx(0.4)  # (0.6 + 0.2) / 2

    def test_inverting_flips_that_signal_only(self) -> None:
        v = self._composite({"alpha"}).compute_signal(self._snapshot()).value
        assert v == pytest.approx(-0.2)  # (-0.6 + 0.2) / 2

    def test_raw_value_is_preserved_for_attribution(self) -> None:
        detailed = self._composite({"alpha"}).compute_detailed_signal(self._snapshot())
        alpha = next(s for s in detailed.signals if s.name == "alpha")
        assert alpha.value == pytest.approx(-0.6)
        assert alpha.metadata["inverted"] is True
        assert alpha.metadata["raw_value"] == pytest.approx(0.6)

    def test_weights_stay_positive(self) -> None:
        detailed = self._composite({"alpha"}).compute_detailed_signal(self._snapshot())
        assert all(s.weight >= 0 for s in detailed.signals)

    def test_unknown_inverted_name_is_ignored_not_fatal(self) -> None:
        v = self._composite({"nonexistent"}).compute_signal(self._snapshot()).value
        assert v == pytest.approx(0.4)

    def test_exposed_on_the_property(self) -> None:
        assert self._composite({"alpha"}).inverted_strategies == {"alpha"}
        assert self._composite().inverted_strategies == set()

    def test_a_config_that_declares_the_inversion_gets_it(
        self, starter_data_dir, active_algorithm_config
    ) -> None:
        """A version's config.yaml turns the inversion on, as v024 did."""
        from evotrader.algorithms.loader import StrategyLoader

        params = {
            **active_algorithm_config,
            "composite": {**active_algorithm_config["composite"], "inverted": ["momentum"]},
        }
        c = StrategyLoader(
            starter_data_dir / "algorithms" / "strategy_manifest.yaml"
        ).build_composite(params, active_version="v024_momentum_inversion")
        assert c.inverted_strategies == {"momentum"}
