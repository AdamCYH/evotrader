import {
    escapeHtml,
    formatCurrency,
    getDisplayTicker,
    setDisplayTicker,
} from "./utils.js";

/**
 * Render every permitted instrument, traded symbol first.
 *
 * The console used to show only the primary ticker, so a bearish vehicle added
 * in settings was invisible here — the operator had no way to see what the agent
 * was permitted to touch. Falls back to the single ticker when an older backend
 * does not send the list.
 */
function renderPermittedTickers(el, local) {
    const primary = local.ticker || "";
    const list = Array.isArray(local.tickers) && local.tickers.length
        ? local.tickers
        : (primary ? [primary] : []);

    if (!list.length) {
        el.textContent = "—";
        return;
    }

    el.replaceChildren(...list.map((symbol) => {
        const chip = document.createElement("span");
        chip.className = symbol === primary ? "tkr tkr-primary" : "tkr tkr-alt";
        chip.textContent = symbol;
        return chip;
    }));
}


/**
 * Render portfolio cards & top badges
 */
export function renderPortfolio(data, elements) {
    const {
        badgeTicker,
        badgeVersion,
        badgeModeText,
        badgeMode,
        brokerTotal,
        brokerCash,
        brokerBp,
        todayPnl,
        todayTrades,
        lossStreak
    } = elements;

    // Badges updates
    if (badgeTicker) renderPermittedTickers(badgeTicker, data.local);
    // Cache the PRIMARY only — display fallbacks elsewhere mean "the instrument
    // we trade", not "any symbol we are allowed to touch".
    setDisplayTicker(data.local.ticker);
    if (badgeVersion) badgeVersion.textContent = data.local.algo_version;

    if (badgeMode && badgeModeText) {
        if (data.local.dry_run) {
            badgeModeText.textContent = "PAPER TRADING";
            badgeMode.className = "badge paper-mode";
        } else {
            badgeModeText.textContent = "LIVE TRADING";
            badgeMode.className = "badge live-mode";
        }
    }

    // Broker figures
    if (brokerTotal) {
        const val = formatCurrency(data.broker.total_value);
        brokerTotal.textContent = val;
        brokerTotal.title = val;
    }
    if (brokerCash) {
        const val = formatCurrency(data.broker.cash);
        brokerCash.textContent = val;
        brokerCash.title = val;
    }
    if (brokerBp) {
        const val = formatCurrency(data.broker.buying_power);
        brokerBp.textContent = val;
        brokerBp.title = val;
    }

    // Performance statistics
    if (todayPnl) {
        const pnl = parseFloat(data.local.period_pnl ?? data.local.today_pnl ?? 0);
        const val = (pnl >= 0 ? "+" : "") + formatCurrency(pnl);
        todayPnl.textContent = val;
        todayPnl.title = val;
        todayPnl.className = "metric-value " + (pnl > 0 ? "text-emerald" : pnl < 0 ? "text-crimson" : "text-white");
    }

    if (todayTrades) {
        const val = data.local.period_trades ?? data.local.trades_count;
        todayTrades.textContent = val;
        todayTrades.title = val;
    }
    
    // No-trade streak badge
    const noTradeStreak = document.getElementById("no-trade-streak");
    if (noTradeStreak) {
        const streak = data.local.consecutive_no_trades ?? 0;
        if (streak > 0) {
            noTradeStreak.textContent = `🧊 ${streak} no-trade streak`;
            noTradeStreak.title = `${streak} consecutive successful cycle${streak > 1 ? "s" : ""} without trading`;
            noTradeStreak.style.display = "inline-block";
        } else {
            noTradeStreak.style.display = "none";
        }
    }
    
    // Win / Loss record as primary metric
    const winLossCount = document.getElementById("win-loss-count");
    if (winLossCount) {
        const wins = data.local.period_wins ?? 0;
        const losses = data.local.period_losses ?? 0;
        const total = wins + losses;

        if (total > 0) {
            const winRate = ((wins / total) * 100).toFixed(0);
            winLossCount.textContent = `${wins}W / ${losses}L`;
            winLossCount.title = `Win Rate: ${winRate}% (${wins} wins, ${losses} losses)`;
            winLossCount.className = "metric-value " + (wins > losses ? "text-emerald" : wins < losses ? "text-crimson" : "text-white");
        } else {
            winLossCount.textContent = "—";
            winLossCount.title = "No closed trades this period";
            winLossCount.className = "metric-value text-dim";
        }
    }

    // Loss streak badge — only visible when streak > 0
    if (lossStreak) {
        const streak = data.local.consecutive_losses;
        if (streak > 0) {
            lossStreak.textContent = `🔥 ${streak} loss streak`;
            lossStreak.title = `${streak} consecutive losing trade${streak > 1 ? "s" : ""}`;
            lossStreak.style.display = "inline-block";
        } else {
            lossStreak.style.display = "none";
        }
    }
}

/**
 * Render position alignment comparing database metrics with live broker holds
 */
export function renderPositions(portfolioData, trades, tableBody, syncStatusEl) {
    if (!tableBody || !syncStatusEl) return;

    const brokerPositions = portfolioData.broker.positions || [];
    const ticker = portfolioData.local.ticker;

    // Identify active open trades in local db (excluding cancelled/rejected orders)
    const openDbTrades = trades.filter(t => 
        t.action === "OPEN" && 
        (!t.order_status || t.order_status === "FILLED") &&
        !trades.some(c => c.related_trade_id === t.id && ["CLOSE", "STOP_LOSS", "TAKE_PROFIT"].includes(c.action))
    );

    tableBody.innerHTML = "";

    // 1. Separate database open trades into equity and option positions
    const dbEquityTrades = openDbTrades.filter(t => !t.option_id);
    const dbOptionTrades = openDbTrades.filter(t => t.option_id);

    // Group DB option trades by option_id
    const dbOptionsGrouped = {};
    dbOptionTrades.forEach(t => {
        if (!dbOptionsGrouped[t.option_id]) {
            dbOptionsGrouped[t.option_id] = [];
        }
        dbOptionsGrouped[t.option_id].push(t);
    });

    // 2. Separate broker positions into equity and option positions
    const brokerEquityPos = brokerPositions.find(p => p.asset_type === "EQUITY" || (!p.asset_type && p.symbol === ticker));
    const brokerOptionPos = brokerPositions.filter(p => p.asset_type === "OPTION" || p.option_id);

    // Helper: compute signed quantity from trade direction.
    // Broker stores shorts as negative; DB stores positive quantity + direction field.
    function signedQty(trade) {
        const qty = parseFloat(trade.quantity);
        return trade.direction === "SHORT" ? -qty : qty;
    }

    // 3. Build unified list of assets to reconcile
    const assets = [];

    // Always include the primary equity ticker
    const dbEquityQty = dbEquityTrades.reduce((acc, t) => acc + signedQty(t), 0.0);
    const brokerEquityQty = brokerEquityPos ? parseFloat(brokerEquityPos.quantity) : 0.0;
    const brokerEquityCost = brokerEquityPos ? parseFloat(brokerEquityPos.avg_price) : 0.0;
    const firstDbEquityPrice = dbEquityTrades.length > 0 ? parseFloat(dbEquityTrades[0].price) : 0.0;

    assets.push({
        name: ticker,
        assetType: "EQUITY",
        dbQty: dbEquityQty,
        brokerQty: brokerEquityQty,
        brokerCost: brokerEquityCost,
        dbAvgPrice: firstDbEquityPrice,
    });

    // Collect all option IDs from either DB or broker
    const allOptionIds = new Set([
        ...Object.keys(dbOptionsGrouped),
        ...brokerOptionPos.map(p => p.option_id)
    ]);

    allOptionIds.forEach(optId => {
        const dbTrades = dbOptionsGrouped[optId] || [];
        const dbOptQty = dbTrades.reduce((acc, t) => acc + signedQty(t), 0.0);
        const bOpt = brokerOptionPos.find(p => p.option_id === optId);
        const brokerOptQty = bOpt ? parseFloat(bOpt.quantity) : 0.0;
        const brokerOptCost = bOpt ? parseFloat(bOpt.avg_price) : 0.0;
        const firstDbOptPrice = dbTrades.length > 0 ? parseFloat(dbTrades[0].price) : 0.0;

        // Try to construct a readable option name
        let optName = optId.substring(0, 12);
        if (bOpt && bOpt.strike && bOpt.expiration && bOpt.option_type) {
            optName = `${ticker} ${bOpt.expiration} ${bOpt.option_type.toUpperCase()} $${bOpt.strike}`;
        } else if (dbTrades.length > 0) {
            const t = dbTrades[0];
            if (t.strike && t.expiration && t.option_type) {
                optName = `${ticker} ${t.expiration} ${t.option_type.toUpperCase()} $${t.strike}`;
            }
        }

        assets.push({
            name: optName,
            assetType: "OPTION",
            optionId: optId,
            dbQty: dbOptQty,
            brokerQty: brokerOptQty,
            brokerCost: brokerOptCost,
            dbAvgPrice: firstDbOptPrice,
        });
    });

    // Check if everything is aligned
    let overallAligned = true;
    let rowCount = 0;

    assets.forEach(asset => {
        const isAligned = Math.abs(asset.dbQty - asset.brokerQty) < 1e-4;
        if (!isAligned) {
            overallAligned = false;
        }

        // Render row if either side has a non-zero position, or if there is a mismatch
        const hasPosition = Math.abs(asset.dbQty) > 1e-6 || Math.abs(asset.brokerQty) > 1e-6;
        if (hasPosition || !isAligned) {
            rowCount++;
            const tr = document.createElement("tr");

            // Format quantity display: show absolute value with Short prefix for negatives
            function fmtQty(qty, avgPrice) {
                if (Math.abs(qty) < 1e-6) {
                    return '<span class="text-dim font-mono">0.00</span>';
                }
                const absQty = Math.abs(qty).toFixed(2);
                const prefix = qty < 0 ? '<span class="text-crimson">Short</span> ' : '';
                return `${prefix}${absQty} <span class="text-dim text-small">(${formatCurrency(avgPrice)})</span>`;
            }

            const dbDisplay = fmtQty(asset.dbQty, asset.dbAvgPrice);
            const brokerDisplay = fmtQty(asset.brokerQty, asset.brokerCost);

            tr.innerHTML = `
                <td class="text-white font-mono font-bold">${escapeHtml(asset.name)}</td>
                <td class="text-right font-mono">${dbDisplay}</td>
                <td class="text-right font-mono">${brokerDisplay}</td>
                <td class="text-right">
                    <span class="sync-badge ${isAligned ? 'in-sync' : 'mismatch'}">
                        ${isAligned ? '<i class="fa-solid fa-circle-check"></i> Clean' : '<i class="fa-solid fa-triangle-exclamation"></i> Drift'}
                    </span>
                </td>
            `;
            tableBody.appendChild(tr);
        }
    });

    if (rowCount === 0) {
        tableBody.innerHTML = `
            <tr>
                <td colspan="4" class="text-center text-dim text-small py-4">No positions currently held.</td>
            </tr>
        `;
    }

    if (overallAligned) {
        syncStatusEl.innerHTML = '<i class="fa-solid fa-circle-check"></i> In Sync';
        syncStatusEl.className = "sync-badge in-sync";
    } else {
        syncStatusEl.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Mismatch';
        syncStatusEl.className = "sync-badge mismatch";
    }
}

/**
 * Render trade approval request card with interactive callback buttons
 */
export function renderPendingOrder(order, approvalPanel, approvalContent, onResolve) {
    if (!approvalPanel || !approvalContent) return;

    if (!order) {
        approvalPanel.classList.add("hidden");
        return;
    }

    const args = order.args || {};
    // Never name a symbol we were not given: a pending order labelled with the
    // wrong ticker reads as a real order in an instrument you are not trading.
    const symbol = args.ticker || args.symbol || getDisplayTicker();
    const side = args.direction || args.side || "LONG";
    const qty = args.quantity || 0;
    const price = args.price || args.limit_price || "--";
    const reasoning = args.reasoning || "Awaiting execution validation...";
    const orderId = order.id;

    approvalPanel.classList.remove("hidden");

    approvalContent.innerHTML = `
        <div class="approval-order-line">
            <span class="journal-action open">${side.toUpperCase()}</span>
            <span>${qty} shares of <strong class="text-white">${symbol}</strong> @ $${price}</span>
        </div>
        <div class="approval-reasoning font-mono text-dim text-small">
            ${escapeHtml(reasoning)}
        </div>
        <div class="approval-actions">
            <button id="btn-approve-order" class="btn btn-approve">
                <i class="fa-solid fa-circle-check"></i> Approve Order
            </button>
            <button id="btn-reject-order" class="btn btn-reject">
                <i class="fa-solid fa-circle-xmark"></i> Reject Order
            </button>
        </div>
    `;

    // Bind events
    document.getElementById("btn-approve-order").addEventListener("click", () => onResolve(orderId, "approve"));
    document.getElementById("btn-reject-order").addEventListener("click", () => onResolve(orderId, "reject"));

    // Play notification chime safely
    try {
        const audio = new Audio("https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg");
        audio.volume = 0.15;
        audio.play();
    } catch (e) {
        // Safe fallback if browser security blocks immediate audio autoplay
    }
}

/**
 * Render unified Positions table combining holdings and sync status.
 * 
 * @param {Object} portfolioData - API response with broker positions
 * @param {Array} trades - All trades for position alignment check
 * @param {HTMLElement} holdingsTableBody - The tbody element
 * @param {HTMLElement} totalPnlBadge - Badge to show total unrealized P&L
 * @param {HTMLElement} syncStatusEl - Badge to show overall sync status
 */
export function renderHoldings(portfolioData, trades, holdingsTableBody, totalPnlBadge, syncStatusEl) {
    if (!holdingsTableBody) return;

    const brokerPositions = portfolioData.broker.positions || [];
    const ticker = portfolioData.local.ticker;
    holdingsTableBody.innerHTML = "";

    // ── Build position alignment map (from renderPositions logic) ──
    const alignmentMap = {};
    if (trades && trades.length > 0) {
        const openDbTrades = trades.filter(t =>
            t.action === "OPEN" &&
            (!t.order_status || t.order_status === "FILLED") &&
            !trades.some(c => c.related_trade_id === t.id && ["CLOSE", "STOP_LOSS", "TAKE_PROFIT"].includes(c.action))
        );
        function signedQty(trade) {
            const qty = parseFloat(trade.quantity);
            return trade.direction === "SHORT" ? -qty : qty;
        }

        // Equity alignment
        const dbEquityTrades = openDbTrades.filter(t => !t.option_id);
        const dbEquityQty = dbEquityTrades.reduce((acc, t) => acc + signedQty(t), 0.0);
        const brokerEquityPos = brokerPositions.find(p => p.asset_type === "EQUITY" || (!p.asset_type && p.symbol === ticker));
        const brokerEquityQty = brokerEquityPos ? parseFloat(brokerEquityPos.quantity) : 0.0;
        alignmentMap["EQUITY_" + ticker] = Math.abs(dbEquityQty - brokerEquityQty) < 1e-4;

        // Option alignment
        const dbOptionTrades = openDbTrades.filter(t => t.option_id);
        const dbOptionsGrouped = {};
        dbOptionTrades.forEach(t => {
            if (!dbOptionsGrouped[t.option_id]) dbOptionsGrouped[t.option_id] = [];
            dbOptionsGrouped[t.option_id].push(t);
        });
        const brokerOptionPos = brokerPositions.filter(p => p.asset_type === "OPTION" || p.option_id);
        const allOptionIds = new Set([
            ...Object.keys(dbOptionsGrouped),
            ...brokerOptionPos.map(p => p.option_id)
        ]);
        allOptionIds.forEach(optId => {
            const dbTrades = dbOptionsGrouped[optId] || [];
            const dbOptQty = dbTrades.reduce((acc, t) => acc + signedQty(t), 0.0);
            const bOpt = brokerOptionPos.find(p => p.option_id === optId);
            const brokerOptQty = bOpt ? parseFloat(bOpt.quantity) : 0.0;
            alignmentMap["OPTION_" + optId] = Math.abs(dbOptQty - brokerOptQty) < 1e-4;
        });
    }

    if (brokerPositions.length === 0) {
        holdingsTableBody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-dim text-small py-4">No active holdings found.</td>
            </tr>
        `;
        if (totalPnlBadge) {
            totalPnlBadge.textContent = "$0.00";
            totalPnlBadge.className = "sync-badge in-sync";
        }
        _updateSyncBadge(syncStatusEl, alignmentMap);
        return;
    }

    // Update table header
    const thead = holdingsTableBody.closest("table")?.querySelector("thead tr");
    if (thead) {
        thead.innerHTML = `
            <th>Position</th>
            <th class="text-right">Avg Cost</th>
            <th class="text-right">Market</th>
            <th class="text-right">Value</th>
            <th class="text-right">P&L</th>
            <th class="text-right">Sync</th>
        `;
    }

    let totalUnrealizedPnl = 0.0;

    brokerPositions.forEach(p => {
        const qty = parseFloat(p.quantity || 0);
        if (qty === 0) return;

        const avgPrice = parseFloat(p.avg_price || 0);
        const currentPrice = parseFloat(p.current_price || avgPrice);
        const unrealizedPnl = parseFloat(p.unrealized_pnl || 0.0);
        totalUnrealizedPnl += unrealizedPnl;

        const assetType = p.asset_type || "EQUITY";
        const isOption = assetType === "OPTION";
        const isShort = qty < 0;
        const absQty = Math.abs(qty);
        const symbol = p.symbol || "—";

        // ── Position cell ──
        let badges = "";
        if (isShort) {
            badges += `<span class="badge-sm dir-short">Short</span>`;
        }
        if (isOption && p.option_type) {
            const cls = p.option_type.toLowerCase() === "call" ? "opt-call" : "opt-put";
            badges += `<span class="badge-sm ${cls}">${p.option_type.toUpperCase()}</span>`;
        }

        let qtyStr = isOption
            ? `${Math.round(absQty)} contract${Math.round(absQty) !== 1 ? "s" : ""}`
            : `${absQty.toFixed(4).replace(/\.?0+$/, '')} shares`;
        let optDetail = "";
        if (isOption) {
            const parts = [];
            if (p.strike) parts.push(`$${parseFloat(p.strike).toFixed(0)} strike`);
            if (p.expiration) parts.push(`exp ${p.expiration}`);
            optDetail = parts.length ? ` · ${parts.join(" · ")}` : "";
        }

        // P&L
        const mult = isOption ? 100.0 : 1.0;
        const totalValue = qty * currentPrice * mult;
        const costBasis = qty * avgPrice * mult;
        const pnlPct = Math.abs(costBasis) > 0 ? (unrealizedPnl / Math.abs(costBasis)) * 100 : 0.0;
        const pnlSign = unrealizedPnl >= 0 ? "+" : "";
        const pnlClass = unrealizedPnl > 0 ? "holdings-pnl-pos" : unrealizedPnl < 0 ? "holdings-pnl-neg" : "";

        // Sync status
        const alignKey = isOption ? "OPTION_" + p.option_id : "EQUITY_" + symbol;
        const isAligned = alignmentMap[alignKey] !== false; // default to aligned if unknown

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td class="holdings-position-cell">
                <div class="holdings-position-row">
                    <span class="holdings-ticker">${escapeHtml(symbol)}</span>
                    ${badges}
                </div>
                <span class="holdings-position-detail">${qtyStr}${optDetail}</span>
            </td>
            <td class="text-right font-mono holdings-price-cell">${formatCurrency(avgPrice)}</td>
            <td class="text-right font-mono holdings-price-cell">${formatCurrency(currentPrice)}</td>
            <td class="text-right font-mono holdings-value-cell">${formatCurrency(totalValue)}</td>
            <td class="text-right">
                <div class="holdings-pnl-inner ${pnlClass}">
                    <span class="holdings-pnl-amount">${pnlSign}${formatCurrency(unrealizedPnl)}</span>
                    <span class="holdings-pnl-pct">${pnlSign}${pnlPct.toFixed(2)}%</span>
                </div>
            </td>
            <td class="text-right">
                <span class="sync-badge ${isAligned ? 'in-sync' : 'mismatch'}" style="font-size: 0.7rem; padding: 0.15rem 0.4rem;">
                    ${isAligned ? '<i class="fa-solid fa-circle-check"></i>' : '<i class="fa-solid fa-triangle-exclamation"></i>'}
                </span>
            </td>
        `;
        holdingsTableBody.appendChild(tr);
    });

    if (totalPnlBadge) {
        const sign = totalUnrealizedPnl >= 0 ? "+" : "";
        totalPnlBadge.textContent = `${sign}${formatCurrency(totalUnrealizedPnl)}`;
        totalPnlBadge.className = "sync-badge " + (totalUnrealizedPnl > 0 ? "in-sync" : totalUnrealizedPnl < 0 ? "mismatch" : "neutral");
    }

    _updateSyncBadge(syncStatusEl, alignmentMap);
}

/**
 * Update the overall sync status badge based on alignment map.
 */
function _updateSyncBadge(syncStatusEl, alignmentMap) {
    if (!syncStatusEl) return;
    const allAligned = Object.values(alignmentMap).every(v => v);
    if (allAligned) {
        syncStatusEl.innerHTML = '<i class="fa-solid fa-circle-check"></i> In Sync';
        syncStatusEl.className = "sync-badge in-sync";
    } else {
        syncStatusEl.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Drift';
        syncStatusEl.className = "sync-badge mismatch";
    }
}
