/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — the account chart's signal overlay: pure helpers.
   No DOM and no Chart.js, so chart.js draws with them, app.js summarises
   with them, and tests/js/equity_overlay.test.mjs covers them.
   ═══════════════════════════════════════════════════════════════════════ */

import { signalLean, SIGNAL_THRESHOLDS } from "./signal_display.js";

/**
 * What the equity chart plots for each history point.
 *
 * Account: the broker value with deposits not yet made added back, so the line
 * starts at the total invested and a deposit is not drawn as a gain.
 * P&L: cumulative realized P&L from zero at the start of the period.
 */
export function equityValues(history, mode) {
    const rows = history || [];
    if (rows.length === 0) return [];
    if (mode === "pnl") {
        const base = rows[0].cumulative_pnl || 0;
        return rows.map(h => (h.cumulative_pnl || 0) - base);
    }
    const totalDeposits = rows[rows.length - 1].cumulative_deposits || 0;
    return rows.map(h => h.portfolio_value + (totalDeposits - (h.cumulative_deposits || 0)));
}

/** Each history date's signal summary from /api/chart/signal-daily, or null. */
export function alignSignalDays(history, days) {
    const byDate = new Map((days || []).map(d => [d.date, d]));
    return (history || []).map(h => byDate.get(h.date) || null);
}

/**
 * Which way a day's average leaned, on the strategy's own thresholds:
 * "long", "short" or "neutral"; null for a day without readings.
 */
export function daySide(day) {
    if (!day) return null;
    const tone = signalLean(day.mean, day.regime).tone;
    if (tone.startsWith("bull")) return "long";
    if (tone.startsWith("bear")) return "short";
    return "neutral";
}

/** Whether a day's average reaches the strategy's "strong" level. */
export function isStrong(day) {
    return !!day && Math.abs(day.mean) >= SIGNAL_THRESHOLDS.strong;
}

/** Pearson correlation of two equal-length lists; null when it cannot be measured. */
export function correlation(xs, ys) {
    const n = Math.min(xs.length, ys.length);
    if (n < 3) return null;
    const mx = xs.slice(0, n).reduce((a, b) => a + b, 0) / n;
    const my = ys.slice(0, n).reduce((a, b) => a + b, 0) / n;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) {
        const dx = xs[i] - mx, dy = ys[i] - my;
        sxy += dx * dy;
        sxx += dx * dx;
        syy += dy * dy;
    }
    if (sxx === 0 || syy === 0) return null;
    return sxy / Math.sqrt(sxx * syy);
}

/**
 * How the chart's value moved by the next day the system ran, after each day's
 * signal. A day's readings come during that day, so the same day's change is
 * partly the move the signal reacted to; the next day's is the one it could
 * have called. The last day with readings has no next day yet and is left out.
 *
 * @param {number[]} values  what the chart plots (equityValues), per history date
 * @param {(object|null)[]} aligned  alignSignalDays for the same dates
 */
export function nextDayFollowThrough(values, aligned) {
    const pairs = [];
    for (let i = 0; i < aligned.length; i++) {
        if (!aligned[i]) continue;
        let j = i + 1;
        while (j < aligned.length && !aligned[j]) j++;
        if (j >= aligned.length) break;
        pairs.push({ signal: aligned[i].mean, side: daySide(aligned[i]), change: values[j] - values[i] });
    }
    const long = pairs.filter(p => p.side === "long");
    const short = pairs.filter(p => p.side === "short");
    return {
        days: pairs.length,
        long: { days: long.length, up: long.filter(p => p.change > 0).length },
        short: { days: short.length, down: short.filter(p => p.change < 0).length },
        r: correlation(pairs.map(p => p.signal), pairs.map(p => p.change)),
    };
}

/** A symmetric range for the signal pane: the smallest of a few round steps that fits. */
export function signalAxisLimit(aligned) {
    const biggest = Math.max(0, ...(aligned || []).filter(Boolean).flatMap(d => [Math.abs(d.min), Math.abs(d.max)]));
    return [0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0].find(step => biggest <= step) || 1.0;
}
