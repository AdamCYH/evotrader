// Run by tests/unit/test_signal_display_js.py (node --test). Covers the display
// rules the console's market panel applies to live data.
import test from "node:test";
import assert from "node:assert/strict";

import {
    channelBreakdown, channelLabel, etLong, etShort, formatPct, isLiveSession,
    relativeVolume, signalLean, silenceReason, vwapDistance,
} from "../../src/evotrader/web/static/js/components/signal_display.js";

// The stored votes of 2026-09-24 17:00 ET (session 0d90b576), trimmed.
const LIVE_1700 = [
    { name: "momentum", value: 0.5613, weight: 0.2045, metadata: {} },
    { name: "mean_reversion", value: -0.1657, weight: 0.129, metadata: {} },
    { name: "gap", value: 0.0, weight: 0.0, metadata: { applicable: true } },
    { name: "intraday_vwap_zscore", value: 0.0, weight: 0.1935,
      metadata: { applicable: false, reason: "stale_vwap_anchor" } },
    { name: "event_window_timing", value: 0.0, weight: 0.0215,
      metadata: { applicable: false, reason: "event_context_not_available" } },
    { name: "range_break_continuation", value: 0.0, weight: 0.1075, metadata: { in_scope: false } },
    { name: "options_positioning", value: 0.0, weight: 0.086,
      metadata: { applicable: false, reason: "no_options_data" } },
    { name: "swing_failure_reversal", value: 0.0, weight: 0.129,
      metadata: { applicable: true, reason: "stale_flush" } },
    { name: "trend_persistence", value: 0.0, weight: 0.129, metadata: {} },
    { name: "vwap_reclaim_continuation", value: 0.1804, weight: 0.0,
      metadata: { applicable: true, reason: "reclaim_confirmed" } },
];

test("lean uses the strategy's thresholds, not ±0.15", () => {
    assert.equal(signalLean(0.12, "trending_bull").label, "Strong bullish");
    assert.equal(signalLean(0.04, "trending_bull").label, "Leaning bullish");
    assert.equal(signalLean(0.04, "range_bound").label, "Neutral");
    assert.equal(signalLean(-0.06, "high_volatility").label, "Leaning bearish");
    assert.equal(signalLean(-0.25, "trending_bear").label, "Strong bearish");
    assert.equal(signalLean(0.02, "trending_bull").label, "Neutral");
    assert.equal(signalLean(null, "trending_bull").key, "none");
});

test("lean never names a trade", () => {
    for (const s of [-0.5, -0.07, 0, 0.07, 0.5]) {
        assert.doesNotMatch(signalLean(s, "trending_bull").label, /BUY|SELL|PUT|HOLD/i);
    }
});

test("breakdown separates the two voters from the eight quiet channels", () => {
    const { voting, silent } = channelBreakdown(LIVE_1700, 0.2766);
    assert.deepEqual(voting.map(v => v.name), ["momentum", "mean_reversion"]);
    assert.equal(silent.length, 8);
    const [mom, mr] = voting;
    const push = { mom: 0.5613 * 0.2045, mr: 0.1657 * 0.129 };
    assert.ok(Math.abs(mom.share - push.mom / (push.mom + push.mr)) < 1e-9, `momentum share ${mom.share}`);
    assert.ok(mom.share > 0.84, "20% configured weight, ~84% of the actual push");
    assert.ok(Math.abs(mom.share + mr.share - 1) < 1e-9);
    assert.equal(mom.against, false);
    assert.equal(mr.against, true, "a sell vote under a bullish signal is dissent");
});

test("every quiet channel says why", () => {
    const { silent } = channelBreakdown(LIVE_1700, 0.2766);
    const why = Object.fromEntries(silent.map(s => [s.name, s.reason]));
    assert.equal(why.gap, "watch only");
    assert.equal(why.vwap_reclaim_continuation, "watch only (+0.18)");
    assert.equal(why.intraday_vwap_zscore, "no live intraday data");
    assert.equal(why.options_positioning, "no options data yet");
    assert.equal(why.range_break_continuation, "not in play");
    assert.equal(why.swing_failure_reversal, "setup expired");
    assert.equal(why.trend_persistence, "no setup");
    assert.equal(silenceReason({ name: "x", value: 0, weight: 0.1, metadata: { role: "multiplier" } }),
        "scales the signal");
    // 17:00 ET after the close: swing_failure has no intraday bars to read.
    assert.equal(silenceReason({ name: "swing_failure_reversal", value: 0, weight: 0.129,
        metadata: { applicable: false, reason: "insufficient_data" } }), "not enough data");
});

test("the failed-gap channel has a plain name and plain reasons", () => {
    assert.equal(channelLabel("gap_fail_continuation"), "Failed gap");
    assert.equal(silenceReason({ name: "gap_fail_continuation", value: 0, weight: 0.1,
        metadata: { applicable: false, reason: "gap_too_small" } }), "no gap today");
    assert.equal(silenceReason({ name: "gap_fail_continuation", value: 0, weight: 0.1,
        metadata: { applicable: true, reason: "gap_not_failed" } }), "no setup");
    assert.equal(silenceReason({ name: "gap_fail_continuation", value: -0.41, weight: 0.0,
        metadata: { applicable: true, reason: "gap_failed" } }), "watch only (-0.41)");
});

test("unknown channels and reasons still render", () => {
    assert.equal(channelLabel("keltner_squeeze"), "keltner squeeze");
    assert.equal(silenceReason({ name: "k", value: 0, weight: 0.1, metadata: { reason: "brand_new_reason" } }),
        "brand new reason");
});

test("intraday readings are hidden outside the live session", () => {
    const afterHours = { vwap: 113.99, vwap_anchor: "prior_session", vwap_dist: 0.4129, ibs: 0.0 };
    assert.equal(isLiveSession(afterHours), false);
    assert.equal(vwapDistance(afterHours, 161.07), null, "the $114 multi-day VWAP is not today's");
    const live = { vwap: 161.716, vwap_anchor: "current_session" };
    assert.ok(Math.abs(vwapDistance(live, 163.165) - 0.00896) < 1e-4, "computed from price when not stored");
    assert.equal(vwapDistance({ ...live, vwap_dist: 0.01 }, 163.165), 0.01, "stored value wins");
});

test("relative volume prefers today's time-of-day reading", () => {
    assert.deepEqual(relativeVolume({ relative_volume: 0.8136, session_relative_volume: 0.7198 }),
        { value: 0.7198, label: "today" });
    assert.deepEqual(relativeVolume({ relative_volume: 0.8136 }), { value: 0.8136, label: "yesterday" });
    assert.equal(relativeVolume({}), null);
});

test("times are shown in market time with the day", () => {
    assert.equal(etShort("2026-09-24T19:30:05+00:00"), "Thu 15:30");
    assert.equal(etShort("2026-09-24T12:30:00+00:00"), "Thu 08:30");
    assert.equal(etLong("2026-09-24T21:00:00+00:00"), "Thu, Sep 24, 5:00 PM ET");
    assert.equal(etShort("not a date"), "");
});

test("percent formatting keeps percent units", () => {
    assert.equal(formatPct(-0.0401), "-0.04%");
    assert.equal(formatPct(0.5949), "+0.59%");
    assert.equal(formatPct(null), null);
});
