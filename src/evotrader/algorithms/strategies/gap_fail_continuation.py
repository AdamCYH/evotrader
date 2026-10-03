"""Gap-fail continuation: a gap that filled and kept going.

The ``gap`` channel bets on the FILL: after a large opening gap it votes
against the gap, scales the vote by the part of the gap still unfilled, and
fades to nothing two hours into the session. So the moment a gap-up has fully
filled, ``gap`` reads 0.0 and nothing in the ensemble can vote on what happens
next, although that is exactly when a failed gap-up most often keeps falling:
the buyers who paid up at the open are all under water, and price is below the
prior close and below the session's average price (VWAP) at the same time.

This channel starts where ``gap`` stops. After a gap-up, once price is THROUGH
the prior close (the gap has more than filled), on the far side of session VWAP,
and has held there for the last few bars, it votes bearish, in the direction of
the fade. Mirrored, after a failed gap-down it votes bullish. Within the regular
session in a bull stack the ensemble has had no bearish author at all; this is
one, with its own gating rather than a sign flip of an existing channel.

WHAT IT READS

* ``gap_pct``, as the ``gap`` channel reads it: the snapshot's gap, from
  whichever source the market-data tool found (the quote in the first minutes,
  the first 5-minute bar during the session, today's daily bar after the close).
* the previous close (``units.previous_close``), daily ATR, session VWAP with a
  ``current_session`` anchor, and the recent 5-minute closes for the hold test.
* the daily MA stack, for ``stack_context`` metadata only, so the firing record
  can be split by whether the fade ran against or with the daily trend.

GATES, in order

1. The gap must be real: at least ``min_gap_atr`` daily ATRs (or ``min_gap_pct``
   when no ATR can be measured). Smaller: off duty (``applicable: False``), as
   ``gap`` itself reads a small gap.
2. The opening range must be over (``min_minutes_since_open``): the first hour
   is the ``gap`` channel's fill window; this channel reads what happens after.
3. Session VWAP and intraday bars must exist. After the close and before the
   open the VWAP anchor is the prior session's, and the channel abstains.
4. Then, with a real gap in play (genuine zeros from here on, ``applicable``):
   price must be through the prior close by ``min_penetration_atr``, on the far
   side of VWAP by ``min_vwap_dev_atr``, and the last ``hold_bars`` 5-minute
   closes must all sit through the prior close. The hold test exists so a single
   print a cent through the prior close does not fire the channel; at an hourly
   cycle cadence three bars is a 15-minute hold.

STRENGTH

``base_strength`` x tanh(gap / ``gap_scale_atr``) x min(1, 0.5 + penetration /
``penetration_scale_atr``): a bigger gap that failed is a bigger trap, and a
deeper break below the prior close is a more complete failure. No decay until
``late_session_minute``, then a linear fade to 0 at the close, so a vote read
in the last half hour is not acted on into the bell.

SHADOW MODE: this ships at weight 0.0 in every regime, so it runs and its
firings are recorded in every snapshot but it does not move the composite.
Promote it on a scored record of firings, split by leg (``failed_gap_up`` /
``failed_gap_down``) and by ``stack_context``, never on the reasoning above.
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.algorithms.units import (
    check_move,
    pct_move_in_atr,
    previous_close,
    validate_threshold_atr,
)
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal
from evotrader.tools.market_hours import minutes_since_open


class GapFailContinuationStrategy(TradingAlgorithm):
    """Continuation of a fully faded gap, in the direction of the fade."""

    def __init__(
        self,
        min_gap_pct: float = 1.5,
        min_gap_atr: float | None = 0.3,
        min_penetration_atr: float = 0.05,
        min_vwap_dev_atr: float = 0.10,
        hold_bars: int = 3,
        base_strength: float = 0.6,
        gap_scale_atr: float = 0.5,
        penetration_scale_atr: float = 0.5,
        min_minutes_since_open: float = 60.0,
        late_session_minute: float = 360.0,
        session_minutes: float = 390.0,
        version: str = "v001",
        **kwargs: Any,
    ) -> None:
        self._min_gap_pct = min_gap_pct
        self._min_gap_atr = min_gap_atr
        self._min_penetration_atr = min_penetration_atr
        self._min_vwap_dev_atr = min_vwap_dev_atr
        self._hold_bars = hold_bars
        self._base_strength = base_strength
        self._gap_scale_atr = gap_scale_atr
        self._penetration_scale_atr = penetration_scale_atr
        self._min_minutes_since_open = min_minutes_since_open
        self._late_session_minute = late_session_minute
        self._session_minutes = session_minutes
        self._version = version

    @property
    def name(self) -> str:
        return "gap_fail_continuation"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        gap = (
            f"{self._min_gap_atr:g} ATR"
            if self._min_gap_atr is not None
            else f"{self._min_gap_pct:g}%"
        )
        return (
            f"Failed-gap continuation: gap >= {gap}, price through the prior close by "
            f">= {self._min_penetration_atr:g} ATR and past VWAP by "
            f">= {self._min_vwap_dev_atr:g} ATR, held {self._hold_bars} bars."
        )

    @property
    def resolution(self) -> str:
        """Intraday: reads session VWAP and the recent 5-minute bars every cycle."""
        return "intraday"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        ind = snapshot.indicators
        quote = snapshot.quote
        gap_pct = snapshot.gap_pct
        price = quote.last
        atr = ind.atr_14
        vwap = ind.vwap
        anchor = ind.vwap_anchor or "unknown"
        candles = snapshot.recent_candles or []

        # ── Off duty: nothing to read (applicable=False, out of the denominator)
        if gap_pct is None or price is None or price <= 0 or atr is None or atr <= 0:
            return self._abstain("no_gap_or_atr", applicable=False)

        prev_close = previous_close(price, snapshot.daily_change_pct, quote.previous_close)
        if prev_close is None or prev_close <= 0:
            return self._abstain("no_previous_close", applicable=False)

        gap_atr = pct_move_in_atr(gap_pct, prev_close, atr)
        entry = check_move(
            fallback_move=gap_pct,
            fallback_threshold=self._min_gap_pct,
            move_atr=gap_atr,
            threshold_atr=self._min_gap_atr,
        )
        gap_meta = {
            "gap_pct": round(gap_pct, 4),
            "gap_atr": round(gap_atr, 4) if gap_atr is not None else None,
            "gap_basis": entry.basis,
        }
        if not entry.met:
            return self._abstain("gap_too_small", applicable=False, **gap_meta)

        elapsed = minutes_since_open(snapshot.timestamp)
        if elapsed is None or elapsed < self._min_minutes_since_open:
            return self._abstain(
                "opening_range_forming",
                applicable=False,
                elapsed_min=round(elapsed, 2) if elapsed is not None else None,
                **gap_meta,
            )
        if vwap is None or anchor != "current_session" or not candles:
            return self._abstain(
                "no_session_vwap", applicable=False, vwap_anchor=anchor, **gap_meta
            )

        # ── In scope: a real gap, an hour or more into a live session ─────────
        sign = 1.0 if gap_pct > 0 else -1.0
        # Positive once price is THROUGH the prior close, against the gap.
        penetration_atr = sign * (prev_close - price) / atr
        # Positive once price is on the far side of session VWAP, against the gap.
        vwap_dev_atr = sign * (vwap - price) / atr
        if self._hold_bars > 0:
            tail = candles[-self._hold_bars :]
            fill_held = len(tail) == self._hold_bars and all(
                sign * (prev_close - c.close) > 0 for c in tail
            )
        else:
            fill_held = True  # the hold test is switched off
        common: dict[str, Any] = {
            **gap_meta,
            "previous_close": round(prev_close, 4),
            "penetration_atr": round(penetration_atr, 4),
            "vwap_dev_atr": round(vwap_dev_atr, 4),
            "fill_held": fill_held,
            "elapsed_min": round(elapsed, 2),
            "stack_context": _stack_context(price, ind),
            "leg": "failed_gap_up" if sign > 0 else "failed_gap_down",
        }
        if penetration_atr < self._min_penetration_atr:
            return self._abstain("gap_not_failed", applicable=True, **common)
        if vwap_dev_atr < self._min_vwap_dev_atr:
            return self._abstain("vwap_not_lost", applicable=True, **common)
        if not fill_held:
            return self._abstain("fill_not_held", applicable=True, **common)

        # ── Strength ──────────────────────────────────────────────────────────
        gap_term = math.tanh((gap_atr or 0.0) / self._gap_scale_atr)
        pen_term = min(1.0, 0.5 + penetration_atr / self._penetration_scale_atr)
        value = -sign * self._base_strength * gap_term * pen_term
        if elapsed <= self._late_session_minute:
            decay = 1.0
        else:
            remaining = self._session_minutes - self._late_session_minute
            decay = max(0.0, 1.0 - (elapsed - self._late_session_minute) / remaining)
        value = max(-1.0, min(1.0, value * decay))
        return AlgoSignal(
            name=self.name,
            value=value,
            weight=1.0,
            metadata={
                **common,
                "applicable": True,
                "fired": True,
                "reason": "gap_failed",
                "gap_term": round(gap_term, 4),
                "penetration_term": round(pen_term, 4),
                "decay_factor": round(decay, 4),
            },
        )

    def _abstain(self, reason: str, *, applicable: bool, **meta: Any) -> AlgoSignal:
        return AlgoSignal(
            name=self.name,
            value=0.0,
            weight=1.0,
            metadata={**meta, "applicable": applicable, "reason": reason},
        )

    def get_parameters(self) -> dict[str, Any]:
        return {
            "min_gap_pct": self._min_gap_pct,
            "min_gap_atr": self._min_gap_atr,
            "min_penetration_atr": self._min_penetration_atr,
            "min_vwap_dev_atr": self._min_vwap_dev_atr,
            "hold_bars": self._hold_bars,
            "base_strength": self._base_strength,
            "gap_scale_atr": self._gap_scale_atr,
            "penetration_scale_atr": self._penetration_scale_atr,
            "min_minutes_since_open": self._min_minutes_since_open,
            "late_session_minute": self._late_session_minute,
            "session_minutes": self._session_minutes,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        float_keys = (
            "min_gap_pct",
            "min_penetration_atr",
            "min_vwap_dev_atr",
            "base_strength",
            "gap_scale_atr",
            "penetration_scale_atr",
            "min_minutes_since_open",
            "late_session_minute",
            "session_minutes",
        )
        for key in float_keys:
            if key in params:
                setattr(self, f"_{key}", float(params[key]))
        if "hold_bars" in params:
            self._hold_bars = int(params["hold_bars"])
        if "min_gap_atr" in params:
            v = params["min_gap_atr"]
            self._min_gap_atr = None if v is None else float(v)

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []

        def _check(key: str, low: float, high: float, *, low_open: bool = False) -> None:
            if key not in params:
                return
            try:
                value = float(params[key])
            except (TypeError, ValueError):
                errors.append(f"{key} must be numeric, got {params[key]!r}")
                return
            too_low = value <= low if low_open else value < low
            if too_low or value > high:
                bound = "(" if low_open else "["
                errors.append(f"{key} must be in {bound}{low}, {high}], got {value}")

        _check("min_gap_pct", 0.0, 10.0)
        _check("min_penetration_atr", 0.0, 5.0)
        _check("min_vwap_dev_atr", 0.0, 5.0)
        _check("hold_bars", 0, 20)
        _check("base_strength", 0.0, 1.0, low_open=True)
        _check("gap_scale_atr", 0.0, 10.0, low_open=True)
        _check("penetration_scale_atr", 0.0, 10.0, low_open=True)
        _check("min_minutes_since_open", 0.0, 390.0)
        _check("late_session_minute", 0.0, 480.0)
        _check("session_minutes", 0.0, 480.0, low_open=True)
        if "min_gap_atr" in params:
            errors.extend(validate_threshold_atr("min_gap_atr", params["min_gap_atr"]))

        # Cross-check: a fade that starts at or after the close never applies.
        late = params.get("late_session_minute", self._late_session_minute)
        session = params.get("session_minutes", self._session_minutes)
        try:
            if float(late) >= float(session):
                errors.append(
                    f"late_session_minute ({late}) must be < session_minutes ({session}); "
                    f"otherwise the late-session fade never starts."
                )
        except (TypeError, ValueError):
            pass
        return errors


def _stack_context(price: float, ind: Any) -> str:
    """The daily MA stack, read as ``intraday_vwap_zscore`` reads it; metadata only."""
    mas = (ind.ema_9, ind.ema_21, ind.sma_20, ind.sma_50)
    if any(v is None for v in mas):
        return "no_stack"
    if price > ind.ema_9 > ind.ema_21 and ind.sma_20 > ind.sma_50:
        return "bull"
    if price < ind.ema_9 < ind.ema_21 and ind.sma_20 < ind.sma_50:
        return "bear"
    return "no_stack"
