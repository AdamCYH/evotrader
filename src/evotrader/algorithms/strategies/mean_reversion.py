"""Mean reversion strategy.

Profits from the tendency of prices to revert to their mean after
extreme moves. Combines:
- RSI extremes (oversold/overbought)
- Bollinger Band mean reversion
- IBS (Internal Bar Strength) for intraday fading

VWAP-deviation authority now lives solely in the quality-gated
intraday_vwap_zscore sub-strategy. See review
20260727_204248_circuit_breaker_streak_semantics_blocks_trading_days_later
finding #2 for rationale.

Best in: Range-Bound regime.
"""

from __future__ import annotations

from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.indicators.bollinger import bollinger_signal
from evotrader.indicators.ibs import ibs_signal
from evotrader.indicators.rsi import rsi_signal
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class MeanReversionStrategy(TradingAlgorithm):
    """Mean reversion strategy for range-bound markets.

    Generates buy signals when price is oversold relative to multiple
    indicators, and sell signals when overbought.
    """

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        bollinger_period: int = 20,
        bollinger_std: float = 2.0,
        ibs_oversold: float = 0.2,
        ibs_overbought: float = 0.8,
        rsi_weight: float = 0.60,
        bollinger_weight: float = 0.20,
        ibs_weight: float = 0.20,
        # vwap_weight removed: VWAP-deviation authority now lives solely in
        # the quality-gated intraday_vwap_zscore sub-strategy. Retaining an
        # ungated copy here double-counted the same deviation in the
        # composite, with the lower-quality voice getting a free vote.
        trend_fade_decay: float = 0.35,
        version: str = "v001",
    ) -> None:
        self._rsi_period = rsi_period
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought
        self._bollinger_period = bollinger_period
        self._bollinger_std = bollinger_std
        self._ibs_oversold = ibs_oversold
        self._ibs_overbought = ibs_overbought
        self._rsi_weight = rsi_weight
        self._bollinger_weight = bollinger_weight
        self._ibs_weight = ibs_weight
        self._trend_fade_decay = trend_fade_decay
        self._version = version

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Mean-reversion using RSI({self._rsi_period}, "
            f"{self._rsi_oversold}/{self._rsi_overbought}), "
            f"Bollinger({self._bollinger_period}, {self._bollinger_std}σ), "
            f"IBS({self._ibs_oversold}/{self._ibs_overbought})."
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute mean-reversion signal from RSI, Bollinger, IBS, and VWAP."""
        ind = snapshot.indicators
        # Track components by name so metadata is never mislabeled when
        # an earlier indicator is None and its component is skipped.
        components: list[tuple[str, float, float]] = []  # (name, signal, weight)

        # IBS and Bollinger %B are pure intraday mean-reversion oscillators
        # with no trend- OR range-persistence value on the cycle timeframe.
        # Dampen them in EVERY regime (not just trends) so a single stale
        # daily oscillator reading cannot pin the composite -- especially in
        # range_bound where mean_reversion carries the dominant weight.

        # RSI signal — not attenuated here. It is NOT neutral between the
        # bands: rsi_signal's neutral-zone ramp returns -(rsi - mid)/half * 0.4,
        # about -0.29 at RSI 66 with the overbought line at 72. That ramp is a
        # deliberate range_bound read; in a confirmed trend the TREND GUARD
        # below decays the whole counter-trend value instead, so it is not
        # decayed twice.
        if ind.rsi_14 is not None:
            rsi_sig = rsi_signal(
                ind.rsi_14,
                oversold=self._rsi_oversold,
                overbought=self._rsi_overbought,
            )
            components.append(("rsi_signal", rsi_sig, self._rsi_weight))

        # Bollinger signal — always attenuated. Bollinger %B fade is a pure
        # intraday mean-reversion oscillator with no persistence value in any
        # regime. A stale reading must not dominate the composite.
        if all(
            v is not None for v in (ind.bollinger_upper, ind.bollinger_middle, ind.bollinger_lower)
        ):
            assert ind.bollinger_upper is not None
            assert ind.bollinger_middle is not None
            assert ind.bollinger_lower is not None
            bb_sig = bollinger_signal(
                close=snapshot.quote.last,
                upper=ind.bollinger_upper,
                middle=ind.bollinger_middle,
                lower=ind.bollinger_lower,
            )
            if bb_sig != 0.0:
                bb_sig *= self._trend_fade_decay
            components.append(("bollinger_signal", bb_sig, self._bollinger_weight))

        # IBS signal — always attenuated. IBS is a next-day predictor
        # re-read hourly on in-progress daily bars. A stale reading must
        # not dominate the composite in any regime.
        if ind.ibs is not None:
            ibs_sig = ibs_signal(
                ind.ibs,
                oversold=self._ibs_oversold,
                overbought=self._ibs_overbought,
            )
            if ibs_sig != 0.0:
                ibs_sig *= self._trend_fade_decay
            components.append(("ibs_signal", ibs_sig, self._ibs_weight))

        # VWAP deviation is deliberately NOT measured here — see
        # intraday_vwap_zscore, which applies z-score gating, staleness
        # decay and an rvol guard to the same underlying deviation.

        # Weighted combination
        if not components:
            return AlgoSignal(name=self.name, value=0.0, weight=1.0)

        total_weight = sum(w for _, _, w in components)
        weighted_sum = sum(s * w for _, s, w in components)
        signal_value = weighted_sum / total_weight if total_weight > 0 else 0.0
        signal_value = max(-1.0, min(1.0, signal_value))

        # ── TREND GUARD ────────────────────────────────────────────
        # In a confirmed trend, decay the COUNTER-trend leg only: a BUY in a
        # downtrend (price < ema_9 < ema_21, sma_20 < sma_50) or a SELL in an
        # uptrend (the mirror). The bands re-arm at every new extreme, so
        # without this the channel dissents on every cycle of a trend: RSI
        # 33-37 read BUY through a 5.44% decline (review
        # 20260729_bearish_inexpressibility finding #7), and RSI 64-71 read
        # -0.15..-0.25 on every cycle of MSTR 2026-09-22..24.
        #
        # Until 2026-09-24 the whole block sat under `signal_value > 0`, so
        # the uptrend branch could never run and the composite leaned bearish
        # by design in BOTH trends (review 20260924_224217 finding 1).
        trend_guard_applied = False
        trend_stack = "unavailable"
        if (
            ind.ema_9 is not None
            and ind.ema_21 is not None
            and ind.sma_20 is not None
            and ind.sma_50 is not None
        ):
            price = snapshot.quote.last
            if price < ind.ema_9 < ind.ema_21 and ind.sma_20 < ind.sma_50:
                trend_stack = "bear"
            elif price > ind.ema_9 > ind.ema_21 and ind.sma_20 > ind.sma_50:
                trend_stack = "bull"
            else:
                trend_stack = "none"
            if (trend_stack == "bear" and signal_value > 0) or (
                trend_stack == "bull" and signal_value < 0
            ):
                signal_value *= self._trend_fade_decay
                trend_guard_applied = True

        # Build metadata by component name — immune to ordering shifts.
        metadata: dict[str, float | int | str] = {name: sig for name, sig, _ in components}
        metadata["component_count"] = len(components)
        # Audit trail: record the anchor visible AT COMPUTE TIME so
        # divergence from the serialized snapshot is detectable.
        metadata["vwap_anchor_at_compute"] = ind.vwap_anchor or "unknown"
        # Always present, so a cycle where the guard did not fire is countable
        # rather than indistinguishable from a build that lacks the guard.
        metadata["trend_guard_applied"] = trend_guard_applied
        metadata["trend_stack"] = trend_stack

        return AlgoSignal(
            name=self.name,
            value=signal_value,
            weight=1.0,
            metadata=metadata,
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "rsi_period": self._rsi_period,
            "rsi_oversold": self._rsi_oversold,
            "rsi_overbought": self._rsi_overbought,
            "bollinger_period": self._bollinger_period,
            "bollinger_std": self._bollinger_std,
            "ibs_oversold": self._ibs_oversold,
            "ibs_overbought": self._ibs_overbought,
            "rsi_weight": self._rsi_weight,
            "bollinger_weight": self._bollinger_weight,
            "ibs_weight": self._ibs_weight,
            "trend_fade_decay": self._trend_fade_decay,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key in (
            "rsi_period",
            "rsi_oversold",
            "rsi_overbought",
            "bollinger_period",
            "bollinger_std",
            "ibs_oversold",
            "ibs_overbought",
            "rsi_weight",
            "bollinger_weight",
            "ibs_weight",
            "trend_fade_decay",
        ):
            if key in params:
                setattr(self, f"_{key}", params[key])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "rsi_oversold" in params and "rsi_overbought" in params:
            if params["rsi_oversold"] >= params["rsi_overbought"]:
                errors.append("rsi_oversold must be less than rsi_overbought")
        if "ibs_oversold" in params and "ibs_overbought" in params:
            if params["ibs_oversold"] >= params["ibs_overbought"]:
                errors.append("ibs_oversold must be less than ibs_overbought")
        if "trend_fade_decay" in params:
            decay = float(params["trend_fade_decay"])
            if not (0.0 <= decay <= 1.0):
                errors.append(f"trend_fade_decay must be between 0.0 and 1.0, got {decay}")
        # Validate component weights sum to ~1.0 if all four are provided
        weight_keys = ("rsi_weight", "bollinger_weight", "ibs_weight")
        if all(k in params for k in weight_keys):
            total = sum(float(params[k]) for k in weight_keys)
            if abs(total - 1.0) > 0.01:
                errors.append(f"Component weights must sum to 1.0, got {total:.4f}")
        return errors
