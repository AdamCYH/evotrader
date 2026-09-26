/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — Detail Inspection Drawer Controller
   
   Manages the slide-out side panel used for inspecting tool executions,
   session agent thoughts, and evolution proposals.
   ═══════════════════════════════════════════════════════════════════════ */

import * as api from "./api.js";
import * as ui from "./components.js";

// ── DOM Element References ──────────────────────────────────────────────

const detailDrawer = document.getElementById("detail-drawer");
const detailDrawerBackdrop = document.getElementById("detail-drawer-backdrop");
const detailLabel1 = document.getElementById("detail-label-1");
const detailLabel2 = document.getElementById("detail-label-2");
const detailLabel3 = document.getElementById("detail-label-3");
const detailLabel4 = document.getElementById("detail-label-4");
const detailAgentBadge = document.getElementById("detail-agent-badge");
const detailToolName = document.getElementById("detail-tool-name");
const detailToolSource = document.getElementById("detail-tool-source");
const detailTimestamp = document.getElementById("detail-timestamp");
const detailArgsPre = document.getElementById("detail-args-pre");
const detailRespPre = document.getElementById("detail-resp-pre");
const detailMcpAddressContainer = document.getElementById("detail-mcp-address-container");
const detailMcpAddress = document.getElementById("detail-mcp-address");
const tabArgs = document.getElementById("drawer-tab-args");
const tabResp = document.getElementById("drawer-tab-resp");
const panelArgs = document.getElementById("drawer-panel-args");
const panelResp = document.getElementById("drawer-panel-resp");

function setDrawerMetaLabels(l1 = "Agent", l2 = "Tool", l3 = "Type", l4 = "Timestamp") {
    if (detailLabel1) detailLabel1.textContent = l1;
    if (detailLabel2) detailLabel2.textContent = l2;
    if (detailLabel3) detailLabel3.textContent = l3;
    if (detailLabel4) detailLabel4.textContent = l4;
}

// LLM Modal Elements
const llmModal = document.getElementById("llm-modal");
const llmModalBackdrop = document.getElementById("llm-modal-backdrop");
const btnCloseLlmModal = document.getElementById("btn-close-llm-modal");
const llmTabReq = document.getElementById("llm-tab-req");
const llmTabRes = document.getElementById("llm-tab-res");
const llmPanelReq = document.getElementById("llm-panel-req");
const llmPanelRes = document.getElementById("llm-panel-res");
const llmReqPre = document.getElementById("llm-req-pre");
const llmResPre = document.getElementById("llm-res-pre");

export function openLlmModal(index) {
    if (!llmModal || !window._currentLlmLogs || !window._currentLlmLogs[index]) return;
    const log = window._currentLlmLogs[index];
    
    llmReqPre.innerHTML = log.request ? ui.syntaxHighlightJson(log.request) : "No request data";
    llmResPre.innerHTML = log.response ? ui.syntaxHighlightJson(log.response) : "No response data";
    
    llmModal.classList.remove("hidden");
    llmModalBackdrop.classList.remove("hidden");
    
    switchLlmModalTab("req");
}
window.openLlmModal = openLlmModal;

function closeLlmModal() {
    if (llmModal) llmModal.classList.add("hidden");
    if (llmModalBackdrop) llmModalBackdrop.classList.add("hidden");
}

function switchLlmModalTab(tab) {
    if (tab === "req") {
        if (llmTabReq) llmTabReq.classList.add("active");
        if (llmTabRes) llmTabRes.classList.remove("active");
        if (llmPanelReq) llmPanelReq.classList.add("active");
        if (llmPanelRes) llmPanelRes.classList.remove("active");
    } else {
        if (llmTabReq) llmTabReq.classList.remove("active");
        if (llmTabRes) llmTabRes.classList.add("active");
        if (llmPanelReq) llmPanelReq.classList.remove("active");
        if (llmPanelRes) llmPanelRes.classList.add("active");
    }
}

// Bind LLM modal events
if (btnCloseLlmModal) btnCloseLlmModal.addEventListener("click", closeLlmModal);
if (llmModalBackdrop) llmModalBackdrop.addEventListener("click", closeLlmModal);
if (llmTabReq) llmTabReq.addEventListener("click", () => switchLlmModalTab("req"));
if (llmTabRes) llmTabRes.addEventListener("click", () => switchLlmModalTab("res"));

// ── Tab Switching ───────────────────────────────────────────────────────

export function switchDrawerTab(tab) {
    if (tab === "args") {
        if (tabArgs) tabArgs.classList.add("active");
        if (tabResp) tabResp.classList.remove("active");
        if (panelArgs) panelArgs.classList.add("active");
        if (panelResp) panelResp.classList.remove("active");
    } else {
        if (tabArgs) tabArgs.classList.remove("active");
        if (tabResp) tabResp.classList.add("active");
        if (panelArgs) panelArgs.classList.remove("active");
        if (panelResp) panelResp.classList.add("active");
    }
}

// ── Open / Close Drawer ─────────────────────────────────────────────────

function openDrawer() {
    if (!detailDrawer || !detailDrawerBackdrop) return;
    detailDrawer.classList.remove("collapsed");
    detailDrawer.classList.add("open");
    detailDrawerBackdrop.classList.add("active");
    detailDrawerBackdrop.classList.remove("hidden");
}

export function closeDetailDrawer() {
    if (detailDrawer) {
        detailDrawer.classList.remove("open");
        detailDrawer.classList.add("collapsed");
    }
    if (detailDrawerBackdrop) {
        detailDrawerBackdrop.classList.remove("active");
        setTimeout(() => {
            if (detailDrawer && detailDrawer.classList.contains("collapsed")) {
                detailDrawerBackdrop.classList.add("hidden");
            }
        }, 300); // match CSS transition
    }
}

// ── Tool Execution Inspector ────────────────────────────────────────────

export function openDetailDrawer(card) {
    if (!detailDrawer || !detailDrawerBackdrop) return;

    setDrawerMetaLabels("Agent", "Tool", "Type", "Timestamp");

    // Restore standard drawer titles & tabs
    const titleEl = detailDrawer.querySelector(".drawer-title");
    if (titleEl) {
        titleEl.innerHTML = `<i class="fa-solid fa-screwdriver-wrench text-cyan"></i> <span>Inspect Tool Execution</span>`;
    }
    if (tabArgs) {
        tabArgs.innerHTML = `<i class="fa-solid fa-sliders"></i> Parameters`;
    }
    if (tabResp) {
        tabResp.innerHTML = `<i class="fa-solid fa-reply"></i> Response`;
    }
    if (detailArgsPre) {
        detailArgsPre.className = "font-mono json-display";
    }
    if (detailRespPre) {
        detailRespPre.className = "font-mono json-display";
    }

    const agent = card.getAttribute("data-agent") || "unknown";
    const toolName = card.getAttribute("data-tool-name") || "";
    const timestamp = card.getAttribute("data-timestamp") || "";
    const argsStr = card.getAttribute("data-args") || "{}";
    const responseStr = card.getAttribute("data-response") || "null";

    // Update fields
    if (detailToolName) {
        detailToolName.className = "mono-chip";
        detailToolName.style.cursor = "default";
        detailToolName.onclick = null;
        detailToolName.title = toolName;
        detailToolName.innerHTML = `<i class="fa-solid fa-screwdriver-wrench text-dim"></i> ${escapeHtml(toolName)}`;
    }
    if (detailTimestamp) {
        detailTimestamp.innerHTML = `<i class="fa-solid fa-clock text-dim"></i> ${ui.formatTime(timestamp, true)}`;
    }

    const toolInfoStr = card.getAttribute("data-tool-info") || "{}";
    let toolInfo = {};
    try {
        toolInfo = JSON.parse(toolInfoStr);
    } catch (e) {
        console.error("Failed to parse data-tool-info:", e);
    }

    if (detailToolSource) {
        const isLocal = toolInfo.source === "local";
        if (isLocal) {
            detailToolSource.textContent = "Local";
            detailToolSource.className = "type-badge local";
            if (detailMcpAddressContainer) detailMcpAddressContainer.classList.add("hidden");
        } else {
            detailToolSource.textContent = "MCP";
            detailToolSource.className = "type-badge mcp";
            
            const providerName = toolInfo.provider || "mcp_provider";
            const providerLabel = toolInfo.provider_label || providerName.split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
            const mcpAddress = toolInfo.address || "remote connection";
            
            if (detailMcpAddress) detailMcpAddress.textContent = `${providerLabel} @ ${mcpAddress}`;
            if (detailMcpAddressContainer) detailMcpAddressContainer.classList.remove("hidden");
        }
    }

    if (detailAgentBadge) {
        const iconClass = ui.getAgentIcon(agent);
        detailAgentBadge.innerHTML = `<i class="fa-solid ${iconClass}"></i> ${escapeHtml(agent.replace("_", " "))}`;
        detailAgentBadge.className = `agent-chip ${escapeHtml(agent)}`;
    }

    if (detailArgsPre) detailArgsPre.innerHTML = ui.syntaxHighlightJson(argsStr);
    if (detailRespPre) {
        if (responseStr === "null") {
            detailRespPre.innerHTML = "No response received yet.";
        } else {
            detailRespPre.innerHTML = ui.syntaxHighlightJson(responseStr);
        }
    }

    // Reset tabs to parameters
    switchDrawerTab("args");

    // Open drawer
    openDrawer();
}

// ── Session Thoughts & LLM Metrics Drawer ───────────────────────────────

export async function openSessionDrawer(sessionId) {
    if (!detailDrawer || !detailDrawerBackdrop || !sessionId) return;

    const shortId = sessionId.length > 14 ? sessionId.substring(0, 14) + "…" : sessionId;

    // Set title and initial metadata labels
    const titleEl = detailDrawer.querySelector(".drawer-title");
    if (titleEl) {
        titleEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin text-cyan"></i> <span>Loading Session...</span>`;
    }

    setDrawerMetaLabels("Session Type", "Session ID", "Cycle Status", "Timestamp");

    // Hide delete button and MCP container
    const btnDelete = document.getElementById("detail-drawer-delete");
    if (btnDelete) btnDelete.classList.add("hidden");
    if (detailMcpAddressContainer) detailMcpAddressContainer.classList.add("hidden");

    // Initial loading states
    if (detailAgentBadge) {
        detailAgentBadge.innerHTML = `<span class="badge badge-secondary"><i class="fa-solid fa-spinner fa-spin"></i> Loading...</span>`;
        detailAgentBadge.className = "";
    }
    if (detailToolName) {
        detailToolName.className = "mono-chip";
        detailToolName.style.cursor = "default";
        detailToolName.onclick = null;
        detailToolName.title = sessionId;
        detailToolName.innerHTML = `<span class="text-dim font-mono">${escapeHtml(shortId)}</span>`;
    }
    if (detailToolSource) {
        detailToolSource.innerHTML = `<span class="badge badge-secondary">...</span>`;
        detailToolSource.className = "";
    }
    if (detailTimestamp) {
        detailTimestamp.innerHTML = `<i class="fa-solid fa-spinner fa-spin text-dim"></i> Loading...`;
    }

    // Set tab labels
    if (tabResp) tabResp.innerHTML = `<i class="fa-solid fa-chart-pie"></i> Overview & LLM Metrics`;
    if (tabArgs) tabArgs.innerHTML = `<i class="fa-solid fa-brain"></i> Agent Thoughts`;

    // Loading state in tab panels
    if (detailRespPre) {
        detailRespPre.className = "proposal-view";
        detailRespPre.innerHTML = `<div class="text-dim text-center py-4"><i class="fa-solid fa-spinner fa-spin"></i> Loading session overview...</div>`;
    }
    if (detailArgsPre) {
        detailArgsPre.className = "proposal-view";
        detailArgsPre.innerHTML = `<div class="text-dim text-center py-4"><i class="fa-solid fa-spinner fa-spin"></i> Loading agent thoughts...</div>`;
    }

    // Switch to Overview tab by default and open drawer
    switchDrawerTab("resp");
    openDrawer();

    // Fetch data in parallel
    try {
        const [thoughtsData, cycleDetail, llmLogsData] = await Promise.all([
            api.fetchThoughts(sessionId).catch(() => ({ thoughts: [] })),
            api.fetchCycleDetail(sessionId).catch(() => null),
            api.fetchLlmLogs(sessionId).catch(() => ({ logs: [] }))
        ]);

        const thoughts = thoughtsData.thoughts || [];
        const logs = llmLogsData.logs || [];
        window._currentLlmLogs = logs;

        const isEvol = (cycleDetail && (cycleDetail.is_evolution === 1 || cycleDetail.is_evolution === true))
            || sessionId.toLowerCase().includes('evol')
            || thoughts.some(t => t.agent_name === 'evolution')
            || logs.some(l => (l.agent_name || '').toLowerCase().includes('evolution'));

        // Update Drawer Header Title
        if (titleEl) {
            titleEl.innerHTML = isEvol
                ? `<i class="fa-solid fa-dna" style="color: var(--purple);"></i> <span>Evolution Session Details</span>`
                : `<i class="fa-solid fa-chart-line" style="color: var(--amber);"></i> <span>Trading Cycle Details</span>`;
        }

        // Field 1: Session Type
        if (detailAgentBadge) {
            detailAgentBadge.innerHTML = isEvol
                ? `<span class="badge badge-purple" style="font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px; display: inline-flex; align-items: center; gap: 0.3rem;"><i class="fa-solid fa-dna"></i> Evolution</span>`
                : `<span class="badge badge-amber" style="font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px; display: inline-flex; align-items: center; gap: 0.3rem;"><i class="fa-solid fa-chart-line"></i> Trading Cycle</span>`;
            detailAgentBadge.className = "";
        }

        // Field 2: Session ID (Clickable copy chip without double border/padding)
        if (detailToolName) {
            detailToolName.className = "mono-chip";
            detailToolName.style.cursor = "pointer";
            detailToolName.title = `Click to copy full Session ID: ${sessionId}`;
            detailToolName.innerHTML = `<span>${escapeHtml(shortId)}</span> <i class="fa-regular fa-copy text-dim" style="font-size: 0.72rem; margin-left: 0.25rem;"></i>`;
            detailToolName.onclick = () => {
                navigator.clipboard.writeText(sessionId);
                const icon = detailToolName.querySelector("i");
                if (icon) {
                    icon.className = "fa-solid fa-check text-emerald";
                    setTimeout(() => {
                        if (icon) icon.className = "fa-regular fa-copy text-dim";
                    }, 1500);
                }
            };
        }

        // Field 3: Cycle Status
        if (detailToolSource) {
            let statusBadge = "";
            const status = cycleDetail ? (cycleDetail.cycle_status || "").toLowerCase() : "";
            if (status === "complete") {
                statusBadge = `<span class="cycle-status-badge status-complete"><i class="fa-solid fa-circle-check"></i> Complete</span>`;
            } else if (status === "partial") {
                statusBadge = `<span class="cycle-status-badge status-partial"><i class="fa-solid fa-triangle-exclamation"></i> Partial</span>`;
            } else if (status === "error") {
                statusBadge = `<span class="cycle-status-badge status-error"><i class="fa-solid fa-circle-xmark"></i> Error</span>`;
            } else if (status === "running") {
                statusBadge = `<span class="cycle-status-badge status-running"><i class="fa-solid fa-spinner fa-spin"></i> Running</span>`;
            } else if (logs.length > 0) {
                statusBadge = `<span class="cycle-status-badge status-complete"><i class="fa-solid fa-circle-check"></i> Recorded</span>`;
            } else {
                statusBadge = `<span class="cycle-status-badge status-complete"><i class="fa-solid fa-circle-check"></i> Finished</span>`;
            }
            detailToolSource.innerHTML = statusBadge;
            detailToolSource.className = "";
        }

        // Field 4: Timestamp
        const startTime = cycleDetail?.start_time || (logs[0]?.timestamp) || (thoughts[0]?.timestamp);
        if (detailTimestamp) {
            detailTimestamp.innerHTML = startTime
                ? `<i class="fa-solid fa-clock text-dim"></i> ${ui.formatTime(startTime, true)}`
                : `<span class="text-dim">—</span>`;
        }

        // Update Tab 2 Label with thoughts count
        if (tabArgs) {
            tabArgs.innerHTML = `<i class="fa-solid fa-brain"></i> Agent Thoughts (${thoughts.length})`;
        }

        // Tab 1: Render Overview & LLM Metrics
        if (detailRespPre) {
            const totalInput = logs.reduce((sum, l) => sum + (Number(l.input_tokens) || 0), 0);
            const totalOutput = logs.reduce((sum, l) => sum + (Number(l.output_tokens) || 0), 0);
            const totalTokens = totalInput + totalOutput;
            const totalCalls = logs.length;
            const avgLatencyMs = totalCalls > 0 ? (logs.reduce((sum, l) => sum + (Number(l.duration_ms) || 0), 0) / totalCalls) : 0;
            const avgLatencySec = (avgLatencyMs / 1000.0).toFixed(2);

            const formatTok = (n) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : `${n}`;
            const tokenStr = formatTok(totalTokens);
            const inTokStr = formatTok(totalInput);
            const outTokStr = formatTok(totalOutput);

            // Separate Cognitive Agents vs Tools Executed vs Workflow Stages
            const agentsSet = new Set();
            const toolsSet = new Set();
            let thoughtCount = 0;
            let toolCallCount = 0;
            let runtimeNoteCount = 0;

            logs.forEach(l => {
                const an = (l.agent_name || "").toLowerCase();
                if (an && !["system", "user", "unknown"].includes(an)) {
                    agentsSet.add(an);
                }
            });

            thoughts.forEach(t => {
                const an = (t.agent_name || "").toLowerCase();
                if (an && !["system", "user"].includes(an)) {
                    agentsSet.add(an);
                }
                if (t.event_type === "runtime") {
                    // Harness diagnostics (quota checks). Not reasoning and not
                    // a tool call — counting them as either overstates the one
                    // and understates the other.
                    runtimeNoteCount++;
                } else if (t.event_type === "thought") {
                    thoughtCount++;
                } else if (t.event_type === "tool_call" || t.event_type === "tool_response") {
                    toolCallCount++;
                    let toolName = t.content;
                    if (t.meta) {
                        try {
                            const m = typeof t.meta === "string" ? JSON.parse(t.meta) : t.meta;
                            if (m.tool_name) toolName = m.tool_name;
                        } catch (e) {}
                    }
                    if (toolName && toolName !== "TOOL_CALL" && !agentsSet.has(toolName.toLowerCase())) {
                        toolsSet.add(toolName);
                    }
                }
            });

            const agentsList = Array.from(agentsSet);
            const toolsList = Array.from(toolsSet);
            const modelsList = Array.from(new Set(logs.map(l => l.model).filter(Boolean)));
            const stages = cycleDetail?.stages_completed || [];

            let infoHtml = `<div class="proposal-container">`;

            // Card 1: Token Usage & LLM Performance KPIs
            const modelBadgesHtml = modelsList.length > 0
                ? modelsList.map(m => `<span class="mono-chip font-mono" style="font-size: 0.65rem; padding: 0.08rem 0.35rem; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 3px; word-break: break-all;">${escapeHtml(m)}</span>`).join("")
                : `<span class="text-dim font-mono" style="font-size: 0.68rem;">${isEvol ? 'Evolution' : 'Pipeline'}</span>`;

            infoHtml += `
                <div class="proposal-card">
                    <span class="proposal-card-title"><i class="fa-solid fa-chart-pie ${isEvol ? 'text-purple' : 'text-amber'}"></i> Token Usage & Performance</span>
                    <div class="cycle-detail-kpis">
                        <div class="cycle-kpi-card" title="Total Tokens: ${totalTokens.toLocaleString()} (${totalInput.toLocaleString()} in / ${totalOutput.toLocaleString()} out)">
                            <div class="cycle-kpi-header">
                                <span class="cycle-kpi-label"><i class="fa-solid fa-coins text-amber"></i> Total Tokens</span>
                            </div>
                            <div class="cycle-kpi-value font-mono">${tokenStr}</div>
                            <div class="cycle-kpi-sub font-mono">
                                <span class="text-dim">${inTokStr} in</span>
                                <span class="cycle-kpi-dot">•</span>
                                <span class="text-cyan">${outTokStr} out</span>
                            </div>
                        </div>

                        <div class="cycle-kpi-card" title="Total LLM Calls: ${totalCalls} (${modelsList.join(', ') || 'Pipeline'})">
                            <div class="cycle-kpi-header">
                                <span class="cycle-kpi-label"><i class="fa-solid fa-microchip ${isEvol ? 'text-purple' : 'text-cyan'}"></i> LLM Calls</span>
                            </div>
                            <div class="cycle-kpi-value font-mono">${totalCalls} <span class="cycle-kpi-unit">calls</span></div>
                            <div class="cycle-kpi-sub font-mono">
                                ${modelBadgesHtml}
                            </div>
                        </div>

                        <div class="cycle-kpi-card" title="Average Latency: ${avgLatencySec}s per invocation across ${totalCalls} calls">
                            <div class="cycle-kpi-header">
                                <span class="cycle-kpi-label"><i class="fa-solid fa-bolt text-cyan"></i> Avg Latency</span>
                            </div>
                            <div class="cycle-kpi-value font-mono text-cyan">${avgLatencySec}<span class="cycle-kpi-unit">s</span></div>
                            <div class="cycle-kpi-sub font-mono text-dim">
                                <span>per invocation</span>
                            </div>
                        </div>
                    </div>
                </div>
            `;

            // Card 2: Pipeline Execution & Behavior (Distinguishing Agents vs Tools vs Stages)
            infoHtml += `
                <div class="proposal-card">
                    <span class="proposal-card-title"><i class="fa-solid fa-diagram-project ${isEvol ? 'text-purple' : 'text-amber'}"></i> Pipeline Execution & Behavior</span>
                    <div class="proposal-card-content" style="display: flex; flex-direction: column; gap: 0.75rem; font-size: 0.85rem;">
                        
                        <!-- Cognitive Agents -->
                        <div style="display: flex; flex-direction: column; gap: 0.35rem;">
                            <div class="text-dim" style="font-size: 0.7rem; font-weight: 700; text-transform: uppercase; display: flex; align-items: center; gap: 0.35rem;">
                                <i class="fa-solid fa-brain text-purple"></i> Cognitive Agents (${agentsList.length})
                            </div>
                            <div style="display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center;">
                                ${agentsList.length > 0
                                    ? agentsList.map(a => `<span class="agent-chip ${escapeHtml(a)}" style="font-size: 0.7rem; padding: 0.12rem 0.45rem;"><i class="fa-solid ${ui.getAgentIcon(a)}"></i> ${escapeHtml(a.replace('_', ' '))}</span>`).join("")
                                    : `<span class="text-dim text-small">${isEvol ? 'Evolution Agent' : 'Trading Orchestrator'}</span>`
                                }
                            </div>
                        </div>

                        <!-- Tools Executed -->
                        ${toolsList.length > 0 ? `
                            <div style="display: flex; flex-direction: column; gap: 0.35rem; border-top: 1px solid var(--border-color); padding-top: 0.5rem;">
                                <div class="text-dim" style="font-size: 0.7rem; font-weight: 700; text-transform: uppercase; display: flex; align-items: center; gap: 0.35rem;">
                                    <i class="fa-solid fa-screwdriver-wrench text-cyan"></i> Tools & Actions Executed (${toolsList.length})
                                </div>
                                <div style="display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center;">
                                    ${toolsList.map(t => `<span class="mono-chip font-mono" style="font-size: 0.68rem; padding: 0.1rem 0.4rem; background: var(--bg-hover);"><i class="fa-solid fa-bolt text-dim" style="font-size: 0.6rem;"></i> ${escapeHtml(t)}</span>`).join("")}
                                </div>
                            </div>
                        ` : ""}

                        <!-- Workflow Stages -->
                        ${stages.length > 0 ? `
                            <div style="display: flex; flex-direction: column; gap: 0.35rem; border-top: 1px solid var(--border-color); padding-top: 0.5rem;">
                                <div class="text-dim" style="font-size: 0.7rem; font-weight: 700; text-transform: uppercase; display: flex; align-items: center; gap: 0.35rem;">
                                    <i class="fa-solid fa-list-check text-emerald"></i> Workflow Stages Completed (${stages.length})
                                </div>
                                <div style="display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center;">
                                    ${stages.map(s => `<span class="badge badge-outline" style="font-size: 0.68rem; padding: 0.1rem 0.45rem;"><i class="fa-solid fa-circle-check text-emerald" style="font-size: 0.6rem;"></i> ${escapeHtml(s.replace('_', ' '))}</span>`).join("")}
                                </div>
                            </div>
                        ` : ""}

                        <!-- Activity Stats Summary -->
                        <div style="display: flex; flex-wrap: wrap; gap: 1rem; color: var(--text-dim); font-size: 0.75rem; border-top: 1px solid var(--border-color); padding-top: 0.5rem; margin-top: 0.1rem;">
                            <span><i class="fa-solid fa-brain" style="font-size: 0.7rem;"></i> Thoughts: <strong style="color: var(--text-primary);">${thoughtCount}</strong></span>
                            <span><i class="fa-solid fa-wrench" style="font-size: 0.7rem;"></i> Tool Events: <strong style="color: var(--text-primary);">${toolCallCount}</strong></span>
                            <span><i class="fa-solid fa-microchip" style="font-size: 0.7rem;"></i> LLM Calls: <strong style="color: var(--text-primary);">${totalCalls}</strong></span>
                            ${cycleDetail?.has_trades ? `<span style="color: var(--emerald); font-weight: 700;"><i class="fa-solid fa-dollar-sign"></i> Trades Executed</span>` : ""}
                        </div>
                    </div>
                </div>
            `;

            // Card 3: LLM Calls Breakdown Table
            if (logs.length > 0) {
                infoHtml += `
                    <div class="proposal-card">
                        <div class="proposal-card-row">
                            <span class="proposal-card-title"><i class="fa-solid fa-microchip ${isEvol ? 'text-purple' : 'text-amber'}"></i> LLM Calls Breakdown</span>
                            <span class="text-dim text-small" style="font-size: 0.7rem;">Click row to inspect prompt/response</span>
                        </div>
                        <div class="proposal-card-content" style="padding: 0; margin-top: 0.35rem;">
                            <div style="max-height: 360px; overflow-y: auto; overflow-x: auto; border: 1px solid var(--border-color); border-radius: 4px;">
                                <table class="metrics-table" style="width: 100%; border-collapse: collapse; font-size: 0.8rem;">
                                    <thead>
                                        <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-dim); text-align: left; background: var(--bg-card);">
                                            <th style="padding: 0.45rem 0.6rem; position: sticky; top: 0; background: var(--bg-card); z-index: 10;">Time</th>
                                            <th style="padding: 0.45rem 0.6rem; position: sticky; top: 0; background: var(--bg-card); z-index: 10;">Agent</th>
                                            <th style="padding: 0.45rem 0.6rem; position: sticky; top: 0; background: var(--bg-card); z-index: 10;">Model</th>
                                            <th style="padding: 0.45rem 0.6rem; text-align: right; position: sticky; top: 0; background: var(--bg-card); z-index: 10;">Input</th>
                                            <th style="padding: 0.45rem 0.6rem; text-align: right; position: sticky; top: 0; background: var(--bg-card); z-index: 10;">Output</th>
                                            <th style="padding: 0.45rem 0.6rem; text-align: right; position: sticky; top: 0; background: var(--bg-card); z-index: 10;">Duration</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                `;

                // Sort ascending by time
                logs.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));

                logs.forEach((log, index) => {
                    const timeStr = log.timestamp ? log.timestamp.split("T")[1].substring(0, 8) : "-";
                    const agentStr = log.agent_name ? (log.agent_name.charAt(0).toUpperCase() + log.agent_name.slice(1)) : "Unknown";
                    const inputStr = Number(log.input_tokens || 0).toLocaleString();
                    let outputStr = Number(log.output_tokens || 0).toLocaleString();
                    if (log.reasoning_tokens) {
                        outputStr += ` <span style="font-size:0.7em; color:var(--text-dim);" title="Reasoning Tokens">(${log.reasoning_tokens})</span>`;
                    }
                    const durStr = log.duration_ms ? (log.duration_ms / 1000).toFixed(2) + "s" : "-";
                    const borderBottom = index < logs.length - 1 ? 'border-bottom: 1px solid var(--border-color);' : '';

                    infoHtml += `
                        <tr style="${borderBottom} cursor: pointer;" onclick="window.openLlmModal(${index})" class="table-row-hover">
                            <td class="font-mono text-dim" style="padding: 0.45rem 0.6rem; white-space: nowrap;">${timeStr}</td>
                            <td style="padding: 0.45rem 0.6rem; color: var(--text-primary); font-weight: 500; white-space: nowrap;">${escapeHtml(agentStr)}</td>
                            <td class="font-mono text-dim" style="padding: 0.45rem 0.6rem; white-space: nowrap;">${escapeHtml(log.model || 'Unknown')}</td>
                            <td class="font-mono text-dim" style="padding: 0.45rem 0.6rem; text-align: right;">${inputStr}</td>
                            <td class="font-mono text-cyan" style="padding: 0.45rem 0.6rem; text-align: right;">${outputStr}</td>
                            <td class="font-mono text-amber" style="padding: 0.45rem 0.6rem; text-align: right;">${durStr}</td>
                        </tr>
                    `;
                });
                infoHtml += `</tbody></table></div></div></div>`;
            } else {
                infoHtml += `
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-microchip ${isEvol ? 'text-purple' : 'text-amber'}"></i> LLM Calls</span>
                        <div class="proposal-card-content text-dim text-small">No individual LLM call spans recorded for this session.</div>
                    </div>
                `;
            }

            // "View in Run History" button
            infoHtml += `
                <div style="margin-top: 0.5rem;">
                    <button class="btn btn-secondary btn-sm btn-view-in-explorer" style="width: 100%; padding: 0.65rem; font-weight: 600; display: inline-flex; align-items: center; justify-content: center; gap: 0.5rem;">
                        <i class="fa-solid fa-compass"></i> View in Run History Explorer
                    </button>
                </div>
            `;

            infoHtml += `</div>`;
            detailRespPre.innerHTML = infoHtml;

            // Wire up navigation button
            const viewBtn = detailRespPre.querySelector(".btn-view-in-explorer");
            if (viewBtn) {
                viewBtn.addEventListener("click", () => {
                    closeDetailDrawer();
                    window.dispatchEvent(new CustomEvent("select-run", { detail: { sessionId } }));
                });
            }
        }

        // Tab 2: Render thought cards stream
        if (detailArgsPre) {
            if (thoughts.length === 0) {
                detailArgsPre.innerHTML = `<div class="text-dim text-center py-4">No agent thought events recorded for this session.</div>`;
            } else {
                detailArgsPre.innerHTML = "";
                const container = document.createElement("div");
                container.className = "session-thoughts-stream";
                container.style.cssText = "display: flex; flex-direction: column; gap: 0.5rem; max-height: 60vh; overflow-y: auto; padding: 0.5rem;";

                const processed = ui.mergeToolCallsAndResponses(thoughts, false);
                processed.reverse();
                processed.forEach(t => ui.renderThoughtCard(t, container, false, false));

                detailArgsPre.appendChild(container);
            }
        }
    } catch (err) {
        console.error("Failed to load session drawer data:", err);
        if (detailRespPre) {
            detailRespPre.innerHTML = `<div class="text-crimson text-center py-4">Error loading session details: ${escapeHtml(err.message)}</div>`;
        }
    }
}

// ── Evolution Proposal Drawer ───────────────────────────────────────────

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

export async function openEvolutionDetailDrawer(item, switchTabFn) {
    if (!detailDrawer) return;

    setDrawerMetaLabels("Component", "Change Type", "Status", "Timestamp");

    // Set Title
    const titleEl = detailDrawer.querySelector(".drawer-title");
    if (titleEl) {
        titleEl.innerHTML = `<i class="fa-solid fa-dna" style="color: var(--purple);"></i> <span>Inspect Self-Evolution Proposal</span>`;
    }

    // Set Metadata
    if (detailAgentBadge) {
        const iconClass = ui.getAgentIcon("evolution");
        detailAgentBadge.innerHTML = `<i class="fa-solid ${iconClass}"></i> ${escapeHtml(item.target_component.replace("_", " "))}`;
        detailAgentBadge.className = `agent-chip evolution`;
    }

    if (detailToolName) {
        detailToolName.className = "mono-chip";
        detailToolName.style.cursor = "default";
        detailToolName.onclick = null;
        detailToolName.title = item.change_type;
        detailToolName.innerHTML = `<i class="fa-solid fa-dna text-dim"></i> ${escapeHtml(item.change_type.replace("_", " "))}`;
    }

    if (detailTimestamp) {
        detailTimestamp.innerHTML = `<i class="fa-solid fa-clock text-dim"></i> ${ui.formatTime(item.timestamp, true)}`;
    }

    if (detailToolSource) {
        const status = (item.status || "PROPOSED").toUpperCase();
        const badgeClass = status === "APPLIED" ? "badge-success-evo" : (status === "REJECTED" || status === "FAILED" ? "badge-failed-evo" : "badge-purple");
        detailToolSource.innerHTML = `<span class="badge ${badgeClass}" style="font-size: 0.72rem; padding: 0.12rem 0.45rem;">${escapeHtml(status)}</span>`;
        detailToolSource.className = "";
    }

    if (detailMcpAddressContainer) {
        detailMcpAddressContainer.classList.add("hidden");
    }

    // Change labels of the tabs to match our context
    if (tabArgs) {
        tabArgs.innerHTML = `<i class="fa-solid fa-file-invoice"></i> Proposal`;
    }
    if (tabResp) {
        tabResp.innerHTML = `<i class="fa-solid fa-code-compare"></i> Diff View`;
    }

    // Tab 1: Proposal Info
    let proposalHtml = `<div class="proposal-container">`;
    
    // Status
    proposalHtml += `
        <div class="proposal-card">
            <div class="proposal-card-row">
                <span class="proposal-card-title"><i class="fa-solid fa-circle-info text-purple"></i> Status</span>
                <span class="evolution-badge ${item.status.toLowerCase()}">${item.status}</span>
            </div>
        </div>
    `;

    // Versions
    proposalHtml += `
        <div class="proposal-card">
            <div class="proposal-card-row">
                <span class="proposal-card-title"><i class="fa-solid fa-code-branch text-purple"></i> Version Change</span>
                <span class="mono-chip font-mono" style="font-size: 0.85rem;" title="${escapeHtml(item.old_version)} ➔ ${escapeHtml(item.new_version)}">${escapeHtml(ui.formatVersion(item.old_version))} ➔ ${escapeHtml(ui.formatVersion(item.new_version))}</span>
            </div>
        </div>
    `;

    // Reasoning
    const reasoningText = item.reasoning ? escapeHtml(item.reasoning.trim()) : "No reasoning provided.";
    proposalHtml += `
        <div class="proposal-card">
            <span class="proposal-card-title"><i class="fa-solid fa-brain text-purple"></i> Evolution Reasoning</span>
            <div class="proposal-card-content">
                <p>${reasoningText}</p>
            </div>
        </div>
    `;

    // Expected Impact
    if (item.expected_impact) {
        const impactText = escapeHtml(item.expected_impact.trim());
        proposalHtml += `
            <div class="proposal-card">
                <span class="proposal-card-title"><i class="fa-solid fa-chart-line text-purple"></i> Expected Impact / Summary</span>
                <div class="proposal-card-content">
                    <p>${impactText}</p>
                </div>
            </div>
        `;
    }

    // Metrics Before / After
    if (item.metrics_before) {
        try {
            const metricsBefore = typeof item.metrics_before === "string" ? JSON.parse(item.metrics_before) : item.metrics_before;
            if (Object.keys(metricsBefore).length > 0) {
                proposalHtml += `
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-chart-pie text-purple"></i> Performance Metrics (Baseline)</span>
                        <pre class="font-mono json-display" style="max-height: 150px; overflow-y: auto;">${JSON.stringify(metricsBefore, null, 2)}</pre>
                    </div>
                `;
            }
        } catch (e) {}
    }

    if (item.metrics_after) {
        try {
            const metricsAfter = typeof item.metrics_after === "string" ? JSON.parse(item.metrics_after) : item.metrics_after;
            if (Object.keys(metricsAfter).length > 0) {
                proposalHtml += `
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-square-poll-vertical text-purple"></i> Backtest Validation Metrics</span>
                        <pre class="font-mono json-display" style="max-height: 150px; overflow-y: auto;">${JSON.stringify(metricsAfter, null, 2)}</pre>
                    </div>
                `;
            }
        } catch (e) {}
    }

    // Add routing button to navigate to the associated evolve note or code review
    const isProposed = item.status.toLowerCase() === "proposed" || item.status.toLowerCase() === "pending_review";
    proposalHtml += `
        <div style="margin-top: 1.5rem; display: flex; justify-content: center;">
            <button class="btn ${isProposed ? 'btn-primary' : 'btn-secondary'} btn-sm btn-re-route-proposal" 
                    data-agent="${escapeHtml(item.target_component)}" 
                    data-version="${escapeHtml(item.new_version)}"
                    data-type="${escapeHtml(item.change_type)}"
                    style="width: 100%; padding: 0.65rem; font-weight: 600; display: inline-flex; align-items: center; justify-content: center; gap: 0.5rem; border-radius: var(--border-radius-sm);">
                <i class="fa-solid ${isProposed ? 'fa-arrow-right-to-bracket' : 'fa-eye'}"></i> 
                ${isProposed ? 'Review & Approve Proposal' : 'View Associated Evolve Note / Code Review'}
            </button>
        </div>
    `;

    proposalHtml += `</div>`;

    if (detailArgsPre) {
        detailArgsPre.innerHTML = proposalHtml;
        detailArgsPre.className = "proposal-view";

        const reRouteBtn = detailArgsPre.querySelector(".btn-re-route-proposal");
        if (reRouteBtn) {
            reRouteBtn.addEventListener("click", async () => {
                const targetAgent = reRouteBtn.dataset.agent;
                const targetVersion = reRouteBtn.dataset.version;
                const changeType = reRouteBtn.dataset.type;
                
                // Close drawer
                closeDetailDrawer();
                
                // Switch tab to memory
                if (switchTabFn) switchTabFn("memory");
                
                // Trigger navigation helper
                if (changeType === "INSTRUCTION_UPDATE") {
                    if (window.showInstructionProposal) {
                        await window.showInstructionProposal(targetAgent, targetVersion);
                    }
                } else if (changeType === "CODE_REVIEW" || changeType === "ALGORITHM_NEW") {
                    // Both code reviews and strategy proposals go to the unified Proposals panel
                    if (window.showReviewProposal) {
                        await window.showReviewProposal(targetVersion);
                    }
                } else {
                    // ALGORITHM_PARAMS, REGIME_WEIGHTS → Algorithms panel
                    if (window.showAlgorithmProposal) {
                        await window.showAlgorithmProposal(targetVersion);
                    }
                }
            });
        }
    }

    // Tab 2: Diff View
    if (detailRespPre) {
        detailRespPre.innerHTML = `<div class="text-dim text-center py-4"><i class="fa-solid fa-spinner fa-spin"></i> Loading diff...</div>`;
    }

    // Switch to first tab initially
    switchDrawerTab("args");

    // Open Drawer
    openDrawer();

    // Fetch/render the diff
    try {
        let diffText = "";
        if (item.change_type === "INSTRUCTION_UPDATE") {
            const instData = await api.fetchInstructionVersion(item.target_component, item.new_version);
            diffText = instData.diff || "";
        } else if (item.change_type === "ALGORITHM_PARAMS" || item.change_type === "REGIME_WEIGHTS") {
            if (item.code_diffs) {
                const diffs = typeof item.code_diffs === "string" ? JSON.parse(item.code_diffs) : item.code_diffs;
                if (Array.isArray(diffs) && diffs.length > 0) {
                    diffText = diffs.map(d => `--- a/${d.file_path}\n+++ b/${d.file_path}\n${d.diff_content}`).join("\n\n");
                }
            }
            if (!diffText) {
                diffText = `Parameter changes:\nOld version: ${item.old_version}\nNew version: ${item.new_version}`;
            }
        } else if (item.code_diffs) {
            const diffs = typeof item.code_diffs === "string" ? JSON.parse(item.code_diffs) : item.code_diffs;
            if (Array.isArray(diffs)) {
                diffText = diffs.map(d => `--- a/${d.file_path}\n+++ b/${d.file_path}\n${d.diff_content}`).join("\n\n");
            }
        }

        if (detailRespPre) {
            detailRespPre.innerHTML = ui.renderDiff(diffText);
            detailRespPre.className = "diff-view-container";
        }
    } catch (err) {
        if (detailRespPre) {
            detailRespPre.textContent = `Error loading diff: ${err.message}`;
        }
    }
}

// ── Copy Button Handler ─────────────────────────────────────────────────

export async function handleCopyClick(btn) {
    const targetId = btn.getAttribute("data-target");
    const preEl = document.getElementById(targetId);
    if (!preEl) return;

    try {
        await navigator.clipboard.writeText(preEl.textContent);
        const originalHtml = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
        btn.classList.add("copied");
        setTimeout(() => {
            btn.innerHTML = originalHtml;
            btn.classList.remove("copied");
        }, 1500);
    } catch (err) {
        console.error("Copy failed:", err);
    }
}

// ── Event Binding ───────────────────────────────────────────────────────

export function initDrawerEvents() {
    const btnCloseDetailDrawer = document.getElementById("detail-drawer-close");
    if (btnCloseDetailDrawer) {
        btnCloseDetailDrawer.addEventListener("click", closeDetailDrawer);
    }
    if (detailDrawerBackdrop) {
        detailDrawerBackdrop.addEventListener("click", closeDetailDrawer);
    }

    // Tab switching within the drawer
    if (tabArgs) {
        tabArgs.addEventListener("click", () => switchDrawerTab("args"));
    }
    if (tabResp) {
        tabResp.addEventListener("click", () => switchDrawerTab("resp"));
    }

    // Copy buttons inside drawer panels
    document.querySelectorAll(".btn-copy-content").forEach(btn => {
        btn.addEventListener("click", () => handleCopyClick(btn));
    });

    // Expose for cross-module access (journal.js, llm_metrics.js)
    window.openSessionDrawer = openSessionDrawer;
}
