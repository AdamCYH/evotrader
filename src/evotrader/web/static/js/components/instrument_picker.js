/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — the market panel's instrument picker: pure helpers.
   No DOM, so app.js renders with them and
   tests/js/instrument_picker.test.mjs covers them.

   Each instrument's signal is read on its own prices, and an inverse fund's
   reads roughly opposite to the stock it tracks. The panel shows one
   instrument at a time: one chart of both zigzagged between a reading and
   its mirror image.
   ═══════════════════════════════════════════════════════════════════════ */

/**
 * The picker's options, in the order given (the server puts the primary
 * first), with `chosen` added if missing: upper case, no repeats, no blanks.
 */
export function pickerTickers(tickers, chosen = null) {
    const out = [];
    for (const t of [...(tickers || []), chosen]) {
        const symbol = t ? String(t).trim().toUpperCase() : "";
        if (symbol && !out.includes(symbol)) out.push(symbol);
    }
    return out;
}

/**
 * Whether a reading of `ticker` belongs on a panel showing `shown`. A reading
 * that names no instrument, or a panel that shows none yet, is shown, as
 * every reading was before the picker.
 */
export function readingIsShown(ticker, shown) {
    if (!ticker || !shown) return true;
    return String(ticker).trim().toUpperCase() === String(shown).trim().toUpperCase();
}
