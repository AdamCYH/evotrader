"""Range-break continuation strategy.

Detects genuine intraday range breakdowns/breakouts inside a nominally
range_bound regime and emits a continuation signal in the break direction.
Counterweights the VWAP fade during confirmed breaks and captures the
continuation edge directly.

Best in: Range Bound regime (activates on confirmed band breaks).
"""

from __future__ import annotations

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


class RangeBreakContinuationStrategy(TradingAlgorithm):
    """Continuation signal on confirmed intraday Bollinger Band breaks.

    Emits a directional signal when price penetrates the Bollinger Bands
    with both MACD momentum confirmation and sufficient intraday magnitude.
    Returns neutral (0.0) whenever no confirmed break — it must NOT act
    as another mean-reversion voice.
    """

    def __init__(
        self,
        min_break_pct: float = 1.2,
        # The same gate in the instrument's own units: the day's move as a
        # multiple of daily ATR. None (the default) keeps the percent gate, so
        # a config that does not set it behaves as before. See algorithms/units.
        min_break_atr: float | None = None,
        # ── Confirmation ─────────────────────────────────────────────
        # A Bollinger-band break is by definition a ONE-DAY event, so its
        # confirmation must be available on the day it happens. The daily
        # MACD histogram is not: a 12/26 EMA differential against its 9-day
        # signal barely moves on a one-day jump, so it lagged every breakout
        # by construction and the two gates were near-mutually exclusive.
        # Live 2026-09-18 11:36: close 148.345 > upper band 148.217, day_chg
        # +12.17%, macd_histogram -0.25 vs a 0.75 floor -> no fire. This
        # channel was 0-for-18 because price sat INSIDE the bands; on the
        # first day the band gate was reachable, the MACD gate blocked it.
        #
        # Confirm with what the tape says RIGHT NOW instead: price on the
        # break side of session VWAP, and IBS near the break-side extreme.
        vwap_confirm: bool = True,
        ibs_confirm: float = 0.70,
        # Optional secondary gate. Now compared against histogram/ATR so it is
        # unit-free — the old 0.75 was $0.75 on a $150 stock with a $9.50 ATR.
        # Default 0.0 = OFF; the daily MACD cannot confirm a one-day event.
        macd_confirm_floor: float = 0.0,
        base_strength: float = 0.45,
        penetration_scale: float = 0.35,
        version: str = "v001",
    ) -> None:
        self._min_break_pct = min_break_pct
        self._min_break_atr = min_break_atr
        self._vwap_confirm = vwap_confirm
        self._ibs_confirm = ibs_confirm
        self._macd_confirm_floor = macd_confirm_floor
        self._base_strength = base_strength
        self._penetration_scale = penetration_scale
        self._version = version

    @property
    def resolution(self) -> str:
        """Intraday: the bands are daily, but the gate that decides whether the
        channel fires — price vs session VWAP and the bar's close position
        (IBS) — changes every cycle. `resolution` answers "can this change
        between hourly cycles?", and here it can.

        `decay_hours` was removed on 2026-09-22: it was stored, validated and
        returned, and never read by compute_signal, so configs advertised a
        3-hour decay that did not exist. A real decay needs the break bar's
        timestamp, which the sampling-interval scan (carry-forward) provides.
        """
        return "intraday"

    @property
    def name(self) -> str:
        return "range_break_continuation"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Range-break continuation: emits directional signal on confirmed "
            f"Bollinger Band breaks with MACD + magnitude confirmation "
            f"(min_break={self._min_break_label()}, "
            f"base_strength={self._base_strength})."
        )

    def _min_break_label(self) -> str:
        if self._min_break_atr is not None:
            return f"{self._min_break_atr:g} ATR (fallback {self._min_break_pct:g}%)"
        return f"{self._min_break_pct:.1f}%"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute continuation signal from confirmed intraday band breaks.

        Returns negative (bearish continuation) when price breaks below the
        lower Bollinger Band with momentum acceleration; positive symmetric
        logic for upside breaks. Neutral (0.0) whenever no confirmed break.
        """
        ind = snapshot.indicators
        close = snapshot.quote.last

        required = (
            ind.bollinger_upper,
            ind.bollinger_lower,
            ind.bollinger_middle,
            ind.macd_histogram,
            ind.atr_14,
        )
        if any(v is None for v in required):
            return AlgoSignal(
                name=self.name, value=0.0, weight=1.0, metadata={"reason": "missing_indicators"}
            )

        band_width = ind.bollinger_upper - ind.bollinger_lower
        if band_width <= 0 or ind.atr_14 <= 0:
            return AlgoSignal(
                name=self.name, value=0.0, weight=1.0, metadata={"reason": "invalid_bands_or_atr"}
            )

        signal = 0.0
        meta: dict[str, Any] = {}

        # Prefer snapshot.daily_change_pct; fall back to computing from the
        # quote's previous_close so a missing field cannot silently disable
        # the strategy (7/23 non-fire incident).
        day_chg = snapshot.daily_change_pct
        if day_chg is not None:
            meta["day_chg_source"] = "snapshot"
        else:
            prev = getattr(snapshot.quote, "previous_close", None)
            if prev and prev > 0:
                day_chg = ((close - prev) / prev) * 100
                meta["day_chg_source"] = "fallback_prev_close"
            else:
                # Cannot evaluate the magnitude gate at all. Returning 0.0
                # with applicable=True casts a silent NEUTRAL vote that
                # dilutes the composite by this strategy's full weight
                # (0.15-0.18 in range_bound) — the same dead-weight
                # dilution class of bug that motivated algo v008.
                meta["day_chg_source"] = "missing"
                meta["applicable"] = False
                meta["reason"] = "day_change_unavailable"
                return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        # ── Magnitude gate, in the instrument's own units when configured ──
        ref_close = previous_close(
            close, day_chg, getattr(snapshot.quote, "previous_close", None)
        )
        magnitude = check_move(
            fallback_move=day_chg,
            fallback_threshold=self._min_break_pct,
            move_atr=pct_move_in_atr(day_chg, ref_close, ind.atr_14),
            threshold_atr=self._min_break_atr,
        )
        meta["magnitude_basis"] = magnitude.basis
        if magnitude.observed_atr is not None:
            meta["day_chg_atr"] = round(magnitude.observed_atr, 4)

        # Always emit diagnostic metadata for auditability — even on non-fire
        meta["day_chg"] = round(day_chg, 4)
        meta["macd_histogram"] = round(ind.macd_histogram, 3)
        meta["close_vs_lower"] = round(close - ind.bollinger_lower, 3)
        meta["close_vs_upper"] = round(close - ind.bollinger_upper, 3)

        # ── Intraday confirmation, available at the moment of the break ──
        # Reads the stack from snapshot.indicators directly. The VWAP leg
        # requires a current-session anchor: a stale anchor is a plumbing
        # fault, and a plumbing fault must never confirm a breakout.
        vwap_ok = (
            ind.vwap is not None
            and ind.atr_14
            and (ind.vwap_anchor or "unknown") == "current_session"
        )
        ibs_ok = ind.ibs is not None

        def _confirms(upside: bool) -> tuple[bool, str]:
            sources: list[str] = []
            if self._vwap_confirm:
                if not vwap_ok:
                    return False, "vwap_unavailable"
                if not ((close > ind.vwap) if upside else (close < ind.vwap)):
                    return False, "wrong_side_of_vwap"
                sources.append("vwap")
            if self._ibs_confirm > 0:
                if not ibs_ok:
                    return False, "ibs_unavailable"
                ibs_hit = (
                    ind.ibs >= self._ibs_confirm if upside else ind.ibs <= 1.0 - self._ibs_confirm
                )
                if not ibs_hit:
                    return False, "ibs_not_at_extreme"
                sources.append("ibs")
            if self._macd_confirm_floor > 0:
                # Unit-free: histogram expressed in ATRs.
                if ind.macd_histogram is None or not ind.atr_14:
                    return False, "macd_unavailable"
                h_atr = ind.macd_histogram / ind.atr_14
                if not (
                    (h_atr > self._macd_confirm_floor)
                    if upside
                    else (h_atr < -self._macd_confirm_floor)
                ):
                    return False, "macd_not_confirming"
                sources.append("macd_atr")
            return True, "+".join(sources) or "unconditional"

        # ── Bearish break: price penetrates BELOW lower band ──
        if close < ind.bollinger_lower:
            penetration = (ind.bollinger_lower - close) / ind.atr_14
            confirmed, source = _confirms(upside=False)
            magnitude_confirms = day_chg < 0 and magnitude.met
            meta["confirm_source"] = source

            if confirmed and magnitude_confirms:
                signal = -min(1.0, self._base_strength + penetration * self._penetration_scale)
                meta["break_side"] = "lower"
                meta["penetration_atr"] = round(penetration, 3)

        # ── Bullish break: symmetric ──
        elif close > ind.bollinger_upper:
            penetration = (close - ind.bollinger_upper) / ind.atr_14
            confirmed, source = _confirms(upside=True)
            magnitude_confirms = day_chg > 0 and magnitude.met
            meta["confirm_source"] = source

            if confirmed and magnitude_confirms:
                signal = min(1.0, self._base_strength + penetration * self._penetration_scale)
                meta["break_side"] = "upper"
                meta["penetration_atr"] = round(penetration, 3)

        # ── Reflexive dampener handshake ──
        # Emit meta['active_break'] so the composite can optionally attenuate
        # the intraday_vwap_zscore fade while a confirmed break is live.
        meta["active_break"] = 1 if signal != 0.0 else 0

        # ── SCOPE MARKER ──────────────────────────────────────
        # With price inside both bands this channel CANNOT fire, whatever the
        # market does — the first gate is a band penetration and it is simply
        # not present. That is different from having evaluated a break and
        # judged it insufficient, and consumers need to tell the two apart:
        # an off-duty channel counted as a silent one understates ensemble
        # participation and triggers an unearned attenuation haircut.
        #
        # Measured basis: 0-for-18 live cycles, with close sitting $7.45-$8.69
        # INSIDE the lower band all of 2026-09-10 — the gate was unreachable
        # every cycle, not narrowly missed.
        #
        # This is explicitly NOT a demotion. On a real break the channel fires
        # at full authority and rejoins the denominator automatically.
        if signal == 0.0 and ind.bollinger_lower <= close <= ind.bollinger_upper:
            meta["in_scope"] = False
            meta["out_of_scope_reason"] = "price_inside_bands"

        return AlgoSignal(name=self.name, value=signal, weight=1.0, metadata=meta)

    def get_parameters(self) -> dict[str, Any]:
        return {
            "min_break_pct": self._min_break_pct,
            "min_break_atr": self._min_break_atr,
            "vwap_confirm": self._vwap_confirm,
            "ibs_confirm": self._ibs_confirm,
            "macd_confirm_floor": self._macd_confirm_floor,
            "base_strength": self._base_strength,
            "penetration_scale": self._penetration_scale,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        if "min_break_pct" in params:
            self._min_break_pct = float(params["min_break_pct"])
        if "min_break_atr" in params:
            v = params["min_break_atr"]
            self._min_break_atr = None if v is None else float(v)
        if "macd_confirm_floor" in params:
            self._macd_confirm_floor = float(params["macd_confirm_floor"])
        if "vwap_confirm" in params:
            self._vwap_confirm = bool(params["vwap_confirm"])
        if "ibs_confirm" in params:
            self._ibs_confirm = float(params["ibs_confirm"])
        if "base_strength" in params:
            self._base_strength = float(params["base_strength"])
        if "penetration_scale" in params:
            self._penetration_scale = float(params["penetration_scale"])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "min_break_pct" in params:
            v = float(params["min_break_pct"])
            if v <= 0 or v > 10.0:
                errors.append(
                    f"min_break_pct must be in (0, 10.0] percent (e.g. 1.2 = 1.2%), got {v}"
                )
        if "min_break_atr" in params:
            errors.extend(validate_threshold_atr("min_break_atr", params["min_break_atr"]))
        if "ibs_confirm" in params:
            v = float(params["ibs_confirm"])
            if not (0.0 <= v <= 1.0):
                errors.append("ibs_confirm must be in [0, 1] (0 disables the IBS gate)")
        if "macd_confirm_floor" in params:
            v = float(params["macd_confirm_floor"])
            if v < 0:
                errors.append("macd_confirm_floor must be >= 0")
        if "base_strength" in params:
            v = float(params["base_strength"])
            if v <= 0 or v > 1.0:
                errors.append("base_strength must be in (0, 1.0]")
        if "penetration_scale" in params:
            v = float(params["penetration_scale"])
            if v < 0 or v > 2.0:
                errors.append("penetration_scale must be in [0, 2.0]")
        return errors
