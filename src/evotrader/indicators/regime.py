"""Regime detection — classifies the current market environment.

The regime detector determines which market state we're in so the
Strategy Agent can dynamically adjust the weighting between momentum
and mean-reversion signals.

Supports multiple detection methods (configurable via settings.yaml):
- ``rule_based``: Simple threshold-based classification (default, most transparent)
- ``hmm``: Hidden Markov Model (future — proposed by Evolution Agent)
- ``clustering``: K-means/DBSCAN clustering (future)

Units convention
~~~~~~~~~~~~~~~~
- **Confidence** is always a probability in ``[0.0, 1.0]`` (0.65 = 65%).
- **base_conf** is derived from the fractional MA separation (e.g. 0.0199
  for a 1.99% gap between SMA20 and SMA50), NOT from a percent value.
  Do NOT multiply by 100 — that was a documented bug that pinned confidence
  to the clamp ceiling on every call.
- **daily_change_pct** and similar snapshot fields are in PERCENT
  (1.5 = 1.5%), matching the MarketSnapshot convention.
"""

from __future__ import annotations

import logging
import math
from typing import NamedTuple

import pandas as pd

from evotrader.indicators.atr import atr_percentile, compute_atr
from evotrader.indicators.moving_averages import compute_moving_averages
from evotrader.models.market import MarketRegime, RegimeClassification

logger = logging.getLogger(__name__)


class _Structure(NamedTuple):
    """Outcome of the shared structure/override/confidence block."""

    bull: bool
    confidence: float
    basis: str
    slope_confirms: bool


def _resolve_structure(
    *,
    sma_20_val: float,
    sma_50_val: float,
    trend_slope: float,
    price: float,
    ema_9_val: float,
    ema_21_val: float,
    macd_histogram: float | None,
) -> _Structure:
    """Decide bull/bear structure, why, and how confident to be.

    Extracted because the high-volatility branch and step 3 carried a
    near-verbatim 35-line copy of this logic and had ALREADY drifted — the
    high-vol copy reported ATR percentile and ADX in its reasoning, step 3
    reported neither. With the duplication in place, a reviewer patching one
    copy had roughly even odds of missing the other, so this extraction is a
    prerequisite for the three fixes below rather than separate cleanup.

    Returns the verdict, a bounded confidence, the condition that ACTUALLY
    decided the verdict, and whether the slope agrees with it.
    """
    STRONG_SLOPE = 0.0008  # ~8 bps of SMA20 drift over the 5-bar window

    structure_bull = sma_20_val > sma_50_val
    # Why the verdict landed where it did. The reasoning string must describe
    # THIS, not the SMA comparison — that comparison can be reversed below, and
    # printing it regardless produced live output asserting
    # "SMA20 126.21 <= SMA50 108.16" on 2026-09-16 19:30, which is false.
    basis = f"SMA20 {sma_20_val:.2f} {'>' if structure_bull else '<='} SMA50 {sma_50_val:.2f}"
    slope_pct = (trend_slope / sma_50_val) if sma_50_val else 0.0
    location_conf_penalty = 1.0

    # ── PRICE-LOCATION OVERRIDE ───────────────────────────────────
    # The SMA20/SMA50 cross lags in V-recoveries. If price is above all fast
    # MAs the tape is not bearish regardless of the stale cross, and mirrored
    # for bullish-structure/bearish-price.
    if ema_9_val > 0 and ema_21_val > 0 and sma_20_val > 0:
        reclaimed_up = price > ema_9_val and price > ema_21_val and price > sma_20_val
        reclaimed_dn = price < ema_9_val and price < ema_21_val and price < sma_20_val
        if (not structure_bull) and reclaimed_up:
            structure_bull = True
            location_conf_penalty = 0.6
            basis = (
                f"price {price:.2f} reclaimed EMA9 {ema_9_val:.2f} / "
                f"EMA21 {ema_21_val:.2f} / SMA20 {sma_20_val:.2f} "
                f"(overrides stale bearish cross)"
            )
        elif structure_bull and reclaimed_dn:
            structure_bull = False
            location_conf_penalty = 0.6
            basis = (
                f"price {price:.2f} below EMA9 {ema_9_val:.2f} / "
                f"EMA21 {ema_21_val:.2f} / SMA20 {sma_20_val:.2f} "
                f"(overrides stale bullish cross)"
            )
        # ── MAJORITY + MACD CONFIRMATION ──────────────────────────
        # Unanimity across EMA9/EMA21/SMA20 is a high bar, and on 2026-09-16 the
        # EMA21 leg alone held a bull tag in place through a -4.25% session:
        # seven of eight cycles read trending_bull at 0.95 while MACD histogram
        # ran negative all day (-0.64 -> -1.02) and trend_persistence had already
        # flipped bull_stack to false. Trend-following weights on a falling tape
        # put the composite LONG on all eight cycles (mean +0.108) as MSTR went
        # 130.02 -> 124.09. Zero for eight on direction.
        #
        # The slope escape hatch below cannot help: a 20-day mean does not roll
        # over inside one session (SMA20 slope was +6.90).
        #
        # Symmetric by construction — a labelling correction, not a directional
        # lean. It touches no threshold, sizing band or participation gate.
        elif macd_histogram is not None:
            below = sum((price < ema_9_val, price < ema_21_val, price < sma_20_val))
            above = sum((price > ema_9_val, price > ema_21_val, price > sma_20_val))
            if structure_bull and below >= 2 and macd_histogram < 0:
                structure_bull = False
                location_conf_penalty = 0.6
                basis = (
                    f"price below {below}/3 fast MAs with MACD hist "
                    f"{macd_histogram:+.2f} confirming (stale bullish cross)"
                )
            elif (not structure_bull) and above >= 2 and macd_histogram > 0:
                structure_bull = True
                location_conf_penalty = 0.6
                basis = (
                    f"price above {above}/3 fast MAs with MACD hist "
                    f"{macd_histogram:+.2f} confirming (stale bearish cross)"
                )

    if structure_bull and slope_pct < -STRONG_SLOPE and price < sma_20_val:
        structure_bull = False
        basis = f"SMA20 slope {slope_pct:+.5f} rolling over with price below SMA20"
    elif (not structure_bull) and slope_pct > STRONG_SLOPE and price > sma_20_val:
        structure_bull = True
        basis = f"SMA20 slope {slope_pct:+.5f} turning up with price above SMA20"

    slope_confirms = (trend_slope > 0) if structure_bull else (trend_slope < 0)

    # base_conf is a FRACTION (0.0199 == 1.99% MA separation).
    #
    # Multiplying it by a CONSTANT — 100 or 10 — saturates the clamp and makes
    # the output two-valued. Measured 2026-09-16 19:30 on MSTR: SMA20 126.209 vs
    # SMA50 108.160 gives base_conf 0.16687, so the linear term alone was
    # 1.6687, nearly twice the 0.95 ceiling. Across eight cycles that day
    # confidence took exactly two values, 0.95 and 0.57 (= 0.95 x the 0.6
    # location penalty), and the strategy agent correctly discarded it every
    # time as "a saturated rail, not evidence". A number nobody can use is worse
    # than no number.
    #
    # A bounded transform keeps the same ordering but spends its range on the
    # separations that actually occur. This is a RESOLUTION change: it raises no
    # firing bar and gates no trade.
    base_conf = abs(sma_20_val - sma_50_val) / sma_50_val if sma_50_val else 0.0
    confidence = (
        min(
            0.95,
            0.35 + 0.30 * math.tanh(base_conf / 0.04) + (0.20 if slope_confirms else 0.0),
        )
        * location_conf_penalty
    )

    return _Structure(structure_bull, confidence, basis, slope_confirms)


def detect_regime_rule_based(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    adx_threshold: float = 25.0,
    volatility_high_pct: float = 75.0,
    daily_change_pct: float | None = None,
    vwap: float | None = None,
    macd_histogram: float | None = None,
) -> RegimeClassification:
    """Classify market regime using rule-based thresholds.

    Decision tree:
    1. If ATR percentile > ``volatility_high_pct``:
       a. If ADX >= threshold OR clear SMA crossover with slope → TRENDING_BULL/BEAR
       b. Else → HIGH_VOLATILITY (directionless)
    2. If ADX < ``adx_threshold`` → RANGE_BOUND
       2b. Intraday trend override: if |daily_change_pct| >= 1.5% AND price
           is beyond session VWAP in the move's direction AND MACD histogram
           confirms → TRENDING_BULL/BEAR (daily ADX lags fresh single-session
           trends by construction)
    3. If SMA(20) > SMA(50) and positive slope → TRENDING_BULL
    4. Else → TRENDING_BEAR

    Args:
        close: Series of closing prices (at least 60 bars).
        high: Series of high prices.
        low: Series of low prices.
        volume: Series of trade volumes (unused in rule-based, for interface compat).
        adx_threshold: ADX threshold for trending vs. range-bound.
        volatility_high_pct: ATR percentile threshold for high-volatility regime.
        daily_change_pct: Today's price change in percent (e.g. -2.38 = -2.38%).
            Used for intraday trend override when ADX lags.
        vwap: Session VWAP value. Used with daily_change_pct for intraday override.
        macd_histogram: Current MACD histogram value. Confirms intraday trend direction.

    Returns:
        ``RegimeClassification`` with regime, confidence, and diagnostics.
    """
    if len(close) < 50:
        return RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=0.3,
            reasoning="Insufficient data (<50 bars) — defaulting to range_bound.",
        )

    # Compute required indicators
    mas = compute_moving_averages(close)
    atr_series = compute_atr(high, low, close, period=14)
    current_atr = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
    atr_pct = atr_percentile(current_atr, atr_series, lookback=60)

    # Compute ADX (simplified directional index)
    adx_value = _compute_adx_simplified(high, low, close, period=14)

    # Compute trend direction (slope of SMA-20 over the last 5 bars)
    sma_20_recent = mas.sma_20.dropna().tail(5)
    trend_slope = 0.0
    if len(sma_20_recent) >= 2:
        trend_slope = float(sma_20_recent.iloc[-1] - sma_20_recent.iloc[0])

    sma_20_val = float(mas.sma_20.iloc[-1]) if not mas.sma_20.isna().iloc[-1] else 0
    sma_50_val = float(mas.sma_50.iloc[-1]) if not mas.sma_50.isna().iloc[-1] else 0

    # ── Decision tree ─────────────────────────────────────────
    # Step 1: High volatility check — but do NOT discard trend information.
    # When volatility is high AND a clear trend exists, classify as the
    # trending regime (with attenuated confidence) so the composite applies
    # trend-following weights instead of mean-reversion fade weights.
    if atr_pct >= volatility_high_pct:
        has_trend = adx_value >= adx_threshold or (
            sma_20_val > 0
            and sma_50_val > 0
            and abs(sma_20_val - sma_50_val) / sma_50_val > 0.001
            and (abs(trend_slope) / sma_50_val if sma_50_val else 0.0) > 0.0002
        )

        if has_trend:
            # Structure gives the baseline; a strongly contradicting SMA20 slope
            # (normalised to price) OVERRIDES stale structure so a fresh pullback/
            # bounce is classified by its current momentum, not a lagging cross.
            price = float(close.iloc[-1])
            ema_9_val = float(mas.ema_9.iloc[-1]) if not mas.ema_9.isna().iloc[-1] else 0.0
            ema_21_val = float(mas.ema_21.iloc[-1]) if not mas.ema_21.isna().iloc[-1] else 0.0
            _st = _resolve_structure(
                sma_20_val=sma_20_val,
                sma_50_val=sma_50_val,
                trend_slope=trend_slope,
                price=price,
                ema_9_val=ema_9_val,
                ema_21_val=ema_21_val,
                macd_histogram=macd_histogram,
            )
            structure_bull = _st.bull
            confidence = _st.confidence
            slope_confirms = _st.slope_confirms
            basis = _st.basis

            if structure_bull:
                return RegimeClassification(
                    regime=MarketRegime.TRENDING_BULL,
                    confidence=confidence,
                    reasoning=(
                        f"High vol (ATR {atr_pct:.0f}%pctile) WITH directional trend "
                        f"(ADX {adx_value:.1f}, slope {trend_slope:+.2f}, "
                        f"{basis}). "
                        f"Volatile bullish trend{'(slope confirms)' if slope_confirms else '(pullback)'} — using trend-following weights."
                    ),
                    adx=adx_value,
                    trend_direction=trend_slope,
                    volatility_percentile=atr_pct,
                )
            else:
                return RegimeClassification(
                    regime=MarketRegime.TRENDING_BEAR,
                    confidence=confidence,
                    reasoning=(
                        f"High vol (ATR {atr_pct:.0f}%pctile) WITH directional trend "
                        f"(ADX {adx_value:.1f}, slope {trend_slope:+.2f}, "
                        f"{basis}). "
                        f"Volatile bearish trend{'(slope confirms)' if slope_confirms else '(bounce)'} — using trend-following weights."
                    ),
                    adx=adx_value,
                    trend_direction=trend_slope,
                    volatility_percentile=atr_pct,
                )

        # Genuinely directionless high volatility — mean-reversion regime
        return RegimeClassification(
            regime=MarketRegime.HIGH_VOLATILITY,
            confidence=min(0.9, atr_pct / 100),
            reasoning=(
                f"ATR percentile {atr_pct:.0f}% exceeds threshold {volatility_high_pct}%, "
                f"no directional conviction (ADX {adx_value:.1f}, slope {trend_slope:+.2f}). "
                f"Directionless high volatility."
            ),
            adx=adx_value,
            trend_direction=trend_slope,
            volatility_percentile=atr_pct,
        )

    # Step 2: Range-bound check
    if adx_value < adx_threshold:
        # ── Step 2b: Intraday trend override ─────────────────────
        # Daily ADX is a 14-day smoothed measure and stays <25 on a fresh
        # single-session trend (7/23: -2.38% day read ADX 20.5 = range_bound,
        # zeroing momentum and leaving the composite long-biased into a
        # falling market). If today's move is large AND price is beyond the
        # session VWAP in the move's direction AND MACD histogram confirms,
        # classify as trending for weighting purposes.
        #
        # daily_change_pct is in PERCENT: 1.5 = 1.5% (see units convention).
        INTRADAY_TREND_THRESHOLD = 1.5  # 1.5%
        if (
            daily_change_pct is not None
            and vwap is not None
            and macd_histogram is not None
            and abs(daily_change_pct) >= INTRADAY_TREND_THRESHOLD
        ):
            price = float(close.iloc[-1])
            bearish = daily_change_pct < 0 and price < vwap and macd_histogram < 0
            bullish = daily_change_pct > 0 and price > vwap and macd_histogram > 0
            if bearish or bullish:
                # Confidence scales with move magnitude, capped at 0.65
                override_conf = min(0.65, 0.45 + abs(daily_change_pct) * 0.05)
                return RegimeClassification(
                    regime=(MarketRegime.TRENDING_BEAR if bearish else MarketRegime.TRENDING_BULL),
                    confidence=override_conf,
                    reasoning=(
                        f"Intraday trend override: day {daily_change_pct:+.2f}%, "
                        f"price {'below' if bearish else 'above'} session VWAP, "
                        f"MACD hist {macd_histogram:+.2f} confirms. Daily ADX "
                        f"({adx_value:.1f}) lags fresh single-session trends."
                    ),
                    adx=adx_value,
                    trend_direction=trend_slope,
                    volatility_percentile=atr_pct,
                )

        # ── Step 2c: MULTI-SESSION GRIND OVERRIDE ────────────────
        # Daily ADX(14) stays below 25 during a slow one-way grind, and the
        # single-session override above never fires when the move arrives in
        # sub-1.5% daily increments.  QQQ fell 5.44% over 5 sessions in
        # increments of -0.20/-1.28/-1.17/-0.72/-0.43% with ADX pinned at
        # 24.5 — classified range_bound every cycle, zeroing momentum and
        # leaving the composite structurally long into a falling market.
        CUMULATIVE_TREND_THRESHOLD = 3.0  # 3.0% over the lookback window
        if len(close) >= 6 and sma_20_val > 0 and sma_50_val > 0:
            cum_change_pct = (
                (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100
            )
            price = float(close.iloc[-1])
            grind_bear = (
                cum_change_pct <= -CUMULATIVE_TREND_THRESHOLD
                and sma_20_val < sma_50_val
                and price < sma_20_val
            )
            grind_bull = (
                cum_change_pct >= CUMULATIVE_TREND_THRESHOLD
                and sma_20_val > sma_50_val
                and price > sma_20_val
            )
            if grind_bear or grind_bull:
                return RegimeClassification(
                    regime=(
                        MarketRegime.TRENDING_BEAR if grind_bear else MarketRegime.TRENDING_BULL
                    ),
                    confidence=min(0.60, 0.40 + abs(cum_change_pct) * 0.02),
                    reasoning=(
                        f"Multi-session grind override: {cum_change_pct:+.2f}% "
                        f"over 5 sessions with MA stack aligned. Daily ADX "
                        f"({adx_value:.1f}) and the single-session override "
                        f"both miss slow one-way trends by construction."
                    ),
                    adx=adx_value,
                    trend_direction=trend_slope,
                    volatility_percentile=atr_pct,
                )

        return RegimeClassification(
            regime=MarketRegime.RANGE_BOUND,
            confidence=min(0.85, (adx_threshold - adx_value) / adx_threshold + 0.3),
            reasoning=(
                f"ADX {adx_value:.1f} below threshold {adx_threshold}. "
                f"Market lacks directional conviction — range-bound."
            ),
            adx=adx_value,
            trend_direction=trend_slope,
            volatility_percentile=atr_pct,
        )

    # Step 3: Trending direction — structure baseline, with slope-override.
    # Observed 2026-08-04..06: QQQ ~2% ABOVE its own SMA20 and still tagged
    # trending_bear at 0.90 on eight consecutive cycles, mis-weighting the whole
    # ensemble — the price-location override inside the helper exists for that.
    price = float(close.iloc[-1])
    ema_9_val = float(mas.ema_9.iloc[-1]) if not mas.ema_9.isna().iloc[-1] else 0.0
    ema_21_val = float(mas.ema_21.iloc[-1]) if not mas.ema_21.isna().iloc[-1] else 0.0
    _st = _resolve_structure(
        sma_20_val=sma_20_val,
        sma_50_val=sma_50_val,
        trend_slope=trend_slope,
        price=price,
        ema_9_val=ema_9_val,
        ema_21_val=ema_21_val,
        macd_histogram=macd_histogram,
    )
    structure_bull = _st.bull
    confidence = _st.confidence
    slope_confirms = _st.slope_confirms
    basis = _st.basis

    if structure_bull:
        return RegimeClassification(
            regime=MarketRegime.TRENDING_BULL,
            confidence=confidence,
            reasoning=(
                f"{basis} (bullish structure); "
                f"slope {trend_slope:+.2f} {'confirms' if slope_confirms else 'is a pullback within'} the uptrend."
            ),
            adx=adx_value,
            trend_direction=trend_slope,
            volatility_percentile=atr_pct,
        )
    else:
        return RegimeClassification(
            regime=MarketRegime.TRENDING_BEAR,
            confidence=confidence,
            reasoning=(
                f"{basis} (bearish structure); "
                f"slope {trend_slope:+.2f} {'confirms' if slope_confirms else 'is a bounce within'} the downtrend."
            ),
            adx=adx_value,
            trend_direction=trend_slope,
            volatility_percentile=atr_pct,
        )


def _compute_adx_simplified(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> float:
    """Compute a simplified ADX value.

    Full ADX computation:
    1. +DM = High - prevHigh (if positive and > -DM, else 0)
    2. -DM = prevLow - Low (if positive and > +DM, else 0)
    3. +DI = smoothed(+DM) / ATR * 100
    4. -DI = smoothed(-DM) / ATR * 100
    5. DX = |+DI - -DI| / (+DI + -DI) * 100
    6. ADX = smoothed(DX)
    """
    if len(close) < period * 2:
        # Bias toward range_bound in the degenerate case: return below the
        # default adx_threshold so the caller does not misclassify as trending.
        return 0.0

    alpha = 1.0 / period

    prev_high = high.shift(1)
    prev_low = low.shift(1)

    # Directional movement
    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)

    # Zero out the smaller DM
    mask_plus = plus_dm > minus_dm
    mask_minus = minus_dm > plus_dm
    plus_dm = plus_dm.where(mask_plus, 0)
    minus_dm = minus_dm.where(mask_minus, 0)

    # Smoothed values (Wilder's smoothing)
    atr_series = compute_atr(high, low, close, period)
    smooth_plus = plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    smooth_minus = minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    # Directional indicators
    safe_atr = atr_series.replace(0, float("nan"))
    plus_di = (smooth_plus / safe_atr) * 100
    minus_di = (smooth_minus / safe_atr) * 100

    # DX and ADX
    di_sum = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()
    dx = (di_diff / di_sum.replace(0, float("nan"))) * 100

    adx = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    last_adx = adx.dropna()
    return float(last_adx.iloc[-1]) if not last_adx.empty else 0.0
