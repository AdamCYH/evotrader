import { escapeHtml, formatCurrency, formatTime } from "./utils.js";

/**
 * Render Trade Journal database history entries.
 *
 * Table columns:
 *  ID | Time | Asset | Trade | Status | Qty | Price | P&L | Regime | Reasoning
 *
 * "Asset" combines ticker + option badge + strike/exp.
 * "Trade" combines action + direction into a single compound badge.
 * "Status" shows the order lifecycle state as a compact dot + label.
 */

/**
 * Build the price cell content for a trade row.
 *
 * Columns in the DB:
 *   - limit_price: The agent's requested limit/stop price
 *   - fill_price:  The actual broker fill price
 *   - price:       The effective execution price (= fill_price when available)
 *
 * When limit_price differs from the effective price, display both
 * with explicit labels so the user can see the discrepancy at a glance.
 */
function buildPriceCell(t) {
    const effectivePrice = t.price != null ? parseFloat(t.price) : null;
    const limitPrice = t.limit_price != null ? parseFloat(t.limit_price) : null;

    if (limitPrice != null && effectivePrice != null && Math.abs(limitPrice - effectivePrice) >= 0.005) {
        // Agent's limit price differs from actual execution — show both
        return `<span title="Actual execution price">${formatCurrency(effectivePrice)}</span>`
             + `<br><span class="text-dim text-small" title="Agent requested limit price">`
             + `Limit ${formatCurrency(limitPrice)}</span>`;
    }
    return effectivePrice != null ? formatCurrency(effectivePrice) : "—";
}

export function renderJournal(trades, tableBody) {
    if (!tableBody) return;

    tableBody.innerHTML = "";
    if (trades.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="10" class="text-center text-dim py-4">Trade Journal is empty.</td></tr>';
        return;
    }

    // Update header to match new layout
    const thead = tableBody.closest("table")?.querySelector("thead tr");
    if (thead) {
        thead.innerHTML = `
            <th>ID</th>
            <th>Time</th>
            <th>Asset</th>
            <th>Trade</th>
            <th>Status</th>
            <th class="text-right">Qty</th>
            <th class="text-right">Price</th>
            <th class="text-right">P&L</th>
            <th>Regime</th>
            <th>Reasoning</th>
        `;
    }

    trades.forEach(t => {
        const tr = document.createElement("tr");
        const ts = formatTime(t.timestamp, true);

        // P&L
        let pnlStr = "—";
        let pnlClass = "zero";
        if (t.realized_pnl !== null) {
            const pnl = parseFloat(t.realized_pnl);
            pnlStr = (pnl >= 0 ? "+" : "") + formatCurrency(pnl);
            pnlClass = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "zero";
        }

        // Option detection
        const isOption = !!(t.option_type || t.option_id);

        // ── Asset cell: ticker + optional option badge + detail line ──
        let optBadge = "";
        let detailLine = "";
        if (isOption && t.option_type) {
            const cls = t.option_type.toLowerCase() === "call" ? "opt-call" : "opt-put";
            optBadge = `<span class="badge-sm ${cls}">${t.option_type.toUpperCase()}</span>`;
            const parts = [];
            if (t.strike) parts.push(`$${parseFloat(t.strike).toFixed(0)}`);
            if (t.expiration) parts.push(t.expiration);
            detailLine = parts.length
                ? `<span class="journal-detail-line">${escapeHtml(parts.join(" · "))}</span>`
                : "";
        }

        // e.g. "OPEN LONG", "CLOSE SHORT", "STOP_LOSS LONG"
        const actionLbl = t.action || "";
        const dirLbl = t.direction || "";
        const dirCls = dirLbl === "SHORT" ? "trade-short" : "trade-long";
        const actionCls = actionLbl.toLowerCase().replace("_", "-");
        
        let tradeLabel = `${actionLbl} ${dirLbl}`;
        if (isOption) {
            if (actionLbl === "OPEN" && dirLbl === "LONG") tradeLabel = "BTO";
            else if (actionLbl === "OPEN" && dirLbl === "SHORT") tradeLabel = "STO";
            else if (actionLbl === "CLOSE" && dirLbl === "LONG") tradeLabel = "STC";
            else if (actionLbl === "CLOSE" && dirLbl === "SHORT") tradeLabel = "BTC";
            else if (actionLbl === "STOP_LOSS") tradeLabel = dirLbl === "LONG" ? "STC (SL)" : "BTC (SL)";
            else if (actionLbl === "TAKE_PROFIT") tradeLabel = dirLbl === "LONG" ? "STC (TP)" : "BTC (TP)";
        } else {
            if (actionLbl === "OPEN" && dirLbl === "LONG") tradeLabel = "BUY";
            else if (actionLbl === "OPEN" && dirLbl === "SHORT") tradeLabel = "SELL SHORT";
            else if (actionLbl === "CLOSE" && dirLbl === "LONG") tradeLabel = "SELL";
            else if (actionLbl === "CLOSE" && dirLbl === "SHORT") tradeLabel = "BUY COVER";
            else if (actionLbl === "STOP_LOSS") tradeLabel = "STOP LOSS";
            else if (actionLbl === "TAKE_PROFIT") tradeLabel = "TAKE PROFIT";
        }

        // Quantity
        const qtyDisplay = isOption
            ? Math.round(t.quantity).toString()
            : (Math.round(t.quantity * 10000) / 10000).toString();

        // ── Status cell: compact dot + label ──
        const orderStatus = t.order_status || "FILLED";
        let statusDot, statusColor, statusLabel;
        switch (orderStatus) {
            case "FILLED":
                statusDot = "●";
                statusColor = "var(--emerald)";
                statusLabel = "Filled";
                break;
            case "PENDING":
                statusDot = "◐";
                statusColor = "var(--amber)";
                statusLabel = "Pending";
                break;
            case "CANCELLED":
                statusDot = "○";
                statusColor = "var(--text-dim)";
                statusLabel = "Cancelled";
                break;
            case "REJECTED":
                statusDot = "✕";
                statusColor = "var(--crimson)";
                statusLabel = "Rejected";
                break;
            case "FAILED":
                statusDot = "✕";
                statusColor = "var(--crimson)";
                statusLabel = "Failed";
                break;
            default:
                statusDot = "●";
                statusColor = "var(--text-dim)";
                statusLabel = orderStatus;
        }
        const statusCell = `<span class="journal-status" style="color:${statusColor}" title="${orderStatus}">${statusDot} ${statusLabel}</span>`;

        // Click handler for session drawer
        const hasSession = !!t.session_id;
        if (hasSession) {
            tr.style.cursor = "pointer";
            tr.setAttribute("data-session-id", t.session_id);
            tr.addEventListener("click", () => {
                if (window.openSessionDrawer) {
                    window.openSessionDrawer(t.session_id);
                }
            });
        }

        const linkIcon = hasSession
            ? `<i class="fa-solid fa-arrow-up-right-from-square" style="font-size:0.55rem;margin-left:0.3rem;opacity:0.35" title="View cycle"></i>`
            : "";

        tr.innerHTML = `
            <td class="font-mono text-dim journal-cell-id">${t.id}${linkIcon}</td>
            <td class="text-dim text-small journal-cell-ts">${ts}</td>
            <td class="journal-cell-asset">
                <div class="journal-asset-row">
                    <span class="journal-ticker">${escapeHtml(t.ticker)}</span>
                    ${optBadge}
                </div>
                ${detailLine}
            </td>
            <td>
                <span class="journal-trade-badge ${dirCls} ${actionCls}">
                    ${tradeLabel}
                </span>
            </td>
            <td>${statusCell}</td>
            <td class="text-right font-mono">${qtyDisplay}</td>
            <td class="text-right font-mono">${buildPriceCell(t)}</td>
            <td class="text-right pnl-badge ${pnlClass}">${pnlStr}</td>
            <td class="text-dim font-mono text-small">${escapeHtml(t.regime)}</td>
            <td class="text-dim text-small journal-cell-reasoning" title="${escapeHtml(t.reasoning)}">${escapeHtml(t.reasoning)}</td>
        `;
        tableBody.appendChild(tr);
    });
}
