import { escapeHtml } from "./utils.js";

/**
 * Render algorithm versions list in the sidebar
 */
export function renderAlgorithmsList(versions, selectedVersion, containerEl, onSelect) {
    containerEl.innerHTML = "";
    if (!versions || versions.length === 0) {
        containerEl.innerHTML = `<div class="empty-state text-dim text-center py-4">No algorithms found.</div>`;
        return;
    }

    // Create a copy and reverse so the newest versions appear at the top
    const sortedVersions = [...versions].reverse();
    sortedVersions.forEach(algo => {
        const item = document.createElement("div");
        const isSelected = algo.version === selectedVersion;
        item.className = `note-item-card glass-card ${isSelected ? "active" : ""}`;
        item.dataset.version = algo.version;

        const statusBadge = algo.is_active 
            ? `<span class="badge badge-xs badge-cyan">Active</span>` 
            : (algo.status === "proposed" || algo.status === "PENDING_REVIEW") 
                ? `<span class="badge badge-xs badge-purple" style="margin-left: 0.5rem;"><i class="fa-solid fa-code-pull-request"></i> Proposed</span>` 
                : "";

        item.innerHTML = `
            <div class="note-card-header">
                <span class="note-card-title font-mono" style="font-size: 0.85rem;">${escapeHtml(algo.version)}</span>
                ${statusBadge}
            </div>
            <div class="note-card-snippet" style="font-size: 0.75rem;">${escapeHtml(algo.name || algo.description || "")}</div>
        `;

        item.addEventListener("click", () => onSelect(algo));
        containerEl.appendChild(item);
    });
}
