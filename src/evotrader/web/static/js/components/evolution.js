import { escapeHtml, formatTime, formatVersion } from "./utils.js";

/**
 * Render Self-Evolution audit items
 */
export function renderEvolution(history, containerEl, onSelect) {
    if (!containerEl) return;

    containerEl.innerHTML = "";
    if (history.length === 0) {
        containerEl.innerHTML = `
            <div class="evolution-item empty-evolution">
                <span class="text-dim text-small">No algorithm changes or instruction updates recorded.</span>
            </div>
        `;
        return;
    }

    history.forEach(item => {
        const div = document.createElement("div");
        div.className = "evolution-item clickable";

        const ts = formatTime(item.timestamp, true);
        const changeType = item.change_type || "ALGO_PARAMS";
        const status = item.status || "PROPOSED";

        const oldVer = formatVersion(item.old_version, 20);
        const newVer = formatVersion(item.new_version, 20);
        const tooltip = `${item.old_version || ""} ➔ ${item.new_version || ""}`;

        div.innerHTML = `
            <div class="evolution-meta" title="${escapeHtml(tooltip)}">
                <span class="evolution-change">${escapeHtml(changeType.replace("_", " "))}: ${escapeHtml(item.target_component)}</span>
                <span class="evolution-ts font-mono">${ts} │ ${escapeHtml(oldVer)} ➔ ${escapeHtml(newVer)}</span>
            </div>
            <span class="evolution-badge ${status.toLowerCase()}">${escapeHtml(status)}</span>
        `;
        
        div.addEventListener("click", () => {
            if (onSelect) onSelect(item);
        });
        
        containerEl.appendChild(div);
    });
}

