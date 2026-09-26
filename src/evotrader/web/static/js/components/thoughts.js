import { escapeHtml, formatTime, renderMarkdown } from "./utils.js";

/**
 * Render list of past trading runs in the explorer sidebar
 */
export function renderCycleList(cycles, activeSessionId, containerEl, onSelect, onDelete) {
    if (!containerEl) return;

    containerEl.innerHTML = "";
    if (cycles.length === 0) {
        containerEl.innerHTML = `
            <div class="empty-thoughts-msg text-dim text-small">
                No past runs found in database.
            </div>
        `;
        return;
    }

    cycles.forEach(c => {
        const div = document.createElement("div");
        const isActive = c.session_id === activeSessionId;
        const isEvolution = c.is_evolution === 1 || c.is_evolution === true;
        div.className = `cycle-item-card ${isEvolution ? 'evolution-run' : ''} ${isActive ? 'active' : ''}`;
        
        // Truncate session ID for display
        const shortSessionId = c.session_id.substring(0, 8) + "...";
        const startTimeStr = formatTime(c.start_time, true);

        const runIcon = isEvolution
            ? '<i class="fa-solid fa-dna" style="color: var(--purple); margin-right: 0.2rem;"></i>'
            : '<i class="fa-solid fa-chart-line" style="color: var(--amber); margin-right: 0.2rem;"></i>';
        const badgeHtml = isEvolution ? 
            '<span class="cycle-evolution-badge"><i class="fa-solid fa-dna"></i> Evolution</span>' :
            (c.has_trades ? 
                '<span class="cycle-trades-badge"><i class="fa-solid fa-dollar-sign"></i> Trades</span>' : 
                '<span class="cycle-trading-badge"><i class="fa-solid fa-chart-line"></i> Trading</span>');

        let statusBadgeHtml = "";
        if (c.cycle_status) {
            if (c.cycle_status === "complete") {
                statusBadgeHtml = '<span class="cycle-status-badge status-complete"><i class="fa-solid fa-circle-check"></i> Complete</span>';
            } else if (c.cycle_status === "partial") {
                statusBadgeHtml = '<span class="cycle-status-badge status-partial"><i class="fa-solid fa-triangle-exclamation"></i> Partial</span>';
            } else if (c.cycle_status === "error") {
                statusBadgeHtml = '<span class="cycle-status-badge status-error"><i class="fa-solid fa-circle-xmark"></i> Error</span>';
            } else if (c.cycle_status === "running") {
                statusBadgeHtml = '<span class="cycle-status-badge status-running"><i class="fa-solid fa-spinner fa-spin"></i> Running</span>';
            }
        }

        div.innerHTML = `
            <div class="cycle-card-header">
                <span class="cycle-id">${runIcon} ${shortSessionId}</span>
                <div class="cycle-header-actions" style="display: flex; align-items: center; gap: 0.35rem;">
                    ${badgeHtml}
                </div>
            </div>
            <div class="cycle-time">${startTimeStr}</div>
            <div class="cycle-details">
                <div style="display: flex; gap: 0.4rem; align-items: center;">
                    <span>Events: <strong>${c.event_count}</strong></span>
                    <span style="color: var(--text-dim); opacity: 0.5;">•</span>
                    <span>LLM Calls: <strong>${c.llm_call_count || 0}</strong></span>
                </div>
                ${statusBadgeHtml}
            </div>
        `;

        div.addEventListener("click", () => {
            onSelect(c.session_id);
        });

        containerEl.appendChild(div);
    });
}

/**
 * Helper to parse basic markdown styling (bold, lists, inline code) and style key trading terms.
 */
export function parseMarkdownAndKeywords(text) {
    if (!text) return "";
    
    // First, escape HTML characters for safety
    let escaped = escapeHtml(text);
    
    // Convert bold text **bold** -> <strong>bold</strong>
    escaped = escaped.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
    
    // Convert inline code `code` -> <code class="inline-code">$1</code>
    escaped = escaped.replace(/`(.*?)`/g, '<code class="inline-code">$1</code>');
    
    // Process line by line to format paragraphs, lists, and tables cleanly without double spacing
    const lines = escaped.split("\n");
    const formattedBlocks = [];
    let currentList = [];
    let currentParagraph = [];
    let currentTable = [];

    const flushParagraph = () => {
        if (currentParagraph.length > 0) {
            formattedBlocks.push(`<p class="thought-p">${currentParagraph.join("<br>")}</p>`);
            currentParagraph = [];
        }
    };

    const flushList = () => {
        if (currentList.length > 0) {
            formattedBlocks.push('<ul class="thought-list">' + currentList.map(li => `<li>${li}</li>`).join("") + '</ul>');
            currentList = [];
        }
    };

    const flushTable = () => {
        if (currentTable.length > 0) {
            let tableHtml = '<div class="table-container"><table class="thought-table">';
            let hasHeader = false;
            let expectedColumnCount = 0;
            
            for (let i = 0; i < currentTable.length; i++) {
                const line = currentTable[i];
                const processedLine = line
                    .replace(/<code[^>]*>([\s\S]*?)<\/code>/g, (match) => {
                        return match.replace(/\|/g, "__ESCAPED_PIPE__");
                    })
                    .replace(/\$\$([\s\S]*?)\$\$/g, (match) => match.replace(/\|/g, "__ESCAPED_PIPE__"))
                    .replace(/\$([\s\S]*?)\$/g, (match) => match.replace(/\|/g, "__ESCAPED_PIPE__"))
                    .replace(/\\\|/g, "__ESCAPED_PIPE__");
                let parts = processedLine.split("|").map(p => p.replace(/__ESCAPED_PIPE__/g, "|").trim());
                if (parts[0] === "") parts.shift();
                if (parts[parts.length - 1] === "") parts.pop();
                
                // If it is a separator line like | --- | --- |, skip it
                const isSeparator = parts.every(p => p === "" || /^[-\s:]+$/.test(p));
                if (isSeparator && i === 1) {
                    continue;
                }

                if (!hasHeader && i === 0) {
                    expectedColumnCount = parts.length;
                    hasHeader = true;
                } else if (hasHeader && expectedColumnCount > 0 && parts.length > expectedColumnCount) {
                    // LLM hallucinated an unescaped | inside the last column, merge them back
                    const extra = parts.splice(expectedColumnCount - 1);
                    parts.push(extra.join(" | "));
                }
                
                tableHtml += '<tr>';
                for (let j = 0; j < parts.length; j++) {
                    if (i === 0) {
                        tableHtml += `<th>${parts[j]}</th>`;
                    } else {
                        tableHtml += `<td>${parts[j]}</td>`;
                    }
                }
                tableHtml += '</tr>';
            }
            
            tableHtml += '</table></div>';
            formattedBlocks.push(tableHtml);
            currentTable = [];
        }
    };

    for (let line of lines) {
        const trimmed = line.trim();
        if (!trimmed) {
            flushList();
            flushParagraph();
            flushTable();
            continue;
        }

        if (trimmed.startsWith("|")) {
            flushList();
            flushParagraph();
            currentTable.push(trimmed);
        } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
            flushParagraph();
            flushTable();
            currentList.push(trimmed.substring(2));
        } else {
            flushList();
            flushTable();
            currentParagraph.push(trimmed);
        }
    }
    flushList();
    flushParagraph();
    flushTable();

    let resultHtml = formattedBlocks.join("");
    
    // Auto-highlight key trading actions and states
    const keywords = [
        { word: "BUY", class: "keyword-buy" },
        { word: "SELL", class: "keyword-sell" },
        { word: "LONG", class: "keyword-buy" },
        { word: "SHORT", class: "keyword-sell" },
        { word: "PASS", class: "keyword-pass" },
        { word: "APPROVED", class: "keyword-buy" },
        { word: "VIOLATION", class: "keyword-violation" },
        { word: "HALTED", class: "keyword-violation" },
        { word: "RISK WARNING", class: "keyword-warn" },
        { word: "BULLISH", class: "keyword-buy" },
        { word: "BEARISH", class: "keyword-sell" }
    ];
    
    keywords.forEach(kw => {
        const regex = new RegExp(`\\b(${kw.word})\\b`, "g");
        resultHtml = resultHtml.replace(regex, `<span class="thought-kw ${kw.class}">$1</span>`);
    });
    
    return resultHtml;
}

/**
 * Merges chronological list of thoughts, combining adjacent tool_calls and tool_responses
 * into a single unified tool_execution object.
 */
export function mergeToolCallsAndResponses(thoughts, isSessionActive = false) {
    const merged = [];
    const openToolCalls = {}; // Key: agent + "_" + tool_name -> Array of call objects

    thoughts.forEach(t => {
        if (t.type === "tool_call") {
            const key = `${t.agent}_${t.tool_name}`;
            if (!openToolCalls[key]) {
                openToolCalls[key] = [];
            }
            const callObj = {
                ...t,
                type: "tool_execution",
                response: null,
                response_timestamp: null,
                is_staled: !isSessionActive
            };
            openToolCalls[key].push(callObj);
            merged.push(callObj);
        } else if (t.type === "tool_response") {
            const key = `${t.agent}_${t.tool_name}`;
            if (openToolCalls[key] && openToolCalls[key].length > 0) {
                const callObj = openToolCalls[key].shift();
                callObj.response = t.response;
                callObj.response_timestamp = t.timestamp;
                callObj.is_staled = false;
                if (!callObj.tool_info && t.tool_info) {
                    callObj.tool_info = t.tool_info;
                }
            } else {
                merged.push({
                    ...t,
                    type: "tool_execution",
                    response: t.response,
                    response_timestamp: t.timestamp,
                    args: {},
                    content: "",
                    is_staled: false
                });
            }
        } else {
            merged.push(t);
        }
    });

    return merged;
}

/**
 * Map agent name to FontAwesome icon class
 */
export function getAgentIcon(agent) {
    switch (agent) {
        case "orchestrator": return "fa-network-wired";
        case "market_intelligence": return "fa-chart-bar";
        case "news_sentiment": return "fa-newspaper";
        case "strategy": return "fa-brain";
        case "risk_manager": return "fa-shield-halved";
        case "execution": return "fa-bolt";
        case "evolution": return "fa-seedling";
        case "system": return "fa-server";
        case "user": return "fa-user";
        default: return "fa-robot";
    }
}

/**
 * Render a single agent thought card into the stream container
 */
export function renderThoughtCard(thought, containerEl, autoscroll, prepend = false) {
    if (!containerEl) return;

    // Remove empty message if it exists
    const emptyMsg = containerEl.querySelector(".empty-thoughts-msg");
    if (emptyMsg) {
        emptyMsg.remove();
    }

    const div = document.createElement("div");
    const agent = thought.agent || "unknown";
    let type = thought.type || "thought";
    const ts = formatTime(thought.timestamp);

    // Normalize tool events into tool_execution
    if (type === "tool_call" || type === "tool_response") {
        type = "tool_execution";
    }

    let isFailed = false;
    let errorMsg = "";
    if (type === "tool_execution" && thought.response !== undefined && thought.response !== null) {
        const resp = thought.response;
        if (typeof resp === "object") {
            if (resp.isError || resp.error !== undefined || resp.success === 0 || resp.success === false) {
                isFailed = true;
                errorMsg = resp.error || (resp.content && resp.content[0] && resp.content[0].text) || "Tool execution failed";
            }
        }
    }

    // Visual classification styling
    let structuralClass = "";
    if (agent !== "orchestrator") {
        structuralClass = "specialist-card";
    }
    if (type === "tool_execution") {
        structuralClass = "tool-card" + (isFailed ? " failed-tool" : "");
        div.setAttribute("data-tool-key", `${agent}_${thought.tool_name}`);
        div.setAttribute("data-timestamp", thought.timestamp);
        div.setAttribute("data-args", JSON.stringify(thought.args || {}));
        div.setAttribute("data-response", JSON.stringify(thought.response !== undefined && thought.response !== null ? thought.response : null));
        div.setAttribute("data-tool-name", thought.tool_name);
        div.setAttribute("data-agent", agent);
        div.setAttribute("data-tool-info", JSON.stringify(thought.tool_info || {}));
    } else if (type === "cycle_complete") {
        structuralClass = "cycle-complete-card";
    }

    div.className = `thought-card ${agent} ${structuralClass}`;

    // Select FontAwesome icon
    const icon = getAgentIcon(agent);

    let bodyHtml = "";
    let typeLabel = "THOUGHT";
    let typeClass = "thought";
    let badgeHtml = "";

    if (type === "thought") {
        const renderedHtml = renderMarkdown(thought.content);
        const rawContent = thought.content || "";
        bodyHtml = `
            <div class="thought-body thought-rendered-view markdown-content">${renderedHtml}</div>
            <div class="thought-body thought-raw-view hidden">
                <div style="display: flex; justify-content: flex-end; margin-bottom: 0.35rem;">
                    <button class="btn btn-xs btn-secondary btn-copy-raw-md" style="font-size: 0.68rem; padding: 0.12rem 0.45rem; display: inline-flex; align-items: center; gap: 0.25rem;">
                        <i class="fa-regular fa-copy"></i> Copy Markdown
                    </button>
                </div>
                <pre class="thought-raw-pre font-mono">${escapeHtml(rawContent)}</pre>
            </div>
        `;
    } else if (type === "cycle_complete") {
        typeLabel = "RUN END";
        typeClass = "cycle_complete";
        
        const status = thought.status || "unknown";
        let statusText = "Unknown";
        let statusIcon = "fa-circle-question";
        let statusClass = "status-unknown";
        if (status === "complete") {
            statusText = "Complete Run";
            statusIcon = "fa-circle-check";
            statusClass = "status-complete";
        } else if (status === "partial") {
            statusText = "Partial Run";
            statusIcon = "fa-triangle-exclamation";
            statusClass = "status-partial";
        } else if (status === "error") {
            statusText = "Error Failure";
            statusIcon = "fa-circle-xmark";
            statusClass = "status-error";
        }

        const durationMs = thought.duration_ms || 0;
        const durationSec = (durationMs / 1000).toFixed(2);
        
        const completed = thought.stages_completed || [];
        const skipped = thought.stages_skipped || [];
        
        let completedHtml = completed.map(s => `
            <div class="stage-pill completed">
                <i class="fa-solid fa-circle-check"></i> ${s.replace("_", " ")}
            </div>
        `).join("");
        
        let skippedHtml = skipped.map(s => `
            <div class="stage-pill skipped">
                <i class="fa-solid fa-circle-minus"></i> ${s.replace("_", " ")}
            </div>
        `).join("");

        let errorDetailsHtml = "";
        if (thought.error) {
            errorDetailsHtml = `
                <div class="cycle-error-details">
                    <div class="error-title"><i class="fa-solid fa-bug"></i> Error Message</div>
                    <pre class="error-trace">${escapeHtml(thought.error)}</pre>
                </div>
            `;
        }

        bodyHtml = `
            <div class="thought-body cycle-summary-card ${statusClass}">
                <div class="cycle-summary-header">
                    <span class="summary-status-title"><i class="fa-solid ${statusIcon}"></i> ${statusText}</span>
                    <span class="summary-duration"><i class="fa-regular fa-clock"></i> ${durationSec}s</span>
                </div>
                
                <div class="cycle-stages-grid">
                    <div class="stages-section">
                        <div class="section-title">Completed Stages</div>
                        <div class="stages-list">${completedHtml || '<span class="text-dim text-small">None</span>'}</div>
                    </div>
                    <div class="stages-section">
                        <div class="section-title">Skipped Stages</div>
                        <div class="stages-list">${skippedHtml || '<span class="text-dim text-small">None</span>'}</div>
                    </div>
                </div>
                ${errorDetailsHtml}
            </div>
        `;
    } else if (type === "tool_execution") {
        const iconClass = isFailed ? "fa-circle-exclamation text-crimson" : "fa-screwdriver-wrench text-amber";
        typeLabel = `<i class="fa-solid ${iconClass}"></i> Tool: <strong>${escapeHtml(thought.tool_name)}</strong>`;
        typeClass = isFailed ? "tool_error" : "tool_call";
        
        const toolInfo = thought.tool_info || {};
        const isLocal = toolInfo.source === "local";
        badgeHtml = isLocal
            ? `<span class="badge badge-xs badge-secondary" style="display: inline-flex; margin-left: 0.35rem; vertical-align: middle;">Local</span>`
            : `<span class="badge badge-xs badge-mcp" style="display: inline-flex; margin-left: 0.35rem; vertical-align: middle;">MCP</span>`;
        
        let argsHtml = "";
        const args = thought.args || {};
        const keys = Object.keys(args);
        if (keys.length > 0) {
            argsHtml = `<div class="tool-args-list">`;
            keys.forEach(k => {
                const val = typeof args[k] === "object" ? JSON.stringify(args[k]) : String(args[k]);
                argsHtml += `
                    <div class="tool-arg-row">
                        <span class="arg-key">${escapeHtml(k)}:</span>
                        <span class="arg-val">${escapeHtml(val)}</span>
                    </div>
                `;
            });
            argsHtml += `</div>`;
        }
        
        let responseSummary = "";
        const resp = thought.response;
        
        if (resp !== undefined && resp !== null) {
            if (isFailed) {
                responseSummary = `
                    <div class="tool-args-list" style="border-top: 1px dashed var(--crimson-border); padding-top: 0.35rem; margin-top: 0.35rem;">
                        <div class="tool-arg-row text-crimson" style="font-weight: 600;">
                            <i class="fa-solid fa-circle-exclamation"></i> Connection Error / Execution Failed
                        </div>
                        <div style="color: var(--crimson); font-size: 0.8rem; margin-top: 0.2rem; white-space: pre-wrap; font-family: var(--font-mono);">${escapeHtml(String(errorMsg))}</div>
                    </div>
                `;
            } else if (typeof resp === "object") {
                const rKeys = Object.keys(resp).slice(0, 5); // limit to 5 preview keys
                if (rKeys.length > 0) {
                    responseSummary = `<div class="tool-args-list" style="border-top: 1px dashed var(--border-color); padding-top: 0.35rem; margin-top: 0.35rem;">`;
                    rKeys.forEach(k => {
                        if (typeof resp[k] !== "object") {
                            responseSummary += `
                                <div class="tool-arg-row">
                                    <span class="arg-key">${escapeHtml(k)}:</span>
                                    <span class="arg-val">${escapeHtml(String(resp[k]))}</span>
                                </div>
                            `;
                        }
                    });
                    responseSummary += `</div>`;
                }
            } else {
                responseSummary = `
                    <div class="tool-args-list" style="border-top: 1px dashed var(--border-color); padding-top: 0.35rem; margin-top: 0.35rem;">
                        <div class="tool-arg-row">
                            <span class="arg-val">${escapeHtml(String(resp))}</span>
                        </div>
                    </div>
                `;
            }
        } else {
            if (thought.is_staled) {
                responseSummary = `
                    <div class="tool-call-line text-dim" style="border-top: 1px dashed var(--border-color); padding-top: 0.35rem; margin-top: 0.35rem; font-style: italic; font-size: 0.76rem;">
                        <i class="fa-solid fa-circle-exclamation text-dim" style="margin-right: 0.2rem;"></i> Response unavailable (session ended)
                    </div>
                `;
            } else {
                responseSummary = `
                    <div class="tool-call-line" style="border-top: 1px dashed var(--border-color); padding-top: 0.35rem; margin-top: 0.35rem; font-style: italic;">
                        <i class="fa-solid fa-circle-notch fa-spin text-amber"></i> Executing tool...
                    </div>
                `;
            }
        }
        
        bodyHtml = `
            <div class="thought-body tool-info">
                ${argsHtml}
                ${responseSummary}
            </div>
            <div class="tool-card-footer" style="display: flex; justify-content: flex-end; margin-top: 0.25rem;">
                <button class="btn btn-sm btn-secondary btn-inspect-tool" style="font-size: 0.72rem; padding: 0.15rem 0.45rem;">
                    <i class="fa-solid fa-sliders"></i> Inspect Raw Data
                </button>
            </div>
        `;
    }

    div.innerHTML = `
        <div class="thought-header">
            <span class="agent-chip ${agent}"><i class="fa-solid ${icon}"></i> ${escapeHtml(agent.replace("_", " "))}</span>
            <div style="display: flex; align-items: center; gap: 0.35rem;">
                <span class="thought-type-badge ${typeClass}">${typeLabel}</span>
                ${type === "thought" ? `
                    <button class="btn btn-xs btn-outline btn-toggle-card-md" title="Toggle Raw / Rendered Markdown" style="font-size: 0.65rem; padding: 0.1rem 0.35rem; display: inline-flex; align-items: center; gap: 0.25rem; border-radius: 4px; cursor: pointer;">
                        <i class="fa-brands fa-markdown text-dim"></i> <span class="toggle-label">Raw</span>
                    </button>
                ` : ""}
                ${badgeHtml}
                <span class="thought-time">${ts}</span>
            </div>
        </div>
        ${bodyHtml}
    `;

    // Wire up per-card markdown toggle & copy
    const toggleBtn = div.querySelector(".btn-toggle-card-md");
    const renderedView = div.querySelector(".thought-rendered-view");
    const rawView = div.querySelector(".thought-raw-view");
    const copyRawBtn = div.querySelector(".btn-copy-raw-md");

    if (toggleBtn && renderedView && rawView) {
        toggleBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            const isRaw = !rawView.classList.contains("hidden");
            if (isRaw) {
                rawView.classList.add("hidden");
                renderedView.classList.remove("hidden");
                toggleBtn.querySelector(".toggle-label").textContent = "Raw";
                toggleBtn.classList.remove("active");
            } else {
                renderedView.classList.add("hidden");
                rawView.classList.remove("hidden");
                toggleBtn.querySelector(".toggle-label").textContent = "Rendered";
                toggleBtn.classList.add("active");
            }
        });
    }

    if (copyRawBtn) {
        copyRawBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(thought.content || "");
            copyRawBtn.innerHTML = `<i class="fa-solid fa-check text-emerald"></i> Copied!`;
            setTimeout(() => {
                copyRawBtn.innerHTML = `<i class="fa-regular fa-copy"></i> Copy Markdown`;
            }, 1500);
        });
    }

    if (window.renderMathInElement) {
        const renderedEl = div.querySelector(".thought-rendered-view");
        if (renderedEl) {
            window.renderMathInElement(renderedEl, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '\\(', right: '\\)', display: false},
                    {left: '\\[', right: '\\]', display: true}
                ],
                ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
                throwOnError: false
            });
        }
    }

    if (prepend) {
        containerEl.prepend(div);
    } else {
        containerEl.appendChild(div);
    }

    // Limit maximum child count to avoid browser memory strain
    if (containerEl.children.length > 500) {
        if (prepend) {
            containerEl.removeChild(containerEl.lastChild);
        } else {
            containerEl.removeChild(containerEl.firstChild);
        }
    }

    if (autoscroll) {
        if (prepend) {
            containerEl.scrollTop = 0;
        } else {
            containerEl.scrollTop = containerEl.scrollHeight;
        }
    }
}
