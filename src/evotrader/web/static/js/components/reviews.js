import { escapeHtml, formatTime } from "./utils.js";

/**
 * Render evolution proposals list sidebar (code reviews + strategy proposals)
 */
export function renderReviewsList(reviews, activeReviewId, containerEl, onSelect) {
    containerEl.innerHTML = "";
    if (!reviews || reviews.length === 0) {
        containerEl.innerHTML = `<div class="empty-state text-dim text-center py-4">No evolution proposals found.</div>`;
        return;
    }
    
    reviews.forEach(review => {
        const item = document.createElement("div");
        const isActive = review.id === activeReviewId;
        item.className = `proposal-card ${isActive ? "active" : ""}`;
        item.dataset.id = review.id;
        
        // Type info
        const isStrategy = review.type === "strategy_proposal";
        const typeIcon = isStrategy ? "fa-bolt" : "fa-code";
        const typeLabel = isStrategy ? "Strategy" : "Code";
        const typeClass = isStrategy ? "type-strategy" : "type-code";

        // Status info
        const isApplied = review.status === "APPLIED" || review.status === "ACTIVE";
        const isRejected = review.status === "REJECTED";
        let statusClass, statusIcon, statusLabel;
        if (isApplied) {
            statusClass = "status-applied";
            statusIcon = "fa-circle-check";
            statusLabel = "Applied";
        } else if (isRejected) {
            statusClass = "status-rejected";
            statusIcon = "fa-circle-xmark";
            statusLabel = "Rejected";
        } else {
            statusClass = "status-pending";
            statusIcon = "fa-clock";
            statusLabel = "Pending";
        }

        // Format the title nicely — strip timestamps and underscores
        const displayTitle = _formatProposalTitle(review.title);
        
        // Date
        const dateStr = review.generated ? formatTime(review.generated, true) : "";
        
        item.innerHTML = `
            <div class="proposal-card__header">
                <span class="proposal-card__type ${typeClass}">
                    <i class="fa-solid ${typeIcon}"></i> ${typeLabel}
                </span>
                <span class="proposal-card__status ${statusClass}">
                    <i class="fa-solid ${statusIcon}"></i> ${statusLabel}
                </span>
            </div>
            <div class="proposal-card__title" title="${escapeHtml(review.title)}">
                ${escapeHtml(displayTitle)}
            </div>
            ${dateStr ? `<div class="proposal-card__meta">
                <i class="fa-regular fa-calendar"></i> ${dateStr}
            </div>` : ""}
        `;
        
        item.addEventListener("click", () => onSelect(review.id));
        containerEl.appendChild(item);
    });
}

/**
 * Format a raw proposal title into a human-readable form.
 * e.g. "20260715_210358_intraday_indicator_staleness" → "Intraday Indicator Staleness"
 * e.g. "event_window_timing" → "Event Window Timing"
 */
function _formatProposalTitle(raw) {
    if (!raw) return "Untitled";
    // Strip leading timestamp patterns like "20260715_210358_"
    let cleaned = raw.replace(/^\d{8}_\d{6}_/, "");
    // Replace underscores with spaces and title-case
    return cleaned
        .replace(/_/g, " ")
        .replace(/\b\w/g, c => c.toUpperCase());
}
