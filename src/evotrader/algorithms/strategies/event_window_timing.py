"""Event Window Timing strategy.

Algorithmically encodes the system's most-validated behavioural edge:
suppress new long-premium directional entries inside a binary-event window
(earnings cluster / macro print), then amplify post-event re-entry when IV
has crushed and a VWAP-overextension mean-reversion setup persists.

This strategy operates as a **gating + amplifying overlay** within the
composite ensemble:
- Outside event windows → ``applicable=False`` (zero dead weight via
  ``renormalize_on_abstain``)
- Inside pre-event blackout → near-zero dampening signal
- Inside post-event amplification window → boosted mean-reversion signal

Empirical basis:
- 5-6 documented losses came from entering during binary events
- Both verified winners were post-event VWAP-fade entries

Phase 1 note: The required event context data (ATM IV, earnings proximity,
macro calendar) is not yet in ``TechnicalIndicators``.  The strategy
gracefully abstains until those fields are populated.
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class EventWindowTimingStrategy(TradingAlgorithm):
    """Event-aware timing overlay for the composite ensemble.

    Modes of operation:
    1. **No event context available** → abstain (``applicable=False``).
       This is the Phase 1 default while the data pipeline is not yet
       connected.
    2. **Pre-event blackout** (within ``blackout_hours`` of a high-impact
       event) → emit a near-zero signal that dampens the composite.
    3. **Post-event amplification** (within ``post_event_hours`` of event
       clearing, AND IV has crushed by ``min_iv_crush``, AND VWAP
       overextension persists) → emit a boosted mean-reversion signal.
    4. **Between events** → abstain (no event context to act on).
    """

    def __init__(
        self,
        blackout_hours: float = 30.0,
        post_event_hours: float = 48.0,
        min_iv_crush: float = 0.10,
        min_overextension: float = 0.015,
        pre_event_dampener: float = 0.05,
        post_event_boost: float = 1.5,
        version: str = "v001",
    ) -> None:
        self._blackout_hours = blackout_hours
        self._post_event_hours = post_event_hours
        self._min_iv_crush = min_iv_crush
        self._min_overextension = min_overextension
        self._pre_event_dampener = pre_event_dampener
        self._post_event_boost = post_event_boost
        self._version = version

    @property
    def name(self) -> str:
        return "event_window_timing"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Event window timing overlay "
            f"(blackout={self._blackout_hours}h, "
            f"post_event={self._post_event_hours}h, "
            f"min_iv_crush={self._min_iv_crush:.0%})"
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute event-window gating/amplification signal.

        This strategy operates as a **multiplicative overlay**: it declares
        ``role='multiplier'`` in its metadata so that the composite applies
        its ``factor`` as a post-aggregation scalar rather than averaging
        the signal value in as a small additive term.

        Modes:

        1. PRE-EVENT BLACKOUT: Inside the blackout window → emit
           ``factor=pre_event_dampener`` (e.g. 0.05), which scales the
           entire composite toward zero — effectively suppressing conviction.

        2. POST-EVENT AMPLIFIER: Event cleared recently AND IV crushed AND
           mean-reversion setup persists → emit ``factor=post_event_boost``
           (e.g. 1.5), amplifying the composite's directional signal.

        3. NO EVENT CONTEXT: Outside event windows OR required data missing
           → abstain (``applicable=False``), contributing zero dead weight
           and no overlay effect.
        """
        ind = snapshot.indicators

        # ── Guard: abstain if event context data is missing ──
        # These fields are populated by the Market Intelligence Agent
        # once the event data pipeline is connected.
        hours_to = ind.hours_to_event
        hours_since = ind.hours_since_event
        iv_now = ind.atm_iv_30dte
        iv_pre = ind.atm_iv_pre_event
        event_type = ind.event_type

        has_event_context = hours_to is not None or hours_since is not None

        if not has_event_context:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "mode": "no_event_data",
                    "reason": "event_context_not_available",
                },
            )

        # ── Mode 1: Pre-event blackout ──
        if hours_to is not None and hours_to <= self._blackout_hours:
            return AlgoSignal(
                name=self.name,
                value=self._pre_event_dampener,
                weight=1.0,
                metadata={
                    "applicable": True,
                    "role": "multiplier",
                    "factor": self._pre_event_dampener,
                    "mode": "pre_event_blackout",
                    "hours_to_event": round(hours_to, 2),
                    "event_type": event_type or "unknown",
                },
            )

        # ── Mode 2: Post-event amplification ──
        if hours_since is not None and hours_since <= self._post_event_hours:
            # Check IV crush condition (if IV data available)
            iv_crush = 0.0
            iv_crush_met = False
            if iv_now is not None and iv_pre is not None and iv_pre > 0:
                iv_crush = (iv_pre - iv_now) / iv_pre
                iv_crush_met = iv_crush >= self._min_iv_crush
            else:
                # If no IV data, we cannot confirm the crush condition.
                # Conservative: abstain rather than guess.
                return AlgoSignal(
                    name=self.name,
                    value=0.0,
                    weight=1.0,
                    metadata={
                        "applicable": False,
                        "mode": "post_event_no_iv",
                        "hours_since_event": round(hours_since, 2),
                        "reason": "iv_data_not_available",
                    },
                )

            # Check VWAP overextension condition
            vwap = ind.vwap
            price = snapshot.quote.last
            overextension = 0.0
            overextension_met = False
            if vwap is not None and vwap > 0:
                overextension = (price - vwap) / vwap
                overextension_met = abs(overextension) >= self._min_overextension

            if iv_crush_met and overextension_met:
                # Determine reversion direction:
                # Price above VWAP → fade down (bearish)
                # Price below VWAP → fade up (bullish)
                reversion_direction = -1.0 if overextension > 0 else 1.0
                magnitude = math.tanh(abs(overextension) / self._min_overextension)
                boosted = max(
                    -1.0,
                    min(
                        1.0,
                        reversion_direction * magnitude * self._post_event_boost,
                    ),
                )

                return AlgoSignal(
                    name=self.name,
                    value=boosted,
                    weight=1.0,
                    metadata={
                        "applicable": True,
                        "role": "multiplier",
                        "factor": self._post_event_boost,
                        "mode": "post_event_entry",
                        "hours_since_event": round(hours_since, 2),
                        "iv_crush": round(iv_crush, 4),
                        "overextension": round(overextension, 4),
                        "event_type": event_type or "unknown",
                    },
                )

            # Post-event window but conditions not met → abstain
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "mode": "post_event_conditions_not_met",
                    "hours_since_event": round(hours_since, 2),
                    "iv_crush": round(iv_crush, 4),
                    "iv_crush_met": iv_crush_met,
                    "overextension": round(overextension, 4),
                    "overextension_met": overextension_met,
                },
            )

        # ── Mode 3: No active event window → abstain ──
        return AlgoSignal(
            name=self.name,
            value=0.0,
            weight=1.0,
            metadata={
                "applicable": False,
                "mode": "no_event",
                "reason": "outside_event_windows",
            },
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "blackout_hours": self._blackout_hours,
            "post_event_hours": self._post_event_hours,
            "min_iv_crush": self._min_iv_crush,
            "min_overextension": self._min_overextension,
            "pre_event_dampener": self._pre_event_dampener,
            "post_event_boost": self._post_event_boost,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key in (
            "blackout_hours",
            "post_event_hours",
            "min_iv_crush",
            "min_overextension",
            "pre_event_dampener",
            "post_event_boost",
        ):
            if key in params:
                setattr(self, f"_{key}", params[key])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "blackout_hours" in params:
            bh = float(params["blackout_hours"])
            if bh <= 0:
                errors.append(f"blackout_hours must be positive, got {bh}")
        if "post_event_hours" in params:
            ph = float(params["post_event_hours"])
            if ph <= 0:
                errors.append(f"post_event_hours must be positive, got {ph}")
        if "min_iv_crush" in params:
            mic = float(params["min_iv_crush"])
            if not (0.0 < mic < 1.0):
                errors.append(f"min_iv_crush must be in (0, 1), got {mic}")
        if "min_overextension" in params:
            mo = float(params["min_overextension"])
            if mo <= 0:
                errors.append(f"min_overextension must be positive, got {mo}")
            elif mo > 0.5:
                # overextension is a FRACTION of VWAP ((price-vwap)/vwap),
                # so 1.5 would demand a 150% deviation and the post-event
                # amplifier could never fire.  Reject the 100x units error.
                errors.append(
                    f"min_overextension is a FRACTION of VWAP, not percent: "
                    f"use 0.015 for 1.5%. Got {mo} (would require "
                    f"{mo * 100:.0f}% deviation and never fire)."
                )
        if "pre_event_dampener" in params:
            ped = float(params["pre_event_dampener"])
            if not (-1.0 <= ped <= 1.0):
                errors.append(f"pre_event_dampener must be in [-1, 1], got {ped}")
        if "post_event_boost" in params:
            peb = float(params["post_event_boost"])
            if peb <= 0:
                errors.append(f"post_event_boost must be positive, got {peb}")
        return errors
