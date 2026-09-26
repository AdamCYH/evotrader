import { escapeHtml } from "./utils.js";

/**
 * Render note files list grouped by author and audience:
 *   Trading Notes  — what YOU write for the trading agents (embedded in memory)
 *   Cycle Handoff  — what the trading agent writes for its own next cycle
 *   Evolution Notes — carry-forward for the self-evolution agent
 */
export function renderNotesFileList(files, activeFilename, containerEl, onSelect) {
    containerEl.innerHTML = "";
    if (!files || files.length === 0) {
        containerEl.innerHTML = `<div class="empty-state text-dim text-center py-4">No note files found.</div>`;
        return;
    }

    const handoffFiles = files.filter(f => f.source === "trading");
    const evolutionFiles = files.filter(f => f.source === "evolution");
    const tradingFiles = files.filter(f => f.source !== "evolution" && f.source !== "trading");

    // Helper to render an individual note card
    const renderCard = (file) => {
        const item = document.createElement("div");
        const isActive = file.filename === activeFilename;
        const isEvolution = file.source === "evolution";
        const isHandoff = file.source === "trading";
        item.className = `note-item-card glass-card ${isActive ? "active" : ""}`;
        item.dataset.filename = file.filename;
        item.dataset.source = file.source || "user";

        // Category Tag
        let catBadge = "";
        if (isHandoff) {
            catBadge = `<span class="badge badge-xs badge-cyan">Cycle Handoff</span>`;
        } else if (isEvolution) {
            const catLabel = (file.category && file.category !== "evolution")
                ? file.category.replace(/_/g, " ")
                : "Carry-Forward";
            catBadge = `<span class="badge badge-xs badge-purple-outline">${escapeHtml(catLabel)}</span>`;
        } else {
            const rawCat = file.category || "general";
            let catColorClass = "badge-secondary";
            let label = "General";
            if (rawCat === "instruction") { catColorClass = "badge-cyan"; label = "Instruction"; }
            else if (rawCat === "market_insight") { catColorClass = "badge-amber"; label = "Market Insight"; }
            else if (rawCat === "strategy_hint") { catColorClass = "badge-purple"; label = "Strategy Hint"; }
            else if (rawCat === "observation") { catColorClass = "badge-emerald"; label = "Observation"; }
            else if (rawCat !== "general") { label = rawCat.replace(/_/g, " "); }
            catBadge = `<span class="badge badge-xs ${catColorClass}">${escapeHtml(label)}</span>`;
        }

        // Priority badge
        let prioColorClass = "";
        let prioIcon = "";
        const prio = (file.priority || "normal").toLowerCase();
        if (prio === "critical") {
            prioColorClass = "badge-danger";
            prioIcon = '<i class="fa-solid fa-triangle-exclamation"></i> ';
        } else if (prio === "high") {
            prioColorClass = "badge-warning";
            prioIcon = '<i class="fa-solid fa-circle-exclamation"></i> ';
        } else if (prio === "normal") {
            prioColorClass = "badge-secondary";
        } else if (prio === "low") {
            prioColorClass = "badge-dim";
        }

        const sizeKb = file.size ? (file.size / 1024).toFixed(1) : "0.0";
        const snippetText = file.snippet || "";

        item.innerHTML = `
            <div class="note-card-header">
                <span class="note-card-title" title="${escapeHtml(file.filename)}">
                    <i class="fa-regular ${isHandoff ? 'fa-share-from-square text-cyan' : isEvolution ? 'fa-file-code text-purple' : 'fa-file-lines text-dim'}"></i>
                    <span>${escapeHtml(file.filename)}</span>
                </span>
            </div>
            <div class="note-card-snippet">${escapeHtml(snippetText)}</div>
            <div class="note-card-footer">
                <div class="note-card-tags" style="display: flex; gap: 0.35rem; align-items: center;">
                    ${catBadge}
                    ${prioColorClass ? `<span class="badge badge-xs ${prioColorClass}">${prioIcon}${escapeHtml(file.priority || "normal")}</span>` : ""}
                </div>
                <span class="note-card-size text-dim">${sizeKb} KB</span>
            </div>
        `;

        item.addEventListener("click", () => onSelect(file.filename, file.source));
        return item;
    };

    // Render Trading Notes Section
    if (tradingFiles.length > 0) {
        const header = document.createElement("div");
        header.className = "note-section-divider";
        header.innerHTML = `
            <span class="note-section-title">
                <i class="fa-solid fa-chart-line text-amber"></i> Trading Notes
                <span class="badge badge-xs badge-secondary" style="margin-left: 0.35rem;">${tradingFiles.length}</span>
            </span>
        `;
        containerEl.appendChild(header);
        tradingFiles.forEach(file => containerEl.appendChild(renderCard(file)));
    }

    // Render Cycle Handoff Section — written BY the trading agent, for its own
    // next cycle. Kept separate from the notes you write: different author,
    // rewritten every cycle, and injected into the prompt rather than searched.
    if (handoffFiles.length > 0) {
        const header = document.createElement("div");
        header.className = "note-section-divider";
        header.style.marginTop = "0.75rem";
        header.innerHTML = `
            <span class="note-section-title">
                <i class="fa-solid fa-share-from-square text-cyan"></i> Cycle Handoff
                <span class="badge badge-xs badge-cyan" style="margin-left: 0.35rem;">${handoffFiles.length}</span>
            </span>
        `;
        containerEl.appendChild(header);
        handoffFiles.forEach(file => containerEl.appendChild(renderCard(file)));
    }

    // Render Evolution Notes Section
    if (evolutionFiles.length > 0) {
        const header = document.createElement("div");
        header.className = "note-section-divider";
        header.style.marginTop = "0.75rem";
        header.innerHTML = `
            <span class="note-section-title">
                <i class="fa-solid fa-dna text-purple"></i> Evolution Notes
                <span class="badge badge-xs badge-purple-outline" style="margin-left: 0.35rem;">${evolutionFiles.length}</span>
            </span>
        `;
        containerEl.appendChild(header);
        evolutionFiles.forEach(file => containerEl.appendChild(renderCard(file)));
    }
}
