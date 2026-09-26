"""Gap / opening range strategy.

Profits from overnight gaps and opening range breakouts/breakdowns:
- Gap fade: When SPY gaps up/down significantly, bet on a gap fill
  (reversion) during the session.
- Opening range breakout: When price breaks the first 15-30 min range,
  follow the momentum.

This strategy works in all regimes with different parameter sets.
"""

from __future__ import annotations

from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal
from evotrader.tools.market_hours import minutes_since_open


class GapStrategy(TradingAlgorithm):
    """Gap analysis and opening range strategy.

    Analyses the gap between the previous close and today's open, plus
    where the current price sits relative to the opening range.
    """

    def __init__(
        self,
        min_gap_pct: float = 0.3,
        gap_fade_threshold: float = 1.0,
        decay_minutes: float = 120.0,
        version: str = "v001",
        **kwargs: Any,
    ) -> None:
        self._min_gap_pct = min_gap_pct
        self._gap_fade_threshold = gap_fade_threshold
        self._decay_minutes = decay_minutes
        self._version = version

    @property
    def name(self) -> str:
        return "gap"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Gap analysis with fade threshold {self._gap_fade_threshold:.1f}% "
            f"and {self._decay_minutes:.0f}-minute decay window."
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute gap-based trading signal.

        Signal logic:
        - Large gap up (> threshold)  → fade (sell signal)
        - Large gap down (< -threshold) → fade (buy signal)
        - Small gap → neutral, defer to other strategies (marked as
          not applicable so the composite can renormalize weights)
        - Adjusts for whether gap has started filling
        - Decays linearly over the session (gap-fill edge is concentrated
          in the first 1-2 hours after market open)
        """
        gap_pct = snapshot.gap_pct

        if gap_pct is None or abs(gap_pct) < self._min_gap_pct:
            # No significant gap — mark as not applicable so composite
            # renormalizes weights over the remaining strategies.
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "gap_pct": gap_pct or 0.0,
                    "gap_type": "none",
                    "applicable": False,
                },
            )

        # Daily change tells us if the gap is filling
        daily_change = snapshot.daily_change_pct or 0.0

        if abs(gap_pct) >= self._gap_fade_threshold:
            # Large gap → fade it (mean reversion play)
            # If gap is up (+), signal is to sell (-); if gap is down (-), buy (+)
            fade_signal = -gap_pct / self._gap_fade_threshold

            # Scale by the unfilled fraction of the gap (0 when fully filled).
            # fill_progress measures how much of the gap has been retraced:
            #   gap-up  (+0.015, daily +0.005): (0.015-0.005)/0.015 = 0.667 filled
            #   gap-down (-0.015, daily -0.005): (-0.015-(-0.005))/-0.015 = 0.667 filled
            fill_progress = (gap_pct - daily_change) / gap_pct if gap_pct != 0 else 0.0
            remaining = max(0.0, min(1.0, 1.0 - fill_progress))
            fade_signal *= remaining

            signal_value = max(-1.0, min(1.0, fade_signal))
            gap_type = "fade"
        else:
            # Moderate gap → weak directional signal
            signal_value = -gap_pct / self._gap_fade_threshold * 0.3
            signal_value = max(-1.0, min(1.0, signal_value))
            gap_type = "moderate"

        # Decay edge over session time: gap-fill alpha concentrates in
        # the first ~2h after open.  Linear ramp from 1.0→0.0 over
        # `decay_minutes` (default 120).
        elapsed = minutes_since_open(snapshot.timestamp)
        decay_factor = 1.0
        if elapsed is not None and self._decay_minutes > 0:
            decay_factor = max(0.0, 1.0 - elapsed / self._decay_minutes)
            signal_value *= decay_factor

        return AlgoSignal(
            name=self.name,
            value=signal_value,
            weight=1.0,
            metadata={
                "gap_pct": gap_pct,
                "daily_change_pct": daily_change,
                "gap_type": gap_type,
                "fill_remaining": remaining if gap_type == "fade" else None,
                "decay_factor": decay_factor,
                "applicable": True,
            },
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "min_gap_pct": self._min_gap_pct,
            "gap_fade_threshold": self._gap_fade_threshold,
            "decay_minutes": self._decay_minutes,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        if "min_gap_pct" in params:
            self._min_gap_pct = float(params["min_gap_pct"])
        if "gap_fade_threshold" in params:
            self._gap_fade_threshold = float(params["gap_fade_threshold"])
        if "decay_minutes" in params:
            self._decay_minutes = float(params["decay_minutes"])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "min_gap_pct" in params:
            v = float(params["min_gap_pct"])
            if v < 0 or v > 10.0:
                errors.append(
                    f"min_gap_pct must be in [0, 10.0] percent (e.g. 0.3 = 0.3%), got {v}"
                )
        if "gap_fade_threshold" in params:
            v = float(params["gap_fade_threshold"])
            if v <= 0 or v > 10.0:
                errors.append(
                    f"gap_fade_threshold must be in (0, 10.0] percent (e.g. 1.0 = 1.0%), got {v}"
                )
        if "decay_minutes" in params and params["decay_minutes"] <= 0:
            errors.append("decay_minutes must be positive")
        return errors
