"""Intraday VWAP Z-Score Reversion strategy.

Computes a rolling z-score of ATR-normalised price deviation from session
VWAP using ``recent_candles``.  Signal strength fades VWAP overextensions
while they are *fresh* — decaying exponentially as the deviation persists
without extending further — eliminating the "stale daily oscillator fires
the identical signal 6+ times per session" defect.

Empirical basis: the two verified winning trades were both VWAP-
overextension fades in range_bound.  This strategy captures that edge
natively at intraday granularity. **Both winners are entirely from
range_bound. There is no trending-regime record to point at.**

TREND-REGIME ASYMMETRY — read before trusting the trending weights
-----------------------------------------------------------------
This is a FADE. In a sustained trend, deviations from VWAP are persistently
one-sided, so the channel fires almost exclusively COUNTER to the trend.
Measured: short 3x on 2026-09-17 as MSTR closed +4.49%, and long once on
09-16 as MSTR closed at the session low — **0-for-4 as composite author in
trending regimes**, against 2-for-2 in range_bound, the regime this docstring
names as its home.

The previous version of this paragraph claimed a "small allocation in trending
regimes for trend-pullback entries". **No trend-pullback path was ever
implemented.** Anyone auditing the 0.18 trending_bull weight would have
concluded a with-trend mode justified it. None existed, and that is how this
channel kept a heavy trending weight through four evolution sessions
unquestioned.

Two parameters now make the claim true, both IDENTITY NO-OPS at 1.0:

* ``countertrend_dampener`` (0.5) scales the emission down when the firing leg
  opposes an intact MA stack.
* ``with_trend_trigger_scale`` (0.7) lowers the trigger on the with-trend leg,
  which was effectively unreachable: the trigger is a symmetric 0.165 ATR, but
  dips below VWAP in an uptrend are shallow and bought quickly. On 09-17 at
  11:30 a genuine pullback measured 0.1277 ATR and emitted nothing, missing by
  0.037 ATR.

Neither engages without a stack, so ``range_bound`` behaviour is bit-identical.

Best in: Range-Bound regime. In trending regimes it now votes with the stack
more readily than against it, but its trending record remains unproven.
"""

from __future__ import annotations

import math
from statistics import stdev
from typing import Any

from evotrader.algorithms.base import TradingAlgorithm
from evotrader.indicators.volume import select_relative_volume, validate_rvol_source
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


class IntradayVwapZscoreStrategy(TradingAlgorithm):
    """Intraday VWAP z-score reversion with built-in staleness decay.

    Unlike the daily mean-reversion indicators (RSI, Bollinger, IBS) which
    are re-read every hourly cycle but only update once per day, this signal
    is computed directly from ``recent_candles`` and therefore reflects the
    *current* intraday state.

    Key features:
    - ATR-normalised deviation from session VWAP → comparable across tickers
    - Z-score over a rolling window → only trades *statistically* extreme moves
    - Staleness decay (``decay^n``) → a brand-new extreme is tradeable, one
      that has persisted for hours is stale and should be ignored
    - Volume confirmation → exhaustion fades work best on fading participation,
      dampened when relative volume is unusually high

    CALIBRATION WARNING — ``entry_z`` and ``min_std`` are NOT independent.

    On QQQ the intraday dispersion of ATR-normalised deviations (``sd_raw``
    ~0.05) sits well below ``min_std`` (0.15), so the floor binds on essentially
    every cycle and ``sd`` is effectively a constant. ``z`` then reduces to
    ``deviation / min_std``, and the firing condition ``|z| >= entry_z``
    becomes::

        |deviation_atr| >= entry_z * min_std        # == trigger_atr

    That is a fixed ATR-DISTANCE trigger, not a statistical-rarity test. While
    the floor binds only the PRODUCT of the two parameters is identifiable, so
    tuning them independently is meaningless — and the recorded tuning history
    of ``entry_z`` (1.5 → 1.9 → 1.1) was implicitly re-tuning an ATR distance,
    which is why those changes never behaved like z-score changes.

    At ``entry_z=1.1, min_std=0.15`` the trigger is **0.165 ATR** (~$1.54 on QQQ
    at $716 with ATR $9.32 — roughly one sixth of an ATR).

    ``tanh`` also saturates: with ``z_scale=0.5`` the emitted magnitude reaches
    ~0.96 of full scale by **0.315 ATR**, so above that the channel cannot
    distinguish a modest dislocation from an extreme one (0.20 ATR → 0.44,
    0.25 → 0.81, 0.29 → 0.93, 0.315 → 0.96).

    ``trigger_atr`` and ``saturation_atr`` are emitted in metadata every cycle
    so the effective calibration is observable rather than inferred.

    Do NOT raise the trigger to "filter noise" without outcome data: the two
    firings on record won (+20.7%, +14.9%) and both sat at modest deviations
    (0.20 and 0.29 ATR), so a higher bar would have excluded them.
    """

    def __init__(
        self,
        zscore_window: int = 20,
        entry_z: float = 1.5,
        z_scale: float = 1.0,
        staleness_decay: float = 0.65,
        max_stale_bars: int = 6,
        stale_threshold_z: float | None = None,
        min_std: float = 0.15,
        max_rvol_for_fade: float = 1.5,
        high_vol_dampener: float = 0.4,
        # CODE DEFAULTS ARE IDENTITY, DELIBERATELY. The live values (0.5 / 0.7)
        # live in the versioned config, not here. A non-identity default would
        # apply to EVERY historical version — v023's recorded backtest would
        # silently stop describing what v023 does — and `registry.rollback()`
        # only rewrites active.yaml, so it could never undo it.
        countertrend_dampener: float = 1.0,
        with_trend_trigger_scale: float = 1.0,
        # Which relative-volume reading gates this channel — see
        # indicators.volume.select_relative_volume. 'daily' is every existing
        # version's behaviour.
        rvol_source: str = "daily",
        version: str = "v001",
    ) -> None:
        self._zscore_window = zscore_window
        self._entry_z = entry_z
        self._z_scale = z_scale
        self._staleness_decay = staleness_decay
        # Bound the exponent. An unbounded exponent over a 20-bar window
        # makes freshness collapse to ~0 (0.65**20 = 0.00018), which does
        # not de-rate a stale extreme, it deletes it.
        self._max_stale_bars = max_stale_bars
        # Decouple the persistence threshold from the firing gate so that
        # lowering entry_z for reachability does not simultaneously inflate
        # the staleness count and suppress every firing.
        self._stale_threshold_z = stale_threshold_z if stale_threshold_z is not None else entry_z
        self._min_std = min_std
        self._max_rvol_for_fade = max_rvol_for_fade
        self._high_vol_dampener = high_vol_dampener
        # Both are identity no-ops at 1.0 — the audit and rollback path.
        self._countertrend_dampener = countertrend_dampener
        self._with_trend_trigger_scale = with_trend_trigger_scale
        self._rvol_source = rvol_source
        self._version = version

    @property
    def name(self) -> str:
        return "intraday_vwap_zscore"

    @property
    def version(self) -> str:
        return self._version

    @property
    def resolution(self) -> str:
        """Intraday: reads session VWAP and the current bar every cycle."""
        return "intraday"

    @property
    def description(self) -> str:
        return (
            f"Intraday VWAP z-score reversion "
            f"(window={self._zscore_window}, entry_z={self._entry_z}, "
            f"staleness_decay={self._staleness_decay})"
        )

    def compute_signal(self, snapshot: MarketSnapshot) -> AlgoSignal:
        """Compute VWAP z-score reversion signal from intraday candles.

        Signal logic:
        1. ATR-normalised deviation from session VWAP
        2. Z-score vs. rolling window of recent deviations
        3. Staleness: decay^(number of trailing bars beyond entry threshold)
        4. Volume gating: dampen when relative volume is too high
        5. Fade the extension: positive z → sell, negative z → buy
        """
        candles = snapshot.recent_candles
        vwap = snapshot.indicators.vwap
        atr = snapshot.indicators.atr_14
        price = snapshot.quote.last

        # ── Guard: abstain if intraday context is insufficient ──
        if not candles or vwap is None or atr is None or atr <= 0:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "reason": "insufficient_intraday_data",
                },
            )

        # 1. ATR-normalised deviation from session VWAP
        #    e.g. +1.8 means price is 1.8 ATRs above VWAP
        deviation = (price - vwap) / atr

        # 2. Z-score vs rolling window of recent deviations
        window = candles[-self._zscore_window :]
        recent_devs = [(c.close - vwap) / atr for c in window]

        sd_raw = stdev(recent_devs) if len(recent_devs) > 2 else 1.0
        sd = max(sd_raw, self._min_std)  # Floor to prevent division-by-tiny-number

        # DIAGNOSTIC: on QQQ the intraday dispersion of ATR-normalised
        # deviations is consistently below min_std, so this floor binds on
        # essentially every cycle and z degenerates to deviation/min_std —
        # a fixed rescale, NOT an adaptive statistical z-score. Surface it
        # so agents do not over-trust 'within_band' as statistical evidence.
        sd_floored = sd_raw < self._min_std

        # Effective calibration in the PHYSICAL unit (ATR of VWAP deviation),
        # so the degeneracy above is observable per-cycle instead of inferred.
        # While the floor binds these are exact and constant; when it does not,
        # they move with sd_raw and `sd_floored=False` marks that.
        # ── TREND ALIGNMENT ──────────────────────────────────────────────
        # Read the stack from indicators DIRECTLY, never from snapshot.regime:
        # the classifier has its own accuracy caveats and this gate must not
        # inherit them.
        _i = snapshot.indicators
        _mas = (_i.ema_9, _i.ema_21, _i.sma_20, _i.sma_50)
        if any(v is None for v in _mas):
            stack_context = "no_stack"
        elif price > _i.ema_9 > _i.ema_21 and _i.sma_20 > _i.sma_50:
            stack_context = "bull"
        elif price < _i.ema_9 < _i.ema_21 and _i.sma_20 < _i.sma_50:
            stack_context = "bear"
        else:
            stack_context = "no_stack"

        # ⚠ SIGN CONVENTION — READ BEFORE EDITING ⚠
        # This channel FADES: it emits OPPOSITE the deviation. Price ABOVE VWAP
        # gives z > 0 and emits a SELL. Therefore, in a BULL stack:
        #
        #     z > 0  (stretched above VWAP, emits SELL)  -> COUNTER-trend
        #     z < 0  (a dip being bought, emits BUY)     -> WITH-trend
        #
        # Getting this backwards silently inverts both knobs below and would
        # amplify exactly the leg we are damping. Mirrored for a bear stack.
        _pre_z = deviation / sd
        if stack_context == "bull":
            leg = "with_trend" if _pre_z < 0 else "counter_trend"
        elif stack_context == "bear":
            leg = "with_trend" if _pre_z > 0 else "counter_trend"
        else:
            # No stack: the concept does not apply, and neither knob engages.
            # This is what keeps range_bound bit-identical.
            leg = "no_stack"

        # The with-trend leg was effectively UNREACHABLE. The trigger is a
        # symmetric 0.165 ATR in both directions, but in an uptrend dips below
        # VWAP are shallow and bought quickly. On 2026-09-17 at 11:30 a genuine
        # pullback measured -0.1277 ATR and emitted nothing — missing by 0.037
        # ATR. The channel was loud in the direction that is wrong and
        # untradeable on a cash account, and silent in the direction that is
        # right and tradeable. Scaling the trigger down on the actionable leg
        # RAISES trade frequency; it is not a threshold tightening.
        effective_entry_z = (
            self._entry_z * self._with_trend_trigger_scale if leg == "with_trend" else self._entry_z
        )

        # Calibration in the PHYSICAL unit (ATR of VWAP deviation), derived from
        # the EFFECTIVE trigger so it describes the bar actually applied.
        trigger_atr = effective_entry_z * sd
        # Deviation at which tanh reaches ~0.96 of full scale (2 z_scale units
        # past the trigger). Beyond it, magnitude carries little information.
        saturation_atr = (effective_entry_z + 2.0 * self._z_scale) * sd

        # Standardise the raw deviation by dispersion ONLY.  Subtracting the
        # window mean (mu) caused a persistent dislocation to cancel itself:
        # if price sat 1.5 ATR above VWAP for the whole window, mu → 1.5 and
        # z → 0, blinding the channel to exactly the sustained overextension
        # it exists to fade.  Staleness decay below already de-rates stale
        # extremes, so mu-subtraction was double-counting that correction.
        z = deviation / sd

        # 3. Staleness: how many trailing bars already exceeded stale_threshold_z?
        #    A brand-new extreme is tradeable; one persisting for hours is stale.
        stale_bars = self._count_trailing_bars_beyond_threshold(recent_devs, sd)
        freshness = self._staleness_decay**stale_bars

        # 4. Volume confirmation
        rvol_raw, rvol_source = select_relative_volume(snapshot.indicators, self._rvol_source)
        rvol = rvol_raw or 1.0
        vol_factor = 1.0 if rvol <= self._max_rvol_for_fade else self._high_vol_dampener

        vwap_anchor = snapshot.indicators.vwap_anchor or "unknown"

        # Anchor discipline is a GATE, not a post-hoc mutation. Evaluate it
        # BEFORE computing a value so a plumbing fault can never masquerade
        # as a genuine 'within_band' read. 'unknown' means the field never
        # propagated (compute_session_vwap only ever returns 'current_session'
        # or 'prior_session'), which is a defect, not a market condition.
        if vwap_anchor != "current_session":
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": False,
                    "reason": (
                        "anchor_unavailable" if vwap_anchor == "unknown" else "stale_vwap_anchor"
                    ),
                    "vwap_anchor": vwap_anchor,
                    "z": round(z, 4),
                    "deviation_atr": round(deviation, 4),
                },
            )

        # If z-score is within the entry band → no signal (within normal range)
        if abs(z) < effective_entry_z:
            return AlgoSignal(
                name=self.name,
                value=0.0,
                weight=1.0,
                metadata={
                    "applicable": True,
                    "z": round(z, 4),
                    "sd_raw": round(sd_raw, 5),
                    "sd_floored": sd_floored,
                    "trigger_atr": round(trigger_atr, 4),
                    "saturation_atr": round(saturation_atr, 4),
                    "stack_context": stack_context,
                    "leg": leg,
                    "effective_entry_z": round(effective_entry_z, 4),
                    "deviation_atr": round(deviation, 4),
                    "freshness": round(freshness, 4),
                    "reason": "within_band",
                    "vwap_anchor": vwap_anchor,
                },
            )

        # 5. Fade the extension:
        #    Positive z (stretched above VWAP) → negative (sell) signal
        #    Negative z (stretched below VWAP) → positive (buy) signal
        sign_z = 1.0 if z > 0 else -1.0
        raw = -math.tanh((abs(z) - effective_entry_z) / self._z_scale) * sign_z
        # Damp the counter-trend leg. At weight 0.18 in both trending regimes
        # this channel fired short 3x on 2026-09-17 (MSTR closed +4.49%) and
        # long once on 09-16 (MSTR closed at the session low) — 0-for-4 as
        # composite author in trending regimes, against 2-for-2 in range_bound.
        # Identity no-op at 1.0, and never engages without a stack.
        trend_factor = self._countertrend_dampener if leg == "counter_trend" else 1.0
        value = max(-1.0, min(1.0, raw * freshness * vol_factor * trend_factor))

        return AlgoSignal(
            name=self.name,
            value=value,
            weight=1.0,
            metadata={
                "applicable": True,
                "z": round(z, 4),
                "sd_raw": round(sd_raw, 5),
                "sd_floored": sd_floored,
                "trigger_atr": round(trigger_atr, 4),
                "saturation_atr": round(saturation_atr, 4),
                "stack_context": stack_context,
                "leg": leg,
                "effective_entry_z": round(effective_entry_z, 4),
                "trend_factor": round(trend_factor, 4),
                "countertrend_dampener_applied": leg == "counter_trend",
                "deviation_atr": round(deviation, 4),
                "freshness": round(freshness, 4),
                "stale_bars": stale_bars,
                "rvol": round(rvol, 4),
                "rvol_source": rvol_source,
                "vol_factor": round(vol_factor, 4),
                "raw_signal": round(raw, 4),
                "vwap_anchor": vwap_anchor,
            },
        )

    def _count_trailing_bars_beyond_threshold(
        self,
        recent_devs: list[float],
        sd: float,
    ) -> int:
        """Count consecutive trailing bars where |z| >= stale_threshold_z.

        Scans from the end of ``recent_devs[:-1]`` backwards and counts how many
        consecutive bars had a z-score beyond the staleness threshold. This
        measures how "stale" the current extreme is.
        """
        count = 0
        # Exclude the CURRENT bar (last element): staleness measures how
        # long the extreme existed BEFORE now. Including it means a
        # brand-new extreme can never score freshness 1.0, contradicting
        # this class's own docstring.
        for dev in reversed(recent_devs[:-1]):
            z_bar = dev / max(sd, self._min_std)
            if abs(z_bar) >= self._stale_threshold_z:
                count += 1
            else:
                break
        # Bound the exponent so decay remains a de-rating, not an erasure.
        return min(count, self._max_stale_bars)

    def get_parameters(self) -> dict[str, Any]:
        return {
            "zscore_window": self._zscore_window,
            "entry_z": self._entry_z,
            "z_scale": self._z_scale,
            "staleness_decay": self._staleness_decay,
            "max_stale_bars": self._max_stale_bars,
            "stale_threshold_z": self._stale_threshold_z,
            "min_std": self._min_std,
            "max_rvol_for_fade": self._max_rvol_for_fade,
            "high_vol_dampener": self._high_vol_dampener,
            "countertrend_dampener": self._countertrend_dampener,
            "with_trend_trigger_scale": self._with_trend_trigger_scale,
            "rvol_source": self._rvol_source,
        }

    def set_parameters(self, params: dict[str, Any]) -> None:
        for key in (
            "zscore_window",
            "entry_z",
            "z_scale",
            "staleness_decay",
            "max_stale_bars",
            "stale_threshold_z",
            "min_std",
            "max_rvol_for_fade",
            "high_vol_dampener",
            "countertrend_dampener",
            "with_trend_trigger_scale",
            "rvol_source",
        ):
            if key in params:
                setattr(self, f"_{key}", params[key])

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if "zscore_window" in params:
            w = int(params["zscore_window"])
            if w < 3:
                errors.append(f"zscore_window must be >= 3 for meaningful statistics, got {w}")
        if "entry_z" in params:
            ez = float(params["entry_z"])
            if ez <= 0:
                errors.append(f"entry_z must be positive, got {ez}")
        if "staleness_decay" in params:
            sd = float(params["staleness_decay"])
            if not (0.0 < sd < 1.0):
                errors.append(f"staleness_decay must be in (0, 1), got {sd}")
        if "max_stale_bars" in params:
            msb = int(params["max_stale_bars"])
            if msb < 0:
                errors.append(f"max_stale_bars must be non-negative, got {msb}")
        if "stale_threshold_z" in params and params["stale_threshold_z"] is not None:
            stz = float(params["stale_threshold_z"])
            if stz <= 0:
                errors.append(f"stale_threshold_z must be positive, got {stz}")
        if "min_std" in params:
            ms = float(params["min_std"])
            if ms <= 0:
                errors.append(f"min_std must be positive, got {ms}")
        if "high_vol_dampener" in params:
            hv = float(params["high_vol_dampener"])
            if not (0.0 <= hv <= 1.0):
                errors.append(f"high_vol_dampener must be in [0, 1], got {hv}")
        if "countertrend_dampener" in params:
            cd = float(params["countertrend_dampener"])
            if not (0.0 <= cd <= 1.0):
                errors.append(
                    f"countertrend_dampener must be in [0, 1] (1.0 = identity no-op), got {cd}"
                )
        if "with_trend_trigger_scale" in params:
            ws = float(params["with_trend_trigger_scale"])
            if not (0.0 < ws <= 1.0):
                errors.append(
                    f"with_trend_trigger_scale must be in (0, 1] (1.0 = identity "
                    f"no-op; above 1.0 would RAISE the with-trend bar, which "
                    f"inverts the intent), got {ws}"
                )
        if "rvol_source" in params:
            err = validate_rvol_source(params["rvol_source"])
            if err:
                errors.append(err)
        return errors
