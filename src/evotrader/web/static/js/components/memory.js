import { escapeHtml, formatTime } from "./utils.js";

/**
 * Render user notes embedded in ChromaDB
 */
export function renderMemoryNotesTable(notes, tableBodyEl) {
    tableBodyEl.innerHTML = "";
    if (!notes || notes.length === 0) {
        tableBodyEl.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-dim text-small py-4">No embedded notes found.</td>
            </tr>
        `;
        return;
    }
    
    notes.sort((a, b) => {
        const ta = a.metadata?.timestamp ? new Date(a.metadata.timestamp).getTime() : 0;
        const tb = b.metadata?.timestamp ? new Date(b.metadata.timestamp).getTime() : 0;
        return tb - ta;
    });
    
    notes.forEach(note => {
        const tr = document.createElement("tr");
        tr.dataset.item = JSON.stringify(note);
        const meta = note.metadata || {};
        
        let sourceVal = meta.source || "file";
        let categoryVal = meta.category || "general";
        let priorityVal = meta.priority || "normal";
        let timeVal = meta.timestamp ? formatTime(meta.timestamp, true) : "—";
        
        // Format category badge
        let catColorClass = "badge-general";
        if (categoryVal === "instruction") catColorClass = "badge-cyan";
        else if (categoryVal === "market_insight") catColorClass = "badge-amber";
        else if (categoryVal === "strategy_hint") catColorClass = "badge-purple";
        else if (categoryVal === "observation") catColorClass = "badge-emerald";
        else if (categoryVal === "evolution") catColorClass = "badge-evolution";
        
        // Format priority badge
        let prioColorClass = "";
        let prioIcon = "";
        if (priorityVal === "critical") {
            prioColorClass = "badge-danger";
            prioIcon = '<i class="fa-solid fa-triangle-exclamation"></i>';
        } else if (priorityVal === "high") {
            prioColorClass = "badge-warning";
            prioIcon = '<i class="fa-solid fa-circle-exclamation"></i>';
        } else if (priorityVal === "normal") {
            prioColorClass = "badge-secondary";
        } else if (priorityVal === "low") {
            prioColorClass = "badge-dim";
        }
        
        tr.innerHTML = `
            <td title="${escapeHtml(note.id)}">${escapeHtml(note.id)}</td>
            <td title="${escapeHtml(note.text)}" class="text-truncate" style="max-width: 400px;">${escapeHtml(note.text)}</td>
            <td><span class="badge badge-xs">${escapeHtml(sourceVal)}</span></td>
            <td><span class="badge badge-xs ${catColorClass}">${escapeHtml(categoryVal)}</span></td>
            <td>${prioColorClass ? `<span class="badge badge-xs ${prioColorClass}">${prioIcon}${priorityVal}</span>` : `<span class="text-dim">—</span>`}</td>
            <td><span class="text-dim text-small">${escapeHtml(timeVal)}</span></td>
        `;
        tableBodyEl.appendChild(tr);
    });
}

/**
 * Render trade experiences embedded in ChromaDB
 */
export function renderMemoryTradesTable(trades, tableBodyEl) {
    tableBodyEl.innerHTML = "";
    if (!trades || trades.length === 0) {
        tableBodyEl.innerHTML = `
            <tr>
                <td colspan="5" class="text-center text-dim text-small py-4">No embedded experiences found.</td>
            </tr>
        `;
        return;
    }
    
    trades.forEach(trade => {
        const tr = document.createElement("tr");
        tr.dataset.item = JSON.stringify(trade);
        const meta = trade.metadata || {};
        
        const pnl = parseFloat(meta.outcome_pnl || 0);
        let pnlClass = "";
        let pnlText = "--";
        if (pnl > 0) {
            pnlClass = "text-emerald";
            pnlText = `+$${pnl.toFixed(2)}`;
        } else if (pnl < 0) {
            pnlClass = "text-crimson";
            pnlText = `-$${Math.abs(pnl).toFixed(2)}`;
        } else if (meta.outcome_pnl !== undefined) {
            pnlText = "$0.00";
        }
        
        const hybrid = parseFloat(meta.hybrid_score || 0);
        
        tr.innerHTML = `
            <td title="${escapeHtml(trade.id)}">${escapeHtml(trade.id)}</td>
            <td title="${escapeHtml(trade.text)}" class="text-truncate" style="max-width: 450px;">${escapeHtml(trade.text)}</td>
            <td class="text-right ${pnlClass}"><strong>${escapeHtml(pnlText)}</strong></td>
            <td><span class="badge badge-xs">${escapeHtml(meta.regime || "unknown")}</span></td>
            <td><span class="badge badge-xs badge-outline">${hybrid.toFixed(3)}</span></td>
        `;
        tableBodyEl.appendChild(tr);
    });
}
