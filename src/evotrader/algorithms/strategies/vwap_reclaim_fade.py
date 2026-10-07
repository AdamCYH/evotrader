"""VWAP reclaim fade strategy.

Votes AGAINST an intraday reclaim of session VWAP that happened on light
volume: bearish after a low-volume reclaim in a bull stack, bullish after one
in a bear stack. The mirror image of ``vwap_reclaim_continuation``.

WHY THIS EXISTS
~~~~~~~~~~~~~~~
A dip through VWAP against the daily trend, followed by a reclaim, can be read
two ways. Buyers (in a bull stack) are back and the trend resumes: that is the
continuation channel's reading. Or the bounce simply ran out of sellers at fair
value, with no new demand behind it, and it fails. What tells the two apart,
on this channel's thesis, is participation: a reclaim on below-normal volume
for the time of day is the second kind. It is a hypothesis. It runs in shadow
so that the firing record decides which reading, if either, holds on an
instrument, instead of reasoning.

ONE DETECTOR, TWO READINGS
~~~~~~~~~~~~~~~~~~~~~~~~~~
Both channels call ``locate_vwap_dip_episode``, so they can never disagree
about WHETHER a reclaim happened, only about what it means. They are mutually
exclusive by construction: at most one of them can be promoted.

NOT A SIGN FLIP of the continuation channel. Inverting that channel would erase
its own record, and would turn its volume term the wrong way: continuation
strengthens with volume, while this thesis is specifically about LOW volume.

STRENGTH
~~~~~~~~
    value = -trend x base_strength x tanh(depth / dip_scale)
            x reclaim_decay ** (age_min / 30) x vol_fade_term

The depth and freshness terms are the continuation channel's, with the
opposite sign. ``vol_fade_term`` is 0 at ``max_rvol_for_fade`` (1.0: a reclaim
on normal volume, where a real bid showed up, so it abstains), 1 at
``full_fade_rvol`` (0.6) and below, and linear between.

Worked example, made-up numbers: bull stack, a 12-bar dip 0.30 ATR deep,
reclaimed 15 minutes ago on relative volume 0.80. depth_term tanh(0.857) =
0.695, freshness 0.8 ** 0.5 = 0.894, vol_fade_term (1.0 - 0.8) / 0.4 = 0.5,
so value = -0.6 x 0.695 x 0.894 x 0.5 = -0.186.

The metadata carries every gate outcome plus ``dip_bars``, ``depth_atr``,
``age_min``, ``rvol`` and ``session_minute``, so the record can be split by
dip length, depth, time of day and volume band.

SHADOW MODE: this ships at weight 0.0 in every regime. It runs and its votes
are recorded, but it does not move the composite until promoted on its own
firing record (one firing per day, scored to the session close).
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm, warming_up
from evotrader.indicators.volume import select_relative_volume, validate_rvol_source
from evotrader.indicators.vwap_episode import locate_vwap_dip_episode
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal
from evotrader.tools.market_hours import minutes_since_open


class VwapReclaimFadeStrategy(TradingAlgorithm):
    """Fade a low-volume VWAP reclaim against the daily MA stack."""

    def __init__(
        self,
        lookback_bars: int = 30,
        min_dip_bars: int = 2,
        min_dip_atr: float = 0.10,
        max_dip_atr: float = 0.90,
        max_minutes_since_reclaim: float = 90.0,
        dip_scale: float = 0.35,
        # Applied per 30-MINUTE unit of age, as in the continuation channel.
        reclaim_decay: float = 0.80,
        base_strength: float = 0.6,
        # At or above this relative volume the reclaim had participation:
        # abstain. At or below full_fade_rvol: full strength.
        max_rvol_for_fade: float = 1.0,
        full_fade_rvol: float = 0.6,
        require_current_session_anchor: bool = True,
        # Today's time-of-day-matched ratio when the profile is warm, the
        # prior session's daily ratio (labelled *_fallback) until then. See
        # indicators.volume.select_relative_volume.
        rvol_source: str = "session",
        version: str = "v001",
    ) -> None:
        self._lookback_bars = lookback_bars
        self._min_dip_bars = min_dip_bars
        self._min_dip_atr = min_dip_atr
        self._max_dip_atr = max_dip_atr
        self._max_minutes_since_reclaim = max_minutes_since_reclaim
        self._dip_scale = dip_scale
        self._reclaim_decay = reclaim_decay
        self._base_strength = base_strength
        self._max_rvol_for_fade = max_rvol_for_fade
        self._full_fade_rvol = full_fade_rvol
        self._require_current_session_anchor = require_current_session_anchor
        self._rvol_source = rvol_source
        self._version = version

    @property
    def name(self) -> str:
        return "vwap_reclaim_fade"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return "Fade a low-volume VWAP reclaim against the daily trend."

    @property
    def resolution(self) -> str:
        """Intraday: reads session VWAP and intraday candles every cycle."""
        return "intraday"

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        ind = snapshot.indicators
        meta: dict[str, Any] = {}
        candles = snapshot.recent_candles
        vwap = ind.vwap
        atr = ind.atr_14
        price = snapshot.quote.last

        # ── 0. ABSTAIN GUARDS: the continuation channel's, reason for reason ──
        if not candles or len(candles) < self._min_dip_bars + 1:
            meta["applicable"] = False
            meta["reason"] = "insufficient_intraday_candles"
            meta["candle_count"] = len(candles) if candles else 0
            if (
                atr is not None
                and atr > 0
                and ind.vwap_anchor == "current_session"
                and warming_up(candles or [], self._min_dip_bars + 1)
            ):
                meta["warming_up"] = True
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if vwap is None or atr is None or atr <= 0:
            meta["applicable"] = False
            meta["reason"] = "missing_vwap_or_atr"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if any(v is None for v in (ind.ema_9, ind.ema_21, ind.sma_20, ind.sma_50)):
            meta["applicable"] = False
            meta["reason"] = "missing_mas"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        anchor = ind.vwap_anchor or "unknown"
        if self._require_current_session_anchor and anchor != "current_session":
            meta["applicable"] = False
            meta["reason"] = "anchor_unavailable" if anchor == "unknown" else "stale_vwap_anchor"
            meta["vwap_anchor"] = anchor
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        meta["applicable"] = True
        meta["vwap_anchor"] = anchor

        # ── 1. TREND from the daily MA stack (not snapshot.regime) ───────────
        bull_stack = price > ind.ema_9 > ind.ema_21 and ind.sma_20 > ind.sma_50
        bear_stack = price < ind.ema_9 < ind.ema_21 and ind.sma_20 < ind.sma_50
        meta["bull_stack"] = bull_stack
        meta["bear_stack"] = bear_stack
        if not bull_stack and not bear_stack:
            meta["in_scope"] = False
            meta["out_of_scope_reason"] = "no_ma_stack"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        trend = 1.0 if bull_stack else -1.0

        # ── 2. THE EPISODE: the shared detector ──────────────────────────────
        episode = locate_vwap_dip_episode(
            candles,
            vwap=vwap,
            atr=atr,
            bull_stack=bull_stack,
            price=price,
            lookback_bars=self._lookback_bars,
            now=snapshot.timestamp,
        )
        if episode.no_cross:
            meta["in_scope"] = False
            meta["out_of_scope_reason"] = "no_vwap_cross_in_window"
            meta["lookback_bars_seen"] = episode.lookback_bars_seen
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        meta.update(episode.as_meta())
        meta["reclaimed_now"] = episode.reclaimed_now

        # ── 3. GATES: genuine abstentions, the continuation channel's ────────
        age_min = episode.age_min
        for failed, reason in (
            (not episode.reclaimed_now, "not_reclaimed"),
            (episode.bars_since_reclaim < 1, "no_reclaim_yet"),
            (age_min is not None and age_min > self._max_minutes_since_reclaim, "reclaim_too_old"),
            (episode.dip_bars < self._min_dip_bars, "dip_too_brief"),
            (episode.depth_atr < self._min_dip_atr, "dip_too_shallow"),
            (episode.depth_atr > self._max_dip_atr, "dip_too_deep_trend_break"),
        ):
            if failed:
                meta["reason"] = reason
                return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        # ── 4. VOLUME: the thesis is a reclaim WITHOUT participation ─────────
        rvol, rvol_source = select_relative_volume(ind, self._rvol_source)
        meta["rvol_source"] = rvol_source
        if rvol is None:
            # No reading at all is a data gap, not a market read.
            meta["applicable"] = False
            meta["reason"] = "rvol_unavailable"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        meta["rvol"] = round(rvol, 4)
        if rvol >= self._max_rvol_for_fade:
            meta["vol_fade_term"] = 0.0
            meta["reason"] = "reclaim_on_volume"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        span = max(1e-9, self._max_rvol_for_fade - self._full_fade_rvol)
        vol_fade_term = min(1.0, max(0.0, (self._max_rvol_for_fade - rvol) / span))

        # ── 5. STRENGTH: continuation's depth and freshness, opposite sign ──
        depth_term = math.tanh(episode.depth_atr / self._dip_scale)
        freshness = self._reclaim_decay ** ((age_min or 0.0) / 30.0)
        value = -trend * self._base_strength * depth_term * freshness * vol_fade_term
        value = max(-1.0, min(1.0, value))

        meta["depth_term"] = round(depth_term, 4)
        meta["freshness"] = round(freshness, 4)
        meta["vol_fade_term"] = round(vol_fade_term, 4)
        minute = minutes_since_open(snapshot.timestamp) if snapshot.timestamp else None
        meta["session_minute"] = round(minute, 1) if minute is not None else None
        meta["leg"] = "fade_bull_reclaim" if bull_stack else "fade_bear_reclaim"
        meta["fired"] = True
        meta["reason"] = "reclaim_faded"
        return AlgoSignal(name=self.name, value=value, weight=1.0, metadata=meta)

    def get_parameters(self) -> dict[str, Any]:
        return {
            "lookback_bars": self._lookback_bars,
            "min_dip_bars": self._min_dip_bars,
            "min_dip_atr": self._min_dip_atr,
            "max_dip_atr": self._max_dip_atr,
            "max_minutes_since_reclaim": self._max_minutes_since_reclaim,
            "dip_scale": self._dip_scale,
            "reclaim_decay": self._reclaim_decay,
            "base_strength": self._base_strength,
            "max_rvol_for_fade": self._max_rvol_for_fade,
            "full_fade_rvol": self._full_fade_rvol,
            "require_current_session_anchor": self._require_current_session_anchor,
            "rvol_source": self._rvol_source,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key in ("lookback_bars", "min_dip_bars"):
            if key in params:
                setattr(self, f"_{key}", int(params[key]))
        for key in (
            "min_dip_atr",
            "max_dip_atr",
            "max_minutes_since_reclaim",
            "dip_scale",
            "reclaim_decay",
            "base_strength",
            "max_rvol_for_fade",
            "full_fade_rvol",
        ):
            if key in params:
                setattr(self, f"_{key}", float(params[key]))
        if "require_current_session_anchor" in params:
            self._require_current_session_anchor = bool(params["require_current_session_anchor"])
        if "rvol_source" in params:
            self._rvol_source = str(params["rvol_source"])

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

        _check("lookback_bars", 4, 78)
        _check("min_dip_bars", 1, 20)
        _check("min_dip_atr", 0.0, 3.0, low_open=True)
        _check("max_dip_atr", 0.0, 5.0, low_open=True)
        _check("max_minutes_since_reclaim", 5.0, 390.0)
        _check("dip_scale", 0.0, 10.0, low_open=True)
        _check("reclaim_decay", 0.0, 1.0, low_open=True)
        _check("base_strength", 0.0, 1.0, low_open=True)
        _check("max_rvol_for_fade", 0.0, 10.0, low_open=True)
        _check("full_fade_rvol", 0.0, 10.0)

        # Inverted bands admit nothing and would read as a silent channel
        # rather than a misconfigured one.
        for lo_key, hi_key in (
            ("min_dip_atr", "max_dip_atr"),
            ("full_fade_rvol", "max_rvol_for_fade"),
        ):
            lo = params.get(lo_key, getattr(self, f"_{lo_key}"))
            hi = params.get(hi_key, getattr(self, f"_{hi_key}"))
            try:
                if float(lo) >= float(hi):
                    errors.append(f"{lo_key} ({lo}) must be < {hi_key} ({hi}).")
            except (TypeError, ValueError):
                pass
        if "rvol_source" in params:
            err = validate_rvol_source(params["rvol_source"])
            if err:
                errors.append(err)
        return errors
