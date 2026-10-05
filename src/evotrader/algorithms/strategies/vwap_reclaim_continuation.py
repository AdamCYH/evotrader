"""VWAP reclaim continuation strategy.

Fires WITH the prevailing daily trend after an intraday pullback to the
opposite side of session VWAP has been RECLAIMED.

Complements ``intraday_vwap_zscore``, which fades deviations AWAY from VWAP.
This channel trades the RETURN: it waits for price to dip through VWAP against
an intact MA stack, confirms the dip stopped extending by requiring a reclaim,
and then votes in the direction of the stack.

WHY THIS EXISTS
~~~~~~~~~~~~~~~
On 2026-09-17 the composite was measurably a two-state machine. ``momentum``
and ``mean_reversion`` are both built from daily MAs, daily MACD and daily RSI,
so across six hourly cycles their combined numerator was 0.0247, 0.0298, 0.0313,
0.0307, 0.0312, 0.0311 — a near-constant +0.096 DC offset in a bull stack. All
of the composite's intraday information came from ONE channel,
``intraday_vwap_zscore``, and that channel is a pure fade. In a sustained trend
deviations from VWAP are persistently one-sided, so it fires almost exclusively
COUNTER to the trend: short three times on 09-17 as MSTR closed +4.49%, and long
on 09-16 as MSTR closed at the session low. 0-for-4 as composite author in
trending regimes, against 2-for-2 in ``range_bound``.

Meanwhile the account is a CASH account — it cannot short. So roughly half of
what the composite said was structurally untradeable, and the half that carried
information pointed the wrong way.

THE MISSED TRADE this channel is built for, dated: 2026-09-17 11:30, MSTR
$129.095 against VWAP $130.18, IBS 0.24 at session lows, bull stack intact. By
12:30 it was $131.225, back above VWAP — a completed dip-and-reclaim. It ran to
$132.65 and closed $131.85. ``intraday_vwap_zscore`` read +0.1143 ATR, inside
its band, silent. The orchestrator had literally armed the setup by name and the
algorithm had no channel capable of expressing it.

NOT A DUPLICATE OF ``swing_failure_reversal``: that is a 20-bar STRUCTURAL swing
reclaim needing ``min_stretch_atr`` 1.2 — $10.40 of intraday travel on MSTR, and
it has never fired live. This uses VWAP as the reference over a 0.10–0.90 ATR
admissible band, which is the intraday scale that actually occurs. SFR asks "did
a structural low hold"; this asks "did an intraday pullback to fair value hold".

CALIBRATION
~~~~~~~~~~~
``dip_scale`` 0.35 with ``tanh`` reaches 0.96 of full scale by ~0.72 ATR, and
``max_dip_atr`` gates at 0.90 ATR. The resolving range and the admissible range
are deliberately MATCHED — unlike ``intraday_vwap_zscore``, whose tanh saturates
at 0.315 ATR while its gate admits far deeper readings, leaving magnitude
non-discriminating over most of its live range. That is the z_scale saturation
lesson carried forward by construction rather than by later tuning.

Thresholds are in ATR units and therefore scale-free: they do not need
re-tuning between QQQ (ATR ~1% of price) and MSTR (~6.5%).

SHADOW MODE — this ships at weight 0.0 in all four regimes. This system has
repeatedly promoted channels on reasoning rather than emission evidence and then
found them 0-for-18 (``range_break_continuation``, ``trend_persistence``). A
channel that never fires is worse than no channel, because it silently occupies
weight. Promote only on a firing census: >= 8 firings over >= 15 sessions.
"""

from __future__ import annotations

import math
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm, warming_up
from evotrader.indicators.volume import select_relative_volume, validate_rvol_source
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class VwapReclaimContinuationStrategy(TradingAlgorithm):
    """Trend-aligned intraday VWAP pullback-and-reclaim entry."""

    def __init__(
        self,
        # 30 five-minute bars = 150 minutes, so a dip that ended 90 minutes
        # ago is still inside the search window.
        lookback_bars: int = 30,
        min_dip_bars: int = 2,
        min_dip_atr: float = 0.10,
        max_dip_atr: float = 0.90,
        # ── Freshness is WALL-CLOCK, not bars ────────────────────────
        # The bars are 5-minute candles but the system samples once an HOUR.
        # The original `max_bars_since_reclaim=6` was a 30-minute window and
        # `reclaim_decay=0.80` per bar was 0.33 at its edge, so the channel
        # could only see a reclaim that landed in the half-hour before a
        # cycle. Live 2026-09-18 10:34: bull stack, dip_bars 3, depth_atr
        # 0.417 (inside the band), reclaimed_now true, reclaim 45 minutes
        # old -> 0.0, reason=reclaim_too_old. Price never re-crossed VWAP,
        # so every later cycle read the same. The one with-trend intraday
        # channel saw the day's only pullback and threw it away on a clock
        # mismatch. 90 minutes = one sampling interval plus slack.
        max_minutes_since_reclaim: float = 90.0,
        dip_scale: float = 0.35,
        # Applied per 30-MINUTE unit of age, not per bar.
        reclaim_decay: float = 0.80,
        base_strength: float = 0.75,
        min_rvol: float = 0.50,
        rvol_scale: float = 1.0,
        require_current_session_anchor: bool = True,
        # Which relative-volume reading gates this channel — see
        # indicators.volume.select_relative_volume. 'daily' is every existing
        # version's behaviour.
        rvol_source: str = "daily",
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
        self._min_rvol = min_rvol
        self._rvol_scale = rvol_scale
        self._require_current_session_anchor = require_current_session_anchor
        self._rvol_source = rvol_source
        self._version = version

    @property
    def name(self) -> str:
        return "vwap_reclaim_continuation"

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return "Trend-aligned VWAP pullback reclaim continuation."

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

        # ── 0. ABSTAIN GUARDS (applicable=False, excluded from denominator) ──
        if not candles or len(candles) < self._min_dip_bars + 1:
            meta["applicable"] = False
            meta["reason"] = "insufficient_intraday_candles"
            meta["candle_count"] = len(candles) if candles else 0
            # On duty, only early: counted in the participation denominator
            # (see base.warming_up).
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

        # Same anchor discipline as intraday_vwap_zscore: a plumbing fault must
        # never masquerade as a market read.
        anchor = ind.vwap_anchor or "unknown"
        if self._require_current_session_anchor and anchor != "current_session":
            meta["applicable"] = False
            meta["reason"] = "anchor_unavailable" if anchor == "unknown" else "stale_vwap_anchor"
            meta["vwap_anchor"] = anchor
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        meta["applicable"] = True
        meta["vwap_anchor"] = anchor

        # ── 1. TREND DIRECTION from the daily MA stack ───────────────────────
        # Deliberately NOT read from snapshot.regime: the classifier has its own
        # accuracy caveats and this gate must not inherit them.
        ema_9, ema_21 = ind.ema_9, ind.ema_21
        sma_20, sma_50 = ind.sma_20, ind.sma_50
        bull_stack = price > ema_9 > ema_21 and sma_20 > sma_50
        bear_stack = price < ema_9 < ema_21 and sma_20 < sma_50
        meta["bull_stack"] = bull_stack
        meta["bear_stack"] = bear_stack

        if not bull_stack and not bear_stack:
            # Preconditions ABSENT, not a judgement on a candidate → off duty.
            meta["in_scope"] = False
            meta["out_of_scope_reason"] = "no_ma_stack"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        direction = 1.0 if bull_stack else -1.0

        # ── 2. LOCATE THE MOST RECENT DIP EPISODE ────────────────────────────
        window = candles[-self._lookback_bars :]
        closes = [c.close for c in window]
        n = len(closes)

        def _is_dip(close_value: float) -> bool:
            return close_value < vwap if bull_stack else close_value > vwap

        last_dip_idx = -1
        for i in range(n - 1, -1, -1):
            if _is_dip(closes[i]):
                last_dip_idx = i
                break

        if last_dip_idx < 0:
            # Price never crossed VWAP in the window → no pullback to judge.
            meta["in_scope"] = False
            meta["out_of_scope_reason"] = "no_vwap_cross_in_window"
            meta["lookback_bars_seen"] = n
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        start_idx = last_dip_idx
        while start_idx - 1 >= 0 and _is_dip(closes[start_idx - 1]):
            start_idx -= 1

        episode = closes[start_idx : last_dip_idx + 1]
        dip_bars = len(episode)
        extreme = min(episode) if bull_stack else max(episode)
        depth_atr = abs(vwap - extreme) / atr
        bars_since_reclaim = (n - 1) - last_dip_idx
        meta["dip_bars"] = dip_bars
        meta["depth_atr"] = round(depth_atr, 4)
        meta["bars_since_reclaim"] = bars_since_reclaim

        # Age of the reclaim in WALL-CLOCK minutes, from the first bar that
        # closed back on the trend side of VWAP. Falls back to bars x 5 only
        # when timestamps are absent, and says so.
        age_min: float | None = None
        age_source = "bars_assumed_5min"
        reclaim_bar = window[last_dip_idx + 1] if last_dip_idx + 1 < n else None
        if reclaim_bar is None:
            # No bar has closed back on the trend side yet, so there is no age
            # to measure. This used to read 'bars_assumed_5min' — the label for
            # a MISSING TIMESTAMP — on every not-reclaimed cycle, which made a
            # healthy clock look broken in the census.
            age_source = "no_reclaim_bar"
        reclaim_ts = getattr(reclaim_bar, "timestamp", None) if reclaim_bar else None
        now_ts = snapshot.timestamp
        if reclaim_ts is not None and now_ts is not None:
            try:
                if reclaim_ts.tzinfo is None and now_ts.tzinfo is not None:
                    reclaim_ts = reclaim_ts.replace(tzinfo=now_ts.tzinfo)
                elif now_ts.tzinfo is None and reclaim_ts.tzinfo is not None:
                    now_ts = now_ts.replace(tzinfo=reclaim_ts.tzinfo)
                delta = (now_ts - reclaim_ts).total_seconds() / 60.0
                if delta >= 0:
                    age_min = delta
                    age_source = "candle_timestamps"
            except Exception:
                age_min = None
        if age_min is None and reclaim_bar is not None:
            age_min = float(bars_since_reclaim) * 5.0
        meta["age_min"] = round(age_min, 2) if age_min is not None else None
        meta["age_source"] = age_source

        # ── 3. GATES ─────────────────────────────────────────────────────────
        # These are GENUINE abstentions: a candidate dip existed and was
        # evaluated, so they stay in the renormalization denominator.
        reclaimed_now = (price > vwap) if bull_stack else (price < vwap)
        meta["reclaimed_now"] = reclaimed_now
        if not reclaimed_now:
            meta["reason"] = "not_reclaimed"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if bars_since_reclaim < 1:
            meta["reason"] = "no_reclaim_yet"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if age_min is not None and age_min > self._max_minutes_since_reclaim:
            meta["reason"] = "reclaim_too_old"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if dip_bars < self._min_dip_bars:
            meta["reason"] = "dip_too_brief"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if depth_atr < self._min_dip_atr:
            meta["reason"] = "dip_too_shallow"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)
        if depth_atr > self._max_dip_atr:
            # Beyond this a pullback is a structural break, not a dip.
            meta["reason"] = "dip_too_deep_trend_break"
            return AlgoSignal(name=self.name, value=0.0, weight=1.0, metadata=meta)

        # ── 4. STRENGTH ──────────────────────────────────────────────────────
        depth_term = math.tanh(depth_atr / self._dip_scale)
        # Decay per 30 minutes of age, so the shape survives any sampling cadence.
        freshness = self._reclaim_decay ** (age_min / 30.0)

        rvol, rvol_source = select_relative_volume(ind, self._rvol_source)
        meta["rvol_source"] = rvol_source
        if rvol is None:
            vol_term = 1.0
            meta["rvol_available"] = False
        else:
            meta["rvol_available"] = True
            meta["rvol"] = round(rvol, 4)
            vol_term = min(1.0, max(self._min_rvol, rvol / self._rvol_scale))
        meta["vol_term"] = round(vol_term, 4)
        # Emitted so the floor's influence is COUNTABLE, not inferred — the
        # lesson from momentum's volume_dampener_floor, which binds on
        # essentially every cycle and has left its own curve decorative.
        meta["vol_floor_binding"] = rvol is not None and (rvol / self._rvol_scale) < self._min_rvol

        signal = direction * self._base_strength * depth_term * freshness * vol_term
        signal = max(-1.0, min(1.0, signal))
        meta["depth_term"] = round(depth_term, 4)
        meta["freshness"] = round(freshness, 4)
        meta["fired"] = True
        meta["reason"] = "reclaim_confirmed"
        return AlgoSignal(name=self.name, value=signal, weight=1.0, metadata=meta)

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
            "min_rvol": self._min_rvol,
            "rvol_scale": self._rvol_scale,
            "require_current_session_anchor": self._require_current_session_anchor,
            "rvol_source": self._rvol_source,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        int_keys = ("lookback_bars", "min_dip_bars")
        float_keys = (
            "min_dip_atr",
            "max_dip_atr",
            "max_minutes_since_reclaim",
            "dip_scale",
            "reclaim_decay",
            "base_strength",
            "min_rvol",
            "rvol_scale",
        )
        # Legacy key from v027's first draft, which rescaled the clock in BARS
        # before the wall-clock fix landed. Accepted and converted (x5 min) so
        # an old config does not silently fall back to the default, but the
        # new key wins if both are present.
        if "max_bars_since_reclaim" in params and "max_minutes_since_reclaim" not in params:
            self._max_minutes_since_reclaim = float(params["max_bars_since_reclaim"]) * 5.0
        for key in int_keys:
            if key in params:
                setattr(self, f"_{key}", int(params[key]))
        for key in float_keys:
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
        _check("min_rvol", 0.0, 1.0)
        _check("rvol_scale", 0.0, 100.0, low_open=True)

        # Cross-check: an inverted band admits nothing and would read as a
        # silent channel rather than a misconfigured one.
        lo = params.get("min_dip_atr", self._min_dip_atr)
        hi = params.get("max_dip_atr", self._max_dip_atr)
        try:
            if float(lo) >= float(hi):
                errors.append(
                    f"min_dip_atr ({lo}) must be < max_dip_atr ({hi}); an "
                    f"inverted band can never fire."
                )
        except (TypeError, ValueError):
            pass
        if "rvol_source" in params:
            err = validate_rvol_source(params["rvol_source"])
            if err:
                errors.append(err)
        return errors
