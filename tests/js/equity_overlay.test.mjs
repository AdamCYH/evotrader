// Run by tests/unit/test_signal_display_js.py (node --test). Covers the pure
// helpers behind the account chart's signal overlay.
import test from "node:test";
import assert from "node:assert/strict";

import {
    alignSignalDays, correlation, daySide, equityValues, isStrong,
    nextDayFollowThrough, signalAxisLimit,
} from "../../src/evotrader/web/static/js/components/equity_overlay.js";

// Five calendar days of a made-up account: a deposit of 1,000 on day 2, a
// weekend on days 4-5 (carried forward), realized P&L on days 3 and 6.
const HISTORY = [
    { date: "2026-03-05", portfolio_value: 5000, cumulative_deposits: 5000, cumulative_pnl: 0 },
    { date: "2026-03-06", portfolio_value: 6050, cumulative_deposits: 6000, cumulative_pnl: 0 },
    { date: "2026-03-07", portfolio_value: 6050, cumulative_deposits: 6000, cumulative_pnl: 0 },
    { date: "2026-03-08", portfolio_value: 6050, cumulative_deposits: 6000, cumulative_pnl: 0 },
    { date: "2026-03-09", portfolio_value: 5990, cumulative_deposits: 6000, cumulative_pnl: -40 },
];

const day = (date, mean, regime = "trending_bull", extra = {}) => (
    { date, mean, min: mean - 0.05, max: mean + 0.05, count: 9, regime, ticker: "XYZ", ...extra }
);

test("account values add back deposits not yet made, as the chart did", () => {
    // The total deposited (6,000) is the starting level, so the deposit on the
    // second day is not drawn as a gain of 1,000.
    assert.deepEqual(equityValues(HISTORY, "account"), [6000, 6050, 6050, 6050, 5990]);
});

test("P&L values start at zero for the period", () => {
    const later = HISTORY.map(h => ({ ...h, cumulative_pnl: h.cumulative_pnl + 100 }));
    assert.deepEqual(equityValues(later, "pnl"), [0, 0, 0, 0, -40]);
    assert.deepEqual(equityValues([], "pnl"), []);
});

test("each history date gets its day's signal, or null", () => {
    const aligned = alignSignalDays(HISTORY, [day("2026-03-05", 0.2), day("2026-03-09", -0.1)]);
    assert.equal(aligned.length, HISTORY.length);
    assert.equal(aligned[0].mean, 0.2);
    assert.equal(aligned[1], null);
    assert.equal(aligned[4].mean, -0.1);
    assert.deepEqual(alignSignalDays(HISTORY, null), [null, null, null, null, null]);
});

test("a day's side follows the strategy's lean thresholds", () => {
    assert.equal(daySide(day("d", 0.04, "trending_bull")), "long");
    assert.equal(daySide(day("d", 0.04, "range_bound")), "neutral");
    assert.equal(daySide(day("d", -0.06, "range_bound")), "short");
    assert.equal(daySide(day("d", 0.0)), "neutral");
    assert.equal(daySide(null), null);
    assert.equal(isStrong(day("d", 0.12)), true);
    assert.equal(isStrong(day("d", -0.05)), false);
});

test("correlation, and when it cannot be measured", () => {
    assert.ok(Math.abs(correlation([1, 2, 3], [2, 4, 6]) - 1) < 1e-12);
    assert.ok(Math.abs(correlation([1, 2, 3], [3, 2, 1]) + 1) < 1e-12);
    assert.equal(correlation([1, 2], [1, 2]), null);
    assert.equal(correlation([1, 1, 1], [1, 2, 3]), null);
});

test("next-day follow-through skips days the system did not run", () => {
    // Signals on days 1, 2 and 5; days 3-4 are a weekend without readings.
    const aligned = alignSignalDays(HISTORY, [
        day("2026-03-05", 0.20),   // long, account 6000 -> 6050 next run: up
        day("2026-03-06", 0.15),   // long, 6050 -> 5990 on day 5 (next run): down
        day("2026-03-09", -0.20),  // last day with readings: no next day yet
    ]);
    const stats = nextDayFollowThrough(equityValues(HISTORY, "account"), aligned);
    assert.equal(stats.days, 2);
    assert.deepEqual(stats.long, { days: 2, up: 1 });
    assert.deepEqual(stats.short, { days: 0, down: 0 });
    assert.equal(stats.r, null, "two pairs cannot give a correlation");
});

test("short-leaning days count a fall as a hit", () => {
    const values = [100, 90, 95, 80];
    const aligned = [day("a", -0.2), day("b", -0.2), day("c", 0.2), day("d", 0.0)];
    const stats = nextDayFollowThrough(values, aligned);
    assert.deepEqual(stats.short, { days: 2, down: 1 });
    assert.deepEqual(stats.long, { days: 1, up: 0 });
    assert.equal(stats.days, 3);
    assert.ok(stats.r !== null);
});

test("the signal pane's range is the smallest round step that fits", () => {
    assert.equal(signalAxisLimit([day("a", 0.02)]), 0.1);
    assert.equal(signalAxisLimit([day("a", 0.30)]), 0.4);
    assert.equal(signalAxisLimit([day("a", -0.60), null]), 0.75);
    assert.equal(signalAxisLimit([]), 0.1);
    assert.equal(signalAxisLimit([day("a", 1.2)]), 1.0);
});
