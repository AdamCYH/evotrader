"""Swing Failure Reversal strategy.

Fires long ONLY after a downside stretch has demonstrably STOPPED extending —
requiring a swing-failure structure (a higher low following a flush, with
reclaim of a reference level) instead of firing continuously while price
makes new lower lows.

Directly targets the discriminator that separated the system's 2 large
winners from its 7 consecutive losers: winners had price STABILIZE and
reclaim; losers all made continued lower lows while the daily oscillators
kept re-arming.

Complementary to intraday_vwap_zscore:
- intraday_vwap_zscore measures "how stretched are we NOW" (fires AT the extreme)
- swing_failure_reversal measures "did the stretch STOP" (fires AFTER the turn)

They are deliberately correlated in DIRECTION but decorrelated in TIMING.

Best in: Range-Bound regime.
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class SwingFailureReversalStrategy(TradingAlgorithm):
    """Swing failure reversal with structural confirmation.

    Long-only by construction: the account is structurally long-only
    (no affordable puts on a small account), so a symmetric bearish
    leg would be unexpressable and is deliberately omitted.

    Key features:
    - Locates the flush (lowest low in lookback window)
    - Requires minimum stretch depth in ATR units
    - Requires structural confirmation: higher low + reclaim + no new low
    - Freshness decay so stale reversals don't re-fire all session
    - Reclaim quality scales signal by how cleanly price reclaimed
    """

    def __init__(
        self,
        lookback_bars: int = 20,
        min_confirm_bars: int = 2,
        max_confirm_bars: int = 8,
        min_stretch_atr: float = 1.2,
        stretch_scale: float = 0.8,
        confirm_decay: float = 0.75,
        reclaim_scale: float = 0.5,
        base_strength: float = 0.8,
        require_current_session_anchor: bool = True,
        version: str = "v001",
    ) -> None:
        self._lookback_bars = lookback_bars
        self._min_confirm_bars = min_confirm_bars
        self._max_confirm_bars = max_confirm_bars
        self._min_stretch_atr = min_stretch_atr
        self._stretch_scale = stretch_scale
        self._confirm_decay = confirm_decay
        self._reclaim_scale = reclaim_scale
        self._base_strength = base_strength
        self._require_current_session_anchor = require_current_session_anchor
        self._version = version

    @property
    def name(self) -> str:
        return "swing_failure_reversal"

    @property
    def version(self) -> str:
        return self._version

    @property
    def resolution(self) -> str:
        """Intraday: reads 5-minute bars and session VWAP every cycle.

        Inherited 'daily' until 2026-09-22, which told the strategy agent this
        channel could not change between hourly cycles when it reads nothing
        but intraday bars.
        """
        return "intraday"

    @property
    def description(self) -> str:
        return (
            f"Swing failure reversal "
            f"(lookback={self._lookback_bars}, "
            f"min_stretch_atr={self._min_stretch_atr}, "
            f"confirm_decay={self._confirm_decay})"
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute swing failure reversal signal from intraday candles.

        Signal logic:
        1. ABSTAIN GUARDS — insufficient data or stale VWAP anchor
        2. LOCATE THE FLUSH — lowest low in lookback window
        3. REQUIRE STRETCH — VWAP-to-flush distance in ATR units
        4. REQUIRE STRUCTURAL CONFIRMATION — higher low + reclaim + no new low
        5. COMPUTE STRENGTH — tanh * freshness * reclaim_quality * base_strength
        """
        candles = snapshot.recent_candles
        vwap = snapshot.indicators.vwap
        atr = snapshot.indicators.atr_14

        # ── 1. ABSTAIN GUARDS ──
        if not candles or len(candles) < self._lookback_bars or atr is None or atr <= 0:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "reason": "insufficient_data",
                },
            )

        if vwap is None:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "reason": "no_vwap",
                },
            )

        # Same anchor discipline as intraday_vwap_zscore: abstain unless
        # the VWAP is verifiably anchored to the current session.
        if self._require_current_session_anchor:
            anchor = snapshot.indicators.vwap_anchor or "unknown"
            if anchor != "current_session":
                return AlgoSignal(
                    name=self.name,
                    value=0.0,
                    weight=1.0,
                    metadata={
                        "applicable": False,
                        "reason": "stale_vwap_anchor",
                        "vwap_anchor": anchor,
                    },
                )

        # ── 2. LOCATE THE FLUSH ──
        window = candles[-self._lookback_bars :]
        flush_idx = 0
        flush_low = window[0].low
        for i, bar in enumerate(window):
            if bar.low < flush_low:
                flush_low = bar.low
                flush_idx = i

        bars_since_flush = len(window) - 1 - flush_idx

        # Need enough bars after flush for confirmation but not too many
        # (staleness guard — stops the signal re-firing all session on
        # one old low).
        if bars_since_flush < self._min_confirm_bars:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": True,
                    "reason": "too_few_confirm_bars",
                    "flush_low": round(flush_low, 4),
                    "bars_since_flush": bars_since_flush,
                },
            )

        if bars_since_flush > self._max_confirm_bars:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": True,
                    "reason": "stale_flush",
                    "flush_low": round(flush_low, 4),
                    "bars_since_flush": bars_since_flush,
                },
            )

        # ── 3. REQUIRE THE STRETCH TO HAVE BEEN REAL ──
        stretch_atr = (vwap - flush_low) / atr
        if stretch_atr < self._min_stretch_atr:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": True,
                    "reason": "stretch_too_shallow",
                    "stretch_atr": round(stretch_atr, 4),
                    "min_stretch_atr": self._min_stretch_atr,
                    "flush_low": round(flush_low, 4),
                    "bars_since_flush": bars_since_flush,
                },
            )

        # ── 4. REQUIRE STRUCTURAL CONFIRMATION ──
        post_flush_bars = window[flush_idx + 1 :]  # Bars after the flush

        # a) HIGHER LOW: min(low of bars after flush) > flush_low
        post_flush_lows = [b.low for b in post_flush_bars]
        higher_low = all(low > flush_low for low in post_flush_lows)

        # b) RECLAIM: last close > high of the flush bar
        flush_high = window[flush_idx].high
        last_close = window[-1].close
        reclaimed = last_close > flush_high

        # c) NO NEW LOW ON LAST BAR: last low > flush_low
        last_low = window[-1].low
        no_new_low = last_low > flush_low

        if not (higher_low and reclaimed and no_new_low):
            reasons = []
            if not higher_low:
                reasons.append("no_higher_low")
            if not reclaimed:
                reasons.append("not_reclaimed")
            if not no_new_low:
                reasons.append("new_low_on_last_bar")
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": True,
                    "reason": "|".join(reasons),
                    "flush_low": round(flush_low, 4),
                    "flush_high": round(flush_high, 4),
                    "bars_since_flush": bars_since_flush,
                    "stretch_atr": round(stretch_atr, 4),
                    "higher_low": higher_low,
                    "reclaimed": reclaimed,
                    "no_new_low": no_new_low,
                },
            )

        # ── 5. COMPUTE STRENGTH ──
        # Deeper flush that held = stronger
        base = math.tanh((stretch_atr - self._min_stretch_atr) / self._stretch_scale)

        # Freshness: decays as bars accumulate beyond min_confirm_bars
        freshness = self._confirm_decay ** max(0, bars_since_flush - self._min_confirm_bars)

        # Reclaim quality: how cleanly price reclaimed above flush high
        reclaim_quality = max(
            0.0,
            min(
                1.0,
                (last_close - flush_high) / atr / self._reclaim_scale,
            ),
        )

        # Final value: long-only (clamped to [0, 1])
        value = max(
            0.0,
            min(
                1.0,
                base * freshness * (0.5 + 0.5 * reclaim_quality) * self._base_strength,
            ),
        )

        return AlgoSignal(
            name=self.name,
            value=value,
            weight=1.0,
            metadata={
                "applicable": True,
                "reason": "confirmed_reversal",
                "flush_low": round(flush_low, 4),
                "flush_high": round(flush_high, 4),
                "bars_since_flush": bars_since_flush,
                "stretch_atr": round(stretch_atr, 4),
                "higher_low": higher_low,
                "reclaimed": reclaimed,
                "reclaim_quality": round(reclaim_quality, 4),
                "freshness": round(freshness, 4),
                "base": round(base, 4),
                "value": round(value, 4),
            },
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "lookback_bars": self._lookback_bars,
            "min_confirm_bars": self._min_confirm_bars,
            "max_confirm_bars": self._max_confirm_bars,
            "min_stretch_atr": self._min_stretch_atr,
            "stretch_scale": self._stretch_scale,
            "confirm_decay": self._confirm_decay,
            "reclaim_scale": self._reclaim_scale,
            "base_strength": self._base_strength,
            "require_current_session_anchor": self._require_current_session_anchor,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key in (
            "lookback_bars",
            "min_confirm_bars",
            "max_confirm_bars",
            "min_stretch_atr",
            "stretch_scale",
            "confirm_decay",
            "reclaim_scale",
            "base_strength",
            "require_current_session_anchor",
        ):
            if key in params:
                setattr(self, f"_{key}", params[key])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "lookback_bars" in params:
            lb = int(params["lookback_bars"])
            if lb < 3:
                errors.append(f"lookback_bars must be >= 3, got {lb}")
        if "min_confirm_bars" in params:
            mc = int(params["min_confirm_bars"])
            if mc < 1:
                errors.append(f"min_confirm_bars must be >= 1, got {mc}")
        if "max_confirm_bars" in params:
            mx = int(params["max_confirm_bars"])
            if mx < 1:
                errors.append(f"max_confirm_bars must be >= 1, got {mx}")
        if "min_confirm_bars" in params and "max_confirm_bars" in params:
            mc = int(params["min_confirm_bars"])
            mx = int(params["max_confirm_bars"])
            if mc >= mx:
                errors.append(f"min_confirm_bars ({mc}) must be < max_confirm_bars ({mx})")
        if "min_stretch_atr" in params:
            ms = float(params["min_stretch_atr"])
            if ms <= 0:
                errors.append(f"min_stretch_atr must be positive, got {ms}")
        if "stretch_scale" in params:
            ss = float(params["stretch_scale"])
            if ss <= 0:
                errors.append(f"stretch_scale must be positive, got {ss}")
        if "confirm_decay" in params:
            cd = float(params["confirm_decay"])
            if not (0.0 < cd < 1.0):
                errors.append(f"confirm_decay must be in (0, 1), got {cd}")
        if "reclaim_scale" in params:
            rs = float(params["reclaim_scale"])
            if rs <= 0:
                errors.append(f"reclaim_scale must be positive, got {rs}")
        if "base_strength" in params:
            bs = float(params["base_strength"])
            if not (0.0 < bs <= 1.0):
                errors.append(f"base_strength must be in (0, 1], got {bs}")
        return errors
