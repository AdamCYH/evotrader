/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — plain-language display rules for the market panel.
   Pure functions (no DOM), shared by the panel and the signal chart, and
   covered by tests/js/signal_display.test.mjs.
   ═══════════════════════════════════════════════════════════════════════ */

/**
 * The strategy's own entry table ("Entry thresholds" in the strategy
 * instructions): a directional read starts at |0.03| in a trending regime and
 * |0.05| otherwise, and |0.10| and above is STRONG. The panel used ±0.15, which
 * labelled a +0.12 strong signal "HOLD".
 */
export const SIGNAL_THRESHOLDS = Object.freeze({ trending: 0.03, other: 0.05, strong: 0.10 });

/** Below this a channel has not voted — the composite's SIGNAL_EPSILON. */
export const VOTE_EPSILON = 0.001;

/**
 * The algorithm's lean, in words. It describes the combined signal only; what
 * to do with it is the trading agent's call, so no BUY / SELL verbs.
 */
export function signalLean(score, regime) {
    const s = Number(score);
    if (score === null || score === undefined || Number.isNaN(s)) {
        return { key: "none", label: "No reading", tone: "neutral" };
    }
    const entry = String(regime || "").toLowerCase().startsWith("trending")
        ? SIGNAL_THRESHOLDS.trending
        : SIGNAL_THRESHOLDS.other;
    const size = Math.abs(s);
    if (size < entry) return { key: "neutral", label: "Neutral", tone: "neutral" };
    const up = s > 0;
    if (size >= SIGNAL_THRESHOLDS.strong) {
        return up
            ? { key: "strong-bull", label: "Strong bullish", tone: "bull" }
            : { key: "strong-bear", label: "Strong bearish", tone: "bear" };
    }
    return up
        ? { key: "lean-bull", label: "Leaning bullish", tone: "bull-soft" }
        : { key: "lean-bear", label: "Leaning bearish", tone: "bear-soft" };
}

/** Plain names for today's channels. A new or evolved channel keeps its code name. */
const CHANNEL_LABELS = {
    momentum: "Trend following",
    mean_reversion: "Overbought / oversold",
    gap: "Opening gap",
    intraday_vwap_zscore: "Stretch from VWAP",
    event_window_timing: "Event timing",
    range_break_continuation: "Breakout",
    options_positioning: "Options sentiment",
    swing_failure_reversal: "Failed breakdown",
    trend_persistence: "Multi-day trend",
    vwap_reclaim_continuation: "VWAP reclaim",
    gap_fail_continuation: "Failed gap",
};

export function channelLabel(name) {
    const key = String(name || "");
    return CHANNEL_LABELS[key] || key.replace(/_/g, " ") || "Channel";
}

/** Why a channel is quiet, in a few words. The full code stays in the tooltip. */
const REASON_LABELS = {
    within_band: "no setup",
    not_reclaimed: "no setup",
    no_reclaim_yet: "no setup",
    dip_too_shallow: "no setup",
    dip_too_brief: "no setup",
    stretch_too_shallow: "no setup",
    too_few_confirm_bars: "no setup",
    reclaim_too_old: "setup expired",
    stale_flush: "setup expired",
    dip_too_deep_trend_break: "trend broken",
    outside_event_windows: "no event nearby",
    event_context_not_available: "no event data",
    iv_data_not_available: "no event data",
    no_options_data: "no options data yet",
    no_deltas: "no options data yet",
    stale_vwap_anchor: "no live intraday data",
    no_vwap: "no live intraday data",
    missing_vwap_or_atr: "no live intraday data",
    insufficient_intraday_data: "no live intraday data",
    insufficient_intraday_candles: "no live intraday data",
    insufficient_data: "not enough data",
    insufficient_daily_candles: "not enough history",
    day_change_unavailable: "no day change yet",
    gap_too_small: "no gap today",
    no_gap_or_atr: "no gap reading",
    no_previous_close: "no previous close",
    opening_range_forming: "too early in the session",
    no_session_vwap: "no live intraday data",
    gap_not_failed: "no setup",
    vwap_not_lost: "no setup",
    fill_not_held: "no setup",
};

function isVoting(sig) {
    const meta = sig.metadata || {};
    return meta.applicable !== false
        && meta.role !== "multiplier"
        && Number(sig.weight || 0) > 0
        && Math.abs(Number(sig.value || 0)) >= VOTE_EPSILON;
}

export function silenceReason(sig) {
    const meta = sig.metadata || {};
    const value = Number(sig.value || 0);
    if (meta.role === "multiplier") return "scales the signal";
    if (Number(sig.weight || 0) <= 0) {
        return Math.abs(value) >= VOTE_EPSILON
            ? `watch only (${value >= 0 ? "+" : ""}${value.toFixed(2)})`
            : "watch only";
    }
    const code = meta.reason ? String(meta.reason) : "";
    if (meta.applicable === false) return REASON_LABELS[code] || "missing data";
    if (meta.in_scope === false) return "not in play";
    return REASON_LABELS[code] || (code ? code.replace(/_/g, " ") : "no setup");
}

/**
 * Split channels into the ones pushing the combined signal and the quiet ones.
 *
 * ``share`` is each voter's slice of the total push, |value x weight| over the
 * sum across voters: the renormalisation scales every voter alike, so this is
 * who is actually driving the number (the configured weight alone is not —
 * momentum's 20% weight was ~84% of the signal on 2026-09-24).
 */
export function channelBreakdown(subSignals, composite) {
    const sigs = Array.isArray(subSignals) ? subSignals : [];
    const voters = sigs.filter(isVoting);
    const total = voters.reduce((acc, s) => acc + Math.abs(Number(s.value) * Number(s.weight)), 0);
    const sign = Math.sign(Number(composite) || 0);
    const voting = voters
        .map(s => {
            const value = Number(s.value);
            return {
                name: s.name,
                label: channelLabel(s.name),
                value,
                weight: Number(s.weight),
                share: total > 0 ? Math.abs(value * Number(s.weight)) / total : 0,
                against: sign !== 0 && Math.sign(value) !== sign,
                reason: (s.metadata || {}).reason || null,
            };
        })
        .sort((a, b) => b.share - a.share);
    const silent = sigs
        .filter(s => !isVoting(s))
        .map(s => ({
            name: s.name,
            label: channelLabel(s.name),
            reason: silenceReason(s),
            code: (s.metadata || {}).reason || null,
        }));
    return { voting, silent };
}

/**
 * True when intraday readings (VWAP, VWAP distance, IBS) describe the live
 * session. Outside regular hours the backend falls back to daily bars and tags
 * the VWAP ``prior_session``; those numbers are not today's tape.
 */
export function isLiveSession(indicators) {
    return String((indicators || {}).vwap_anchor || "") === "current_session";
}

/** Distance from VWAP as a fraction, or null when it is not a live reading. */
export function vwapDistance(indicators, close) {
    const ind = indicators || {};
    if (!isLiveSession(ind)) return null;
    if (ind.vwap_dist !== undefined && ind.vwap_dist !== null) return Number(ind.vwap_dist);
    const vwap = Number(ind.vwap);
    const price = Number(close);
    if (!vwap || !price) return null;
    return (price - vwap) / vwap;
}

/**
 * Today's volume against normal, preferring the time-of-day reading the
 * volume channels use; the daily ratio is yesterday's and says so.
 */
export function relativeVolume(indicators) {
    const ind = indicators || {};
    if (ind.session_relative_volume !== undefined && ind.session_relative_volume !== null) {
        return { value: Number(ind.session_relative_volume), label: "today" };
    }
    if (ind.relative_volume !== undefined && ind.relative_volume !== null) {
        return { value: Number(ind.relative_volume), label: "yesterday" };
    }
    return null;
}

const ET_SHORT = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const ET_LONG = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
});

/** "Thu 15:30" — chart ticks, in market time whatever the viewer's zone. */
export function etShort(timestamp) {
    const d = new Date(timestamp);
    return Number.isNaN(d.getTime()) ? "" : ET_SHORT.format(d).replace(",", "");
}

/** "Thu, Sep 24, 3:30 PM ET" — tooltips and "as of". */
export function etLong(timestamp) {
    const d = new Date(timestamp);
    return Number.isNaN(d.getTime()) ? "" : `${ET_LONG.format(d)} ET`;
}

/** "+0.59%" from a percent-unit number (0.5949 means 0.59%). */
export function formatPct(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
    const n = Number(value);
    return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
}
