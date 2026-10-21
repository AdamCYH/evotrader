"""Swing Failure Fade strategy: the bearish mirror of swing_failure_reversal.

Fires short ONLY after an upside stretch has demonstrably STOPPED extending: a
flush HIGH in the lookback window, a lower high on every bar since, and a close
back below the flush bar's low. swing_failure_reversal is long-only and names
this mirror as its omission; without it no channel can say that a session high
has failed, so a sell-leg firing of intraday_vwap_zscore at the high has
nothing to wait for.

Complementary to intraday_vwap_zscore's sell leg exactly as the long channel is
to its buy leg: intraday_vwap_zscore fires AT the extreme, this fires AFTER the
turn.

Term by term the long channel's rule, reflected: every price is read upside
down (a high for a low, above for below), so the two legs share one gate and
one shape, and their firing records can be compared.
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm, warming_up
from evotrader.algorithms.units import sma_cross, stack_context, validate_stack_mode
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class SwingFailureFadeStrategy(TradingAlgorithm):
    """Swing failure at the high with structural confirmation. Short-only.

    The mirror of swing_failure_reversal:

    - the flush is the highest high in the lookback window;
    - the stretch is how far that high sat above session VWAP, in daily ATRs;
    - confirmation is a lower high on every bar after the flush, a last close
      below the flush bar's low, and no new high on the last bar;
    - strength is tanh of the stretch past its anchor, times freshness (counted
      from the first close below the flush bar's low), times break quality.

    The value is in [-1, 0]. ``stack_mode`` only chooses which daily MA stack
    the metadata reports (``stack_context``), so a firing record can be split
    by it; it does not change the value.
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
        stretch_anchor_atr: float | None = None,
        stack_mode: str = "slow",
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
        self._stretch_anchor_atr = stretch_anchor_atr
        self._stack_mode = stack_mode
        self._version = version

    @property
    def name(self) -> str:
        return "swing_failure_fade"

    @property
    def version(self) -> str:
        return self._version

    @property
    def resolution(self) -> str:
        """Intraday: reads 5-minute bars and session VWAP every cycle."""
        return "intraday"

    @property
    def description(self) -> str:
        return (
            f"Swing failure fade, the bearish mirror of swing_failure_reversal "
            f"(lookback={self._lookback_bars}, "
            f"min_stretch_atr={self._min_stretch_atr}, "
            f"confirm_decay={self._confirm_decay})"
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute the swing failure fade signal from intraday candles.

        1. ABSTAIN GUARDS: insufficient data or a stale VWAP anchor
        2. LOCATE THE FLUSH: the highest high in the lookback window
        3. REQUIRE STRETCH: flush-to-VWAP distance in ATR units
        4. REQUIRE STRUCTURAL CONFIRMATION: lower high + break + no new high
        5. COMPUTE STRENGTH: tanh x freshness x break quality x base_strength
        """
        candles = snapshot.recent_candles
        ind = snapshot.indicators
        vwap = ind.vwap
        atr = ind.atr_14

        # ── 1. ABSTAIN GUARDS (the long channel's) ──
        if not candles or len(candles) < self._lookback_bars or atr is None or atr <= 0:
            meta: dict[str, Any] = {"applicable": False, "reason": "insufficient_data"}
            # Early in a session the lookback has not filled yet; the channel
            # is on duty and counted in the participation denominator.
            if (
                atr is not None
                and atr > 0
                and ind.vwap_anchor == "current_session"
                and warming_up(candles or [], self._lookback_bars)
            ):
                meta.update(warming_up=True, bars=len(candles), bars_needed=self._lookback_bars)
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        if vwap is None:
            return self._quiet({"applicable": False, "reason": "no_vwap"})

        if self._require_current_session_anchor:
            anchor_name = ind.vwap_anchor or "unknown"
            if anchor_name != "current_session":
                return self._quiet(
                    {"applicable": False, "reason": "stale_vwap_anchor", "vwap_anchor": anchor_name}
                )

        # ── 2. LOCATE THE FLUSH (the first bar with the window's highest high) ──
        window = candles[-self._lookback_bars :]
        flush_idx = 0
        flush_high = window[0].high
        for i, bar in enumerate(window):
            if bar.high > flush_high:
                flush_high = bar.high
                flush_idx = i

        bars_since_flush = len(window) - 1 - flush_idx
        price = snapshot.quote.last if snapshot.quote else None
        context = {
            "leg": "fade",
            "stack_context": stack_context(price, ind, self._stack_mode),
            "stack_mode": self._stack_mode,
            "sma_cross": sma_cross(ind),
            "ibs": ind.ibs,
        }

        if bars_since_flush < self._min_confirm_bars:
            return self._quiet(
                {
                    "applicable": True,
                    "reason": "too_few_confirm_bars",
                    "flush_high": round(flush_high, 4),
                    "bars_since_flush": bars_since_flush,
                    **context,
                }
            )

        if bars_since_flush > self._max_confirm_bars:
            return self._quiet(
                {
                    "applicable": True,
                    "reason": "stale_flush",
                    "flush_high": round(flush_high, 4),
                    "bars_since_flush": bars_since_flush,
                    **context,
                }
            )

        # ── 3. REQUIRE THE STRETCH TO HAVE BEEN REAL (above session VWAP) ──
        stretch_atr = (flush_high - vwap) / atr
        if stretch_atr < self._min_stretch_atr:
            return self._quiet(
                {
                    "applicable": True,
                    "reason": "stretch_too_shallow",
                    "stretch_atr": round(stretch_atr, 4),
                    "min_stretch_atr": self._min_stretch_atr,
                    "flush_high": round(flush_high, 4),
                    "bars_since_flush": bars_since_flush,
                    **context,
                }
            )

        # ── 4. REQUIRE STRUCTURAL CONFIRMATION ──
        post_flush_bars = window[flush_idx + 1 :]

        # a) LOWER HIGH: every bar after the flush stayed under its high
        lower_high = all(b.high < flush_high for b in post_flush_bars)

        # b) BREAK: the last close is below the flush bar's low
        flush_low = window[flush_idx].low
        last_close = window[-1].close
        broke_down = last_close < flush_low

        # c) NO NEW HIGH ON THE LAST BAR
        no_new_high = window[-1].high < flush_high

        if not (lower_high and broke_down and no_new_high):
            reasons = []
            if not lower_high:
                reasons.append("no_lower_high")
            if not broke_down:
                reasons.append("not_broken_down")
            if not no_new_high:
                reasons.append("new_high_on_last_bar")
            return self._quiet(
                {
                    "applicable": True,
                    "reason": "|".join(reasons),
                    "flush_high": round(flush_high, 4),
                    "flush_low": round(flush_low, 4),
                    "bars_since_flush": bars_since_flush,
                    "stretch_atr": round(stretch_atr, 4),
                    "lower_high": lower_high,
                    "broke_down": broke_down,
                    "no_new_high": no_new_high,
                    **context,
                }
            )

        # ── 5. COMPUTE STRENGTH (the long channel's, reflected) ──
        # Measured from the anchor: by default the gate itself, so a fade that
        # just clears the gate says almost nothing; stretch_anchor_atr measures
        # it from lower down. The gate decides WHETHER it fires, the anchor
        # only how loud.
        anchor = (
            self._min_stretch_atr if self._stretch_anchor_atr is None else self._stretch_anchor_atr
        )
        base = math.tanh(max(0.0, stretch_atr - anchor) / self._stretch_scale)

        # Freshness: how long the fade has stood CONFIRMED, from the first
        # close below the flush bar's low and never from before
        # min_confirm_bars.
        break_offset = next(
            (i + 1 for i, bar in enumerate(post_flush_bars) if bar.close < flush_low),
            bars_since_flush,  # unreachable: the last bar broke down, or we returned above
        )
        bars_since_confirmed = bars_since_flush - max(break_offset, self._min_confirm_bars)
        freshness = self._confirm_decay ** max(0, bars_since_confirmed)

        # Break quality: how cleanly price broke below the flush bar's low
        break_quality = max(0.0, min(1.0, (flush_low - last_close) / atr / self._reclaim_scale))

        # Short-only: clamped to [-1, 0]
        value = -max(
            0.0,
            min(1.0, base * freshness * (0.5 + 0.5 * break_quality) * self._base_strength),
        )

        return AlgoSignal(
            name=self.name,
            value=value,
            weight=1.0,
            metadata={
                "applicable": True,
                "reason": "confirmed_fade",
                "flush_high": round(flush_high, 4),
                "flush_low": round(flush_low, 4),
                "bars_since_flush": bars_since_flush,
                "bars_since_break": bars_since_flush - break_offset,
                "bars_since_confirmed": max(0, bars_since_confirmed),
                "stretch_atr": round(stretch_atr, 4),
                "lower_high": lower_high,
                "broke_down": broke_down,
                "break_quality": round(break_quality, 4),
                "freshness": round(freshness, 4),
                "stretch_anchor_atr": round(anchor, 4),
                "base": round(base, 4),
                "value": round(value, 4),
                **context,
            },
        )

    def _quiet(self, metadata: dict[str, Any]) -> AlgoSignal:
        return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=metadata)

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
            "stretch_anchor_atr": self._stretch_anchor_atr,
            "stack_mode": self._stack_mode,
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
            "stretch_anchor_atr",
            "stack_mode",
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
        if params.get("stretch_anchor_atr") is not None:
            anchor = float(params["stretch_anchor_atr"])
            gate = float(params.get("min_stretch_atr", self._min_stretch_atr))
            if not (0.0 <= anchor <= gate):
                errors.append(
                    f"stretch_anchor_atr must be in [0, min_stretch_atr ({gate})], got {anchor}"
                )
        if "stack_mode" in params:
            errors.extend(validate_stack_mode(params["stack_mode"]))
        return errors
