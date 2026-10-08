// Run by tests/unit/test_signal_display_js.py (node --test). Covers the pure
// helpers behind the market panel's instrument picker.
import test from "node:test";
import assert from "node:assert/strict";

import {
    pickerTickers, readingIsShown,
} from "../../src/evotrader/web/static/js/components/instrument_picker.js";

test("the picker keeps the server's order, the primary first", () => {
    assert.deepEqual(pickerTickers(["XYZ", "ZYX", "OLD"]), ["XYZ", "ZYX", "OLD"]);
});

test("the chosen instrument is offered even before any reading of it", () => {
    assert.deepEqual(pickerTickers(["XYZ"], "ZYX"), ["XYZ", "ZYX"]);
    assert.deepEqual(pickerTickers([], "XYZ"), ["XYZ"]);
});

test("one option per instrument, upper case, no blanks", () => {
    assert.deepEqual(pickerTickers(["xyz", "XYZ", "", null, " zyx "], "Xyz"), ["XYZ", "ZYX"]);
    assert.deepEqual(pickerTickers(null), []);
});

test("a reading of the shown instrument is shown", () => {
    assert.equal(readingIsShown("XYZ", "XYZ"), true);
    assert.equal(readingIsShown("xyz", "XYZ"), true);
});

test("a reading of another instrument is not: its signal is read on its own prices", () => {
    // An inverse fund's reading sits roughly opposite the stock's; drawn on
    // the stock's chart, the line zigzags between the two.
    assert.equal(readingIsShown("ZYX", "XYZ"), false);
});

test("a reading that names no instrument, or a panel showing none yet, is shown", () => {
    assert.equal(readingIsShown(undefined, "XYZ"), true);
    assert.equal(readingIsShown("", "XYZ"), true);
    assert.equal(readingIsShown("ZYX", null), true);
});
