import { escapeHtml } from "./utils.js";

window.switchModelDistTab = function(tab) {
    const tabCalls = document.getElementById("model-dist-tab-calls");
    const tabTokens = document.getElementById("model-dist-tab-tokens");
    const listCalls = document.getElementById("model-distribution-list-calls");
    const listTokens = document.getElementById("model-distribution-list-tokens");

    if (tab === 'calls') {
        tabCalls.classList.add("btn-primary");
        tabCalls.classList.remove("btn-secondary");
        tabTokens.classList.add("btn-secondary");
        tabTokens.classList.remove("btn-primary");
        listCalls.style.display = "flex";
        listTokens.style.display = "none";
    } else {
        tabTokens.classList.add("btn-primary");
        tabTokens.classList.remove("btn-secondary");
        tabCalls.classList.add("btn-secondary");
        tabCalls.classList.remove("btn-primary");
        listTokens.style.display = "flex";
        listCalls.style.display = "none";
    }
};

/**
 * Render Token Usage and LLM Call metrics on the dashboard
 */
export function renderLlmMetrics(data, cycles = []) {
    if (!data) return;

    // High level metrics
    const callsEl = document.getElementById("metric-llm-calls");
    const tokensEl = document.getElementById("metric-total-tokens");
    const latencyEl = document.getElementById("metric-avg-latency");
    const efficiencyEl = document.getElementById("metric-efficiency");

    if (callsEl) callsEl.textContent = Number(data.total_calls || 0).toLocaleString();
    if (tokensEl) tokensEl.textContent = Number(data.total_tokens || 0).toLocaleString();
    if (latencyEl) {
        const seconds = (data.avg_latency_ms || 0) / 1000.0;
        latencyEl.textContent = `${seconds.toFixed(2)}s`;
    }
    if (efficiencyEl) {
        const efficiencyPercentage = (data.token_efficiency_ratio || 0) * 100;
        efficiencyEl.textContent = `${efficiencyPercentage.toFixed(1)}%`;
    }

    // Model distribution
    const callsListEl = document.getElementById("model-distribution-list-calls");
    const tokensListEl = document.getElementById("model-distribution-list-tokens");
    
    if (callsListEl && tokensListEl) {
        callsListEl.innerHTML = "";
        tokensListEl.innerHTML = "";
        const dist = data.model_distribution || {};
        const models = Object.keys(dist);
        if (models.length === 0) {
            callsListEl.innerHTML = `<div class="text-dim text-small">No model distribution data available.</div>`;
            tokensListEl.innerHTML = `<div class="text-dim text-small">No model distribution data available.</div>`;
        } else {
            const totalCalls = Object.values(dist).reduce((a, b) => a + (b.calls || typeof b === 'number' ? (b.calls || b) : 0), 0);
            const totalTokens = Object.values(dist).reduce((a, b) => a + (b.tokens || 0), 0);
            
            // Generate Calls List (sorted by calls)
            [...models].sort((a, b) => {
                const bCalls = dist[b].calls || (typeof dist[b] === 'number' ? dist[b] : 0);
                const aCalls = dist[a].calls || (typeof dist[a] === 'number' ? dist[a] : 0);
                return bCalls - aCalls;
            }).forEach(model => {
                const info = dist[model];
                const count = info.calls || (typeof info === 'number' ? info : 0);
                const percentage = totalCalls > 0 ? ((count / totalCalls) * 100).toFixed(0) : 0;
                
                const modelRow = document.createElement("div");
                modelRow.style.display = "flex";
                modelRow.style.alignItems = "center";
                modelRow.style.justifyContent = "space-between";
                modelRow.style.gap = "1rem";
                modelRow.style.paddingBottom = "0.25rem";
                
                modelRow.innerHTML = `
                    <div style="flex: 1; min-width: 0;">
                        <div style="display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 0.2rem;">
                            <span class="font-mono text-bold" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(model)}">${escapeHtml(model)}</span>
                            <span class="text-dim font-mono">${count} calls</span>
                        </div>
                        <div style="width: 100%; height: 6px; background: var(--bg-card); border-radius: 3px; overflow: hidden; border: 1px solid var(--border-color);">
                            <div style="width: ${percentage}%; height: 100%; background: linear-gradient(90deg, var(--cyan, #06b6d4), var(--indigo, #6366f1)); border-radius: 3px;"></div>
                        </div>
                    </div>
                    <div style="text-align: right; min-width: 45px; font-size: 0.75rem; color: var(--text-secondary); font-mono font-bold;">
                        ${percentage}%
                    </div>
                `;
                callsListEl.appendChild(modelRow);
            });

            // Generate Tokens List (sorted by tokens)
            [...models].sort((a, b) => (dist[b].tokens || 0) - (dist[a].tokens || 0)).forEach(model => {
                const info = dist[model];
                const tokens = info.tokens || 0;
                const percentage = totalTokens > 0 ? ((tokens / totalTokens) * 100).toFixed(0) : 0;
                const tokenStr = tokens >= 1000000 ? `${(tokens / 1000000).toFixed(2)}M` : `${(tokens / 1000).toFixed(1)}k`;
                const avgTokens = info.calls > 0 ? Math.round(tokens / info.calls) : 0;
                const avgStr = avgTokens >= 1000 ? `${(avgTokens / 1000).toFixed(1)}k` : `${avgTokens}`;

                const modelRow = document.createElement("div");
                modelRow.style.display = "flex";
                modelRow.style.alignItems = "center";
                modelRow.style.justifyContent = "space-between";
                modelRow.style.gap = "1rem";
                modelRow.style.paddingBottom = "0.25rem";
                
                modelRow.innerHTML = `
                    <div style="flex: 1; min-width: 0;">
                        <div style="display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 0.2rem;">
                            <span class="font-mono text-bold" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(model)}">${escapeHtml(model)}</span>
                            <span class="text-dim font-mono">${tokenStr}</span>
                        </div>
                        <div style="width: 100%; height: 6px; background: var(--bg-card); border-radius: 3px; overflow: hidden; border: 1px solid var(--border-color);">
                            <div style="width: ${percentage}%; height: 100%; background: linear-gradient(90deg, var(--emerald, #10b981), var(--cyan, #06b6d4)); border-radius: 3px;"></div>
                        </div>
                    </div>
                    <div style="text-align: right; min-width: max-content; font-size: 0.75rem; color: var(--text-secondary);">
                        <div>${tokenStr} tokens (${percentage}%)</div>
                        <div style="font-size: 0.65rem; color: var(--text-dim); margin-top: 0.15rem;">Avg: ${avgStr} / call</div>
                    </div>
                `;
                tokensListEl.appendChild(modelRow);
            });
        }
    }

    // Session breakdown table
    const tableBodyEl = document.getElementById("metrics-session-table-body");
    if (tableBodyEl) {
        tableBodyEl.innerHTML = "";
        const sessions = data.session_metrics || [];
        if (sessions.length === 0) {
            tableBodyEl.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-dim text-small py-4">No session usage data available.</td>
                </tr>
            `;
        } else {
            const allCycles = (cycles && cycles.length > 0) ? cycles : (window.state?.cycles || []);
            sessions.forEach(s => {
                const tr = document.createElement("tr");
                tr.style.cursor = "pointer";
                tr.addEventListener("click", () => {
                    if (window.openSessionDrawer) {
                        window.openSessionDrawer(s.session_id);
                    }
                });
                const shortSessionId = s.session_id.length > 14 ? s.session_id.substring(0, 14) + "..." : s.session_id;
                const latencySec = (s.avg_latency_ms || 0) / 1000.0;
                
                // Format the local timestamp cleanly
                let timeStr = "-";
                if (s.last_active_ts || s.last_active) {
                    try {
                        const date = s.last_active_ts
                            ? new Date(s.last_active_ts / 1_000_000)
                            : new Date(s.last_active);
                        if (!isNaN(date.getTime())) {
                            const timePart = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                            const datePart = date.toLocaleDateString([], { month: 'short', day: 'numeric' });
                            timeStr = `${datePart} ${timePart}`;
                        }
                    } catch (e) {
                        timeStr = s.last_active ? s.last_active.split("T")[0] : "-";
                    }
                }

                // Determine session type badge (supports API session_type, matching cycle run, or session ID)
                const matchingCycle = allCycles.find(c => c.session_id === s.session_id);
                const isEvol = s.session_type === 'evolution'
                    || s.is_evolution === 1
                    || s.is_evolution === true
                    || (matchingCycle && (matchingCycle.is_evolution === 1 || matchingCycle.is_evolution === true))
                    || (s.session_id && s.session_id.toLowerCase().includes('evol'));
                
                const typeBadge = isEvol
                    ? `<span class="badge badge-purple" style="font-size: 0.7rem; padding: 0.12rem 0.45rem; border-radius: 4px; display: inline-flex; align-items: center; gap: 0.25rem;"><i class="fa-solid fa-dna"></i> Evolution</span>`
                    : `<span class="badge badge-amber" style="font-size: 0.7rem; padding: 0.12rem 0.45rem; border-radius: 4px; display: inline-flex; align-items: center; gap: 0.25rem;"><i class="fa-solid fa-chart-line"></i> Trading</span>`;
                
                tr.innerHTML = `
                    <td class="font-mono text-bold" title="${escapeHtml(s.session_id)}">${escapeHtml(shortSessionId)}</td>
                    <td>${typeBadge}</td>
                    <td class="text-dim text-small">${escapeHtml(timeStr)}</td>
                    <td class="text-right">${s.calls_count}</td>
                    <td class="text-right text-dim">${Number(s.input_tokens || 0).toLocaleString()}</td>
                    <td class="text-right text-dim">${Number(s.output_tokens || 0).toLocaleString()}</td>
                    <td class="text-right font-mono text-cyan">${latencySec.toFixed(2)}s</td>
                `;
                tableBodyEl.appendChild(tr);
            });
        }
    }
}
