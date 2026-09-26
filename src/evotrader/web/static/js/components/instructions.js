import { escapeHtml } from "./utils.js";

/**
 * Render system instructions agent list in the sidebar
 */
export function renderInstructionsAgentList(agents, activeAgentName, activeVersion, containerEl, onSelect) {
    containerEl.innerHTML = "";
    if (!agents || agents.length === 0) {
        containerEl.innerHTML = `<div class="empty-state text-dim text-center py-4">No agents found.</div>`;
        return;
    }

    // Define the sequence of execution for the agents
    const executionOrder = [
        "orchestrator",
        "market_intel",
        "market_intelligence",
        "news_sentiment",
        "strategy",
        "risk_manager",
        "executor",
        "execution",
        "evolution"
    ];

    const sortedAgents = [...agents].sort((a, b) => {
        let indexA = executionOrder.indexOf(a.name);
        let indexB = executionOrder.indexOf(b.name);
        
        if (indexA === -1) indexA = 999;
        if (indexB === -1) indexB = 999;
        
        if (indexA !== indexB) {
            return indexA - indexB;
        }
        return a.name.localeCompare(b.name);
    });

    sortedAgents.forEach(agent => {
        // Create the main agent container
        const agentContainer = document.createElement("div");
        const isAgentActive = (agent.name === activeAgentName);
        agentContainer.className = `agent-accordion-group ${isAgentActive ? "active" : ""}`;

        // Agent Header (clickable to expand/collapse)
        const agentHeader = document.createElement("div");
        agentHeader.className = `note-item-card glass-card agent-header-card ${isAgentActive ? "active" : ""}`;
        
        const proposedIndicator = agent.has_proposed
            ? `<span class="badge badge-xs badge-purple" style="margin-left: 0.5rem;"><i class="fa-solid fa-code-pull-request"></i></span>`
            : "";
            
        const chevronClass = isAgentActive ? "fa-chevron-down" : "fa-chevron-right";

        agentHeader.innerHTML = `
            <div class="note-card-header">
                <span class="note-card-title" style="text-transform: capitalize;">${escapeHtml(agent.name.replace(/_/g, " "))}</span>
                <div style="display: flex; gap: 0.25rem; align-items: center;">
                    ${proposedIndicator}
                    <i class="fa-solid ${chevronClass} text-dim" style="font-size: 0.75rem; margin-left: 0.25rem;"></i>
                </div>
            </div>
        `;

        agentContainer.appendChild(agentHeader);

        // Versions List (collapsible)
        const versionsList = document.createElement("div");
        versionsList.className = "agent-versions-list";
        versionsList.style.display = isAgentActive ? "flex" : "none";

        // Render each version for this agent, reversed so newest is on top
        const reversedVersions = [...agent.versions].reverse();
        reversedVersions.forEach(version => {
            const item = document.createElement("div");
            const isActiveSelection = (agent.name === activeAgentName) && (version === activeVersion);
            
            item.className = `version-item ${isActiveSelection ? "active" : ""}`;

            // Is this version the currently active one for this agent?
            const isVersionActive = (version === agent.active_version);
            
            let statusBadge = "";
            if (isVersionActive) {
                statusBadge = `<span class="badge badge-xs badge-cyan" style="font-size: 0.6rem; padding: 0.1rem 0.35rem;">Active</span>`;
            } else if (agent.has_proposed && version === agent.versions[agent.versions.length - 1]) {
                statusBadge = `<span class="badge badge-xs badge-purple" style="font-size: 0.6rem; padding: 0.1rem 0.35rem;"><i class="fa-solid fa-code-pull-request"></i> Proposed</span>`;
            }

            const indicatorIcon = isActiveSelection
                ? `<i class="fa-solid fa-caret-right version-caret" style="font-size: 0.75rem; margin-right: 0.35rem;"></i>`
                : `<i class="fa-regular fa-file-lines text-dim version-icon" style="font-size: 0.7rem; margin-right: 0.35rem; opacity: 0.5;"></i>`;

            item.innerHTML = `
                <div style="display: flex; align-items: center; gap: 0.2rem; min-width: 0; overflow: hidden;">
                    ${indicatorIcon}
                    <span class="version-name font-mono" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(version)}</span>
                </div>
                ${statusBadge ? `<div style="flex-shrink: 0; margin-left: 0.5rem;">${statusBadge}</div>` : ""}
            `;

            item.addEventListener("click", (e) => {
                e.stopPropagation();
                onSelect(agent, version);
            });
            versionsList.appendChild(item);
        });
        
        agentContainer.appendChild(versionsList);
        
        // Toggle accordion on header click
        agentHeader.addEventListener("click", () => {
            const isExpanded = versionsList.style.display === "flex" || versionsList.style.display === "block";
            versionsList.style.display = isExpanded ? "none" : "flex";
            const icon = agentHeader.querySelector("i.fa-chevron-down, i.fa-chevron-right");
            if (icon) {
                icon.className = isExpanded ? "fa-solid fa-chevron-right text-dim" : "fa-solid fa-chevron-down text-dim";
            }
            agentHeader.classList.toggle("collapsed", isExpanded);
        });

        containerEl.appendChild(agentContainer);
    });
}
