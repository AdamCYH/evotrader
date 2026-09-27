"""Trend persistence (multi-session directional grind) strategy.

Detects slow-grind multi-session directional trends that daily ADX and
single-session thresholds both miss.  Measures trend by PERSISTENCE
(fraction of days moving in one direction) and CUMULATIVE DISPLACEMENT
(total move in ATR units) rather than by single-bar magnitude or a
lagging smoothed oscillator.

Symmetric: emits bearish on grinding declines and bullish on grinding
advances.  Includes exhaustion decay (suppresses piling in at
capitulation) and a counter-day guard (decays when today moves hard
against the established grind).

Best in: Range Bound regime (the only regime where existing strategies
lack a multi-session trend voice).
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


class TrendPersistenceStrategy(TradingAlgorithm):
    """Multi-session directional grind detection.

    Fires when a lookback window shows:
    1. Persistence — a supermajority of sessions closed in one direction
    2. Cumulative displacement — the total move exceeds a threshold in
       ATR units (not just magnitude — the part single-day rules miss)
    3. MA stack alignment — EMAs and SMAs ordered in trend direction
    4. Efficiency — directional travel / total path (Kaufman ratio)
       distinguishes a genuine one-way grind from chop that ends lower
       by accident

    Guards against:
    - Exhaustion: decays signal when cumulative move is very extended
    - Counter-day: decays when today's move opposes the established grind
    """

    def __init__(
        self,
        lookback_days: int = 7,
        min_persistence_frac: float = 0.6,
        min_cum_atr: float = 1.5,
        min_efficiency: float = 0.35,
        cum_scale: float = 2.0,
        base_strength: float = 0.75,
        exhaustion_atr: float = 5.0,
        exhaustion_decay: float = 0.5,
        counter_day_pct: float = 1.0,
        counter_day_decay: float = 0.5,
        # The counter-day size in the instrument's own units (multiples of
        # daily ATR). None keeps the percent rule. See algorithms/units.
        counter_day_atr: float | None = None,
        version: str = "v001",
    ) -> None:
        self._lookback_days = lookback_days
        self._min_persistence_frac = min_persistence_frac
        self._min_cum_atr = min_cum_atr
        self._min_efficiency = min_efficiency
        self._cum_scale = cum_scale
        self._base_strength = base_strength
        self._exhaustion_atr = exhaustion_atr
        self._exhaustion_decay = exhaustion_decay
        self._counter_day_pct = counter_day_pct
        self._counter_day_decay = counter_day_decay
        self._counter_day_atr = counter_day_atr
        self._version = version

    @property
    def name(self) -> str:
        return "trend_persistence"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return (
            f"Multi-session grind detection: persistence>={self._min_persistence_frac:.0%}, "
            f"cum_atr>={self._min_cum_atr}, efficiency>={self._min_efficiency}, "
            f"lookback={self._lookback_days}d."
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute trend persistence signal from daily candle history.

        Returns applicable=False when daily_candles is unavailable or
        insufficient (strategy must NOT silently read intraday data as
        daily).
        """
        ind = snapshot.indicators
        meta: dict[str, Any] = {}

        # ── 0. ABSTAIN GUARDS ──────────────────────────────────────
        if not snapshot.daily_candles or len(snapshot.daily_candles) < self._lookback_days:
            meta["applicable"] = False
            meta["reason"] = "insufficient_daily_candles"
            meta["daily_candles_count"] = (
                len(snapshot.daily_candles) if snapshot.daily_candles else 0
            )
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        if ind.atr_14 is None or ind.atr_14 <= 0:
            meta["applicable"] = False
            meta["reason"] = "missing_atr"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        if any(v is None for v in (ind.ema_9, ind.ema_21, ind.sma_20, ind.sma_50)):
            meta["applicable"] = False
            meta["reason"] = "missing_mas"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        # Use the last lookback_days candles
        bars = snapshot.daily_candles[-self._lookback_days :]
        closes = [b.close for b in bars]
        price = snapshot.quote.last

        # ── 1. PERSISTENCE ─────────────────────────────────────────
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        n = len(deltas)
        if n == 0:
            meta["applicable"] = False
            meta["reason"] = "no_deltas"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        down_days = sum(1 for d in deltas if d < 0)
        up_days = sum(1 for d in deltas if d > 0)
        down_frac = down_days / n
        up_frac = up_days / n

        # ── 2. CUMULATIVE DISPLACEMENT in ATR units ────────────────
        cum_move_atr = (closes[-1] - closes[0]) / ind.atr_14

        # ── 3. STRUCTURAL STACK ────────────────────────────────────
        bear_stack = price < ind.ema_9 < ind.ema_21 and ind.sma_20 < ind.sma_50
        bull_stack = price > ind.ema_9 > ind.ema_21 and ind.sma_20 > ind.sma_50

        # ── 4. EFFICIENCY RATIO (Kaufman) ──────────────────────────
        path = sum(abs(d) for d in deltas)
        efficiency = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0

        # Populate diagnostic metadata
        meta["down_frac"] = round(down_frac, 3)
        meta["up_frac"] = round(up_frac, 3)
        meta["cum_move_atr"] = round(cum_move_atr, 3)
        meta["bear_stack"] = bear_stack
        meta["bull_stack"] = bull_stack
        meta["efficiency"] = round(efficiency, 3)
        meta["lookback_bars"] = len(bars)

        # ── 5. FIRE CONDITIONS (symmetric) ─────────────────────────
        signal = 0.0
        bearish = (
            down_frac >= self._min_persistence_frac
            and cum_move_atr <= -self._min_cum_atr
            and bear_stack
            and efficiency >= self._min_efficiency
        )
        bullish = (
            up_frac >= self._min_persistence_frac
            and cum_move_atr >= self._min_cum_atr
            and bull_stack
            and efficiency >= self._min_efficiency
        )

        meta["bearish_trigger"] = bearish
        meta["bullish_trigger"] = bullish

        # ── SCOPE MARKER ──────────────────────────────────────
        # This channel detects a multi-session grind. When neither direction
        # holds a supermajority AND cumulative displacement is below the
        # minimum, there is no candidate grind for it to judge — its
        # preconditions are absent rather than unmet, and reporting 0.0 here
        # means "off duty", not "looked and found nothing".
        #
        # The distinction is deliberately narrow. If a supermajority OR the
        # displacement threshold IS present, the channel genuinely evaluated a
        # candidate trend and rejected it on the remaining gates (stack,
        # efficiency) — that is a real abstention and stays in the denominator.
        #
        # Measured basis: 0-for-18 live cycles, with down_frac 0.5,
        # cum_move_atr -0.822, bear_stack false and efficiency 0.214-0.252 —
        # all four gates failing at once, every cycle, in a quiet range.
        no_supermajority = max(up_frac, down_frac) < self._min_persistence_frac
        no_displacement = abs(cum_move_atr) < self._min_cum_atr
        if not bearish and not bullish and no_supermajority and no_displacement:
            meta["in_scope"] = False
            meta["out_of_scope_reason"] = "no_directional_grind"

        if bearish or bullish:
            direction = -1.0 if bearish else 1.0

            # Depth term: how far beyond the minimum cumulative displacement
            depth = math.tanh((abs(cum_move_atr) - self._min_cum_atr) / self._cum_scale)

            # Quality term: reward clean one-way travel over noisy drift
            denom = 1.0 - self._min_efficiency
            quality = (
                0.5 + 0.5 * min(1.0, (efficiency - self._min_efficiency) / denom)
                if denom > 0
                else 1.0
            )

            signal = direction * self._base_strength * depth * quality
            meta["depth"] = round(depth, 4)
            meta["quality"] = round(quality, 4)

            # ── 6. EXHAUSTION HAIRCUT ──────────────────────────────
            if abs(cum_move_atr) > self._exhaustion_atr:
                signal *= self._exhaustion_decay
                meta["exhaustion_applied"] = True

            # ── 7. COUNTER-DAY GUARD ──────────────────────────────
            day_chg = snapshot.daily_change_pct or 0.0
            counter = check_move(
                fallback_move=day_chg,
                fallback_threshold=self._counter_day_pct,
                move_atr=pct_move_in_atr(
                    day_chg,
                    previous_close(
                        price, day_chg, getattr(snapshot.quote, "previous_close", None)
                    ),
                    ind.atr_14,
                ),
                threshold_atr=self._counter_day_atr,
            )
            meta["counter_day_basis"] = counter.basis
            if signal * day_chg < 0 and counter.met:
                signal *= self._counter_day_decay
                meta["counter_day_applied"] = True

        signal = max(-1.0, min(1.0, signal))

        return AlgoSignal(name=self.name, value=signal, weight=1.0, metadata=meta)

    def get_parameters(self) -> dict[str, Any]:
        return {
            "lookback_days": self._lookback_days,
            "min_persistence_frac": self._min_persistence_frac,
            "min_cum_atr": self._min_cum_atr,
            "min_efficiency": self._min_efficiency,
            "cum_scale": self._cum_scale,
            "base_strength": self._base_strength,
            "exhaustion_atr": self._exhaustion_atr,
            "exhaustion_decay": self._exhaustion_decay,
            "counter_day_pct": self._counter_day_pct,
            "counter_day_decay": self._counter_day_decay,
            "counter_day_atr": self._counter_day_atr,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        if "lookback_days" in params:
            self._lookback_days = int(params["lookback_days"])
        if "min_persistence_frac" in params:
            self._min_persistence_frac = float(params["min_persistence_frac"])
        if "min_cum_atr" in params:
            self._min_cum_atr = float(params["min_cum_atr"])
        if "min_efficiency" in params:
            self._min_efficiency = float(params["min_efficiency"])
        if "cum_scale" in params:
            self._cum_scale = float(params["cum_scale"])
        if "base_strength" in params:
            self._base_strength = float(params["base_strength"])
        if "exhaustion_atr" in params:
            self._exhaustion_atr = float(params["exhaustion_atr"])
        if "exhaustion_decay" in params:
            self._exhaustion_decay = float(params["exhaustion_decay"])
        if "counter_day_pct" in params:
            self._counter_day_pct = float(params["counter_day_pct"])
        if "counter_day_decay" in params:
            self._counter_day_decay = float(params["counter_day_decay"])
        if "counter_day_atr" in params:
            v = params["counter_day_atr"]
            self._counter_day_atr = None if v is None else float(v)

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "lookback_days" in params:
            v = int(params["lookback_days"])
            if v < 3 or v > 30:
                errors.append(f"lookback_days must be in [3, 30], got {v}")
        if "min_persistence_frac" in params:
            v = float(params["min_persistence_frac"])
            if v < 0.5 or v > 1.0:
                errors.append(f"min_persistence_frac must be in [0.5, 1.0], got {v}")
        if "min_cum_atr" in params:
            v = float(params["min_cum_atr"])
            if v <= 0 or v > 10.0:
                errors.append(f"min_cum_atr must be in (0, 10.0], got {v}")
        if "min_efficiency" in params:
            v = float(params["min_efficiency"])
            if v < 0.0 or v > 1.0:
                errors.append(f"min_efficiency must be in [0, 1.0], got {v}")
        if "cum_scale" in params:
            v = float(params["cum_scale"])
            if v <= 0:
                errors.append(f"cum_scale must be > 0, got {v}")
        if "base_strength" in params:
            v = float(params["base_strength"])
            if v <= 0 or v > 1.0:
                errors.append(f"base_strength must be in (0, 1.0], got {v}")
        if "exhaustion_atr" in params:
            v = float(params["exhaustion_atr"])
            if v <= 0:
                errors.append(f"exhaustion_atr must be > 0, got {v}")
        if "exhaustion_decay" in params:
            v = float(params["exhaustion_decay"])
            if v < 0.0 or v > 1.0:
                errors.append(f"exhaustion_decay must be in [0, 1.0], got {v}")
        if "counter_day_pct" in params:
            v = float(params["counter_day_pct"])
            if v <= 0 or v > 10.0:
                errors.append(
                    f"counter_day_pct must be in (0, 10.0] percent (e.g. 1.0 = 1%), got {v}"
                )
        if "counter_day_decay" in params:
            v = float(params["counter_day_decay"])
            if v < 0.0 or v > 1.0:
                errors.append(f"counter_day_decay must be in [0, 1.0], got {v}")
        if "counter_day_atr" in params:
            errors.extend(validate_threshold_atr("counter_day_atr", params["counter_day_atr"]))
        return errors
