"""Options positioning (contrarian/confirmation) strategy.

Contrarian/confirmation signal from QQQ options put/call positioning.
Elevated P/C ratios are interpreted differently depending on event context:

- **Event pending**: Elevated P/C = hedging noise → abstain/neutral
- **No event, extreme P/C**: Contrarian lean (crowd max-hedged → long)
- **Post-event P/C collapse**: Amplified long lean (IV-crush re-entry)
- **Mild readings**: Confirmation mode, lean WITH moderate positioning

This is the first positioning/flow-based input — all other sub-strategies
are price-derived.  Gracefully abstains when options data is not yet
available in ``MarketSnapshot.options_context``.

Empirical basis:
- P/C vol ratio 3.12, OI ratio 3.48 observed 7/21 with 14x vol/OI
  anomalies at specific strikes
- The system's two best trades (#16 +20.7%, #18 +14.9%) were post-event
  IV-crush re-entries — this strategy encodes their trigger quantitatively
"""

from __future__ import annotations

from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class OptionsPositioningStrategy(TradingAlgorithm):
    """Contrarian/confirmation signal from options put/call positioning.

    Modes of operation:
    1. **No options data** → abstain (``applicable=False``).
       This is the default while the data pipeline is not yet connected.
    2. **Event pending** → elevated P/C is hedging noise, not direction.
       Neutral-to-mild contrarian lean only on washout readings.
    3. **Extreme P/C, no event** → contrarian: crowd max-hedged → long
       lean; crowd complacent → short lean.
    4. **Post-event P/C collapse** → amplified long lean encoding the
       IV-crush re-entry pattern.
    5. **Mild readings** → confirmation mode, lean WITH positioning.
    """

    def __init__(
        self,
        pcr_baseline: float = 1.0,
        pcr_std: float = 0.5,
        extreme_z: float = 2.0,
        contrarian_scale: float = 0.3,
        confirm_scale: float = 0.15,
        event_window_hours: float = 36.0,
        post_event_hours: float = 48.0,
        unwind_threshold: float = 0.5,
        post_event_kicker: float = 0.3,
        version: str = "v001",
    ) -> None:
        self._pcr_baseline = pcr_baseline
        self._pcr_std = pcr_std
        self._extreme_z = extreme_z
        self._contrarian_scale = contrarian_scale
        self._confirm_scale = confirm_scale
        self._event_window_hours = event_window_hours
        self._post_event_hours = post_event_hours
        self._unwind_threshold = unwind_threshold
        self._post_event_kicker = post_event_kicker
        self._version = version

    @property
    def name(self) -> str:
        return "options_positioning"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Options P/C contrarian/confirmation "
            f"(baseline={self._pcr_baseline}, "
            f"extreme_z={self._extreme_z}, "
            f"event_window={self._event_window_hours}h)"
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute options positioning signal.

        Returns neutral with ``applicable=False`` when options context
        data is not available, allowing the composite to renormalize
        weights over the remaining strategies with zero dead weight.
        """
        opt = snapshot.options_context
        if opt is None or opt.pc_volume_ratio is None:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "reason": "no_options_data",
                },
            )

        pcr = opt.pc_volume_ratio
        # Normalise vs baseline: pcr_z = (pcr - baseline_mean) / baseline_std
        pcr_z = (pcr - self._pcr_baseline) / max(self._pcr_std, 0.25)

        event_pending = (
            opt.event_hours_away is not None and opt.event_hours_away <= self._event_window_hours
        )

        if event_pending:
            # Elevated P/C pre-event = hedging noise, not direction.
            # Abstain-to-neutral on elevated readings; mild contrarian
            # lean only on washout (very low P/C before an event).
            if pcr_z > 0:
                value = 0.0
            else:
                value = min(0.3, -pcr_z * self._contrarian_scale)
        elif pcr_z >= self._extreme_z:
            # Extreme put skew with no event = crowd max-hedged
            # → contrarian LONG lean (classic P/C contrarian edge).
            value = min(
                0.6,
                (pcr_z - self._extreme_z + 1.0) * self._contrarian_scale,
            )
        elif pcr_z <= -self._extreme_z:
            # Extreme call skew, no event = complacency
            # → contrarian SHORT lean.
            value = max(
                -0.6,
                (pcr_z + self._extreme_z - 1.0) * self._contrarian_scale,
            )
        else:
            # Mild readings: confirmation mode — lean WITH moderate
            # positioning (negative pcr_z = calls dominate = bullish
            # confirmation, positive pcr_z = puts dominate = bearish
            # confirmation).  Scale is intentionally gentle.
            value = max(-0.25, min(0.25, -pcr_z * self._confirm_scale))

        # Post-event boost: within post_event_hours after event
        # resolution, if P/C is collapsing from an elevated level
        # (hedges unwinding), amplify the long lean — this is the
        # quantified version of the IV-crush re-entry pattern.
        post_event_boost_applied = False
        if opt.event_hours_since is not None and opt.event_hours_since <= self._post_event_hours:
            if opt.pc_ratio_change is not None and opt.pc_ratio_change < -self._unwind_threshold:
                value = min(
                    0.8,
                    max(value, 0.0) + self._post_event_kicker,
                )
                post_event_boost_applied = True

        return AlgoSignal(
            name=self.name,
            value=max(-1.0, min(1.0, value)),
            weight=1.0,
            metadata={
                "pcr": pcr,
                "pcr_z": round(pcr_z, 4),
                "event_pending": event_pending,
                "post_event_boost": post_event_boost_applied,
            },
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "pcr_baseline": self._pcr_baseline,
            "pcr_std": self._pcr_std,
            "extreme_z": self._extreme_z,
            "contrarian_scale": self._contrarian_scale,
            "confirm_scale": self._confirm_scale,
            "event_window_hours": self._event_window_hours,
            "post_event_hours": self._post_event_hours,
            "unwind_threshold": self._unwind_threshold,
            "post_event_kicker": self._post_event_kicker,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key in (
            "pcr_baseline",
            "pcr_std",
            "extreme_z",
            "contrarian_scale",
            "confirm_scale",
            "event_window_hours",
            "post_event_hours",
            "unwind_threshold",
            "post_event_kicker",
        ):
            if key in params:
                setattr(self, f"_{key}", float(params[key]))

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "pcr_std" in params:
            val = float(params["pcr_std"])
            if val <= 0:
                errors.append(f"pcr_std must be positive, got {val}")
        if "extreme_z" in params:
            val = float(params["extreme_z"])
            if val <= 0:
                errors.append(f"extreme_z must be positive, got {val}")
        if "contrarian_scale" in params:
            val = float(params["contrarian_scale"])
            if not (0.0 < val <= 1.0):
                errors.append(f"contrarian_scale must be in (0, 1], got {val}")
        if "confirm_scale" in params:
            val = float(params["confirm_scale"])
            if not (0.0 < val <= 1.0):
                errors.append(f"confirm_scale must be in (0, 1], got {val}")
        if "event_window_hours" in params:
            val = float(params["event_window_hours"])
            if val <= 0:
                errors.append(f"event_window_hours must be positive, got {val}")
        if "post_event_hours" in params:
            val = float(params["post_event_hours"])
            if val <= 0:
                errors.append(f"post_event_hours must be positive, got {val}")
        if "unwind_threshold" in params:
            val = float(params["unwind_threshold"])
            if val <= 0:
                errors.append(f"unwind_threshold must be positive, got {val}")
        if "post_event_kicker" in params:
            val = float(params["post_event_kicker"])
            if not (0.0 <= val <= 1.0):
                errors.append(f"post_event_kicker must be in [0, 1], got {val}")
        return errors
