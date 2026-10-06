/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — Main Application Coordinator (ES6 Module)
   ═══════════════════════════════════════════════════════════════════════ */

import * as api from "./api.js";
import { TechnicalChart } from "./chart.js";
import * as ui from "./components.js";
import {
    channelBreakdown, etLong, formatPct, isLiveSession, relativeVolume,
    signalLean, vwapDistance,
} from "./components/signal_display.js";
import { alignSignalDays, equityValues, nextDayFollowThrough } from "./components/equity_overlay.js";
import { initMemoryController } from "./memory_controller.js";
import {
    openDetailDrawer, closeDetailDrawer, switchDrawerTab,
    openSessionDrawer, openEvolutionDetailDrawer,
    handleCopyClick, initDrawerEvents,
} from "./drawer_controller.js";

// URL Hash Router Helpers
function parseHash() {
    const hash = location.hash.replace("#", "");
    if (!hash) return { tab: null, sessionId: null, memorySubTab: null, memorySelection: null, memoryExtra: null };
    
    if (hash.startsWith("explorer/")) {
        const parts = hash.split("/");
        return { tab: "explorer", sessionId: parts[1], memorySubTab: null, memorySelection: null, memoryExtra: null };
    }
    
    if (hash.startsWith("memory/")) {
        const parts = hash.split("/");
        return {
            tab: "memory",
            sessionId: null,
            memorySubTab: parts[1] || null,
            memorySelection: parts[2] ? decodeURIComponent(parts[2]) : null,
            memoryExtra: parts[3] ? decodeURIComponent(parts[3]) : null
        };
    }
    
    if (hash === "dashboard" || hash === "explorer" || hash === "memory") {
        return { tab: hash, sessionId: null, memorySubTab: null, memorySelection: null, memoryExtra: null };
    }
    
    return { tab: null, sessionId: null, memorySubTab: null, memorySelection: null, memoryExtra: null };
}

function getInitialTab() {
    const parsed = parseHash();
    if (parsed.tab) return parsed.tab;
    return localStorage.getItem("gd_active_tab") || "dashboard";
}

function getInitialSessionId() {
    const parsed = parseHash();
    if (parsed.sessionId) return parsed.sessionId;
    return localStorage.getItem("gd_active_session_id") || null;
}

// State Configuration
const parsedHash = parseHash();
const state = {
    activeTab: getInitialTab(),
    activeSessionId: getInitialSessionId(),
    currentOnlineSessionId: null,
    systemStatus: "idle",
    autoscroll: true,
    autoscrollLogs: localStorage.getItem("gd_drawer_autoscroll") !== "false",
    isInitialLoad: true,
    drawerExpanded: localStorage.getItem("gd_drawer_expanded") === "true",
    chartMode: "account",  // 'account' | 'pnl'
    // The combined signal drawn under the account / P&L curve (remembered).
    signalOverlay: localStorage.getItem("gd_perf_signal_overlay") === "true",
    techChartMode: "signals", // 'signals' | 'strategies' | 'technicals'
    performancePeriod: "week",
    latestTechPoints: [],
    latestMarketSnapshot: null,
    
    // Memory tab state
    memorySubTab: parsedHash.memorySubTab || "vector",
    notesFiles: [],
    activeNoteFile: parsedHash.memorySubTab === "notes" ? parsedHash.memorySelection : null,
    activeNoteSource: null,  // "user" | "evolution"
    isEditingNewNote: false,
    
    // Instruction selection
    activeAgent: parsedHash.memorySubTab === "instructions" && parsedHash.memorySelection ? { name: parsedHash.memorySelection } : null,
    activeInstructionVersion: parsedHash.memorySubTab === "instructions" ? parsedHash.memoryExtra : null,

    // Algorithms selection
    selectedAlgorithmVersion: parsedHash.memorySubTab === "algorithms" ? parsedHash.memorySelection : null,
    
    // Reviews selection
    activeReviewId: parsedHash.memorySubTab === "reviews" ? parsedHash.memorySelection : null,
    
    // Concurrency / Redirect state
    shouldSwitchToActiveSession: false,
    
    // Polling configuration
    pollIntervalSeconds: 10
};

let eventSource = null;
let secondsUntilNextCycle = null;
let secondsUntilNextEvolution = null;
let cronTimerId = null;
const techChart = new TechnicalChart("indicatorChart");
const perfChart = new TechnicalChart("perfChart");

// DOM Elements
const loginModal = document.getElementById("login-modal");
const loginForm = document.getElementById("login-form");
const loginPassword = document.getElementById("login-password");
const loginError = document.getElementById("login-error");
const logoutBtn = document.getElementById("logout-btn");
const btnRefreshToken = document.getElementById("btn-refresh-token");

// Navigation & Tab Containers
const navDashboard = document.getElementById("nav-dashboard");
const navExplorer = document.getElementById("nav-explorer");
const navMemory = document.getElementById("nav-memory");
const viewDashboard = document.getElementById("view-dashboard");
const viewExplorer = document.getElementById("view-explorer");
const viewMemory = document.getElementById("view-memory");

// Synchronously apply initial tab classes to prevent layout flashing during load/auth check
if (navDashboard && navExplorer && navMemory && viewDashboard && viewExplorer && viewMemory) {
    const initTab = state.activeTab;
    navDashboard.classList.toggle("active", initTab === "dashboard");
    navExplorer.classList.toggle("active", initTab === "explorer");
    navMemory.classList.toggle("active", initTab === "memory");
    viewDashboard.classList.toggle("active", initTab === "dashboard");
    viewExplorer.classList.toggle("active", initTab === "explorer");
    viewMemory.classList.toggle("active", initTab === "memory");
}

// Dashboard Buttons
const btnSync = document.getElementById("btn-sync");
const btnTrigger = document.getElementById("btn-trigger");

// Explorer Controls
const btnAutoscroll = document.getElementById("explorer-btn-autoscroll");
const thoughtsContainer = document.getElementById("explorer-thoughts-container");

// Bottom Log Drawer Controls
const logDrawer = document.getElementById("log-drawer");
const logDrawerHeader = document.getElementById("log-drawer-header");
const btnToggleDrawer = document.getElementById("drawer-btn-toggle");
const btnClearDrawer = document.getElementById("drawer-btn-clear");
const btnAutoscrollDrawer = document.getElementById("drawer-btn-autoscroll");
const logContainer = document.getElementById("drawer-log-container");

// Dashboard & Layout Components
const evolutionContainer = document.getElementById("evolution-container");
const journalTableBody = document.getElementById("journal-table-body");
const syncStatus = document.getElementById("sync-status");
const holdingsTableBody = document.getElementById("holdings-table-body");
const holdingsTotalPnl = document.getElementById("holdings-total-pnl");
const btnChartTabAccount = document.getElementById("btn-chart-tab-account");
const btnChartTabPnl = document.getElementById("btn-chart-tab-pnl");
const btnChartSignal = document.getElementById("btn-chart-signal-overlay");
const btnLogDeposit = document.getElementById("btn-log-deposit");
const perfChartTitle = document.getElementById("perf-chart-title");
const indicatorGridAccount = document.getElementById("indicator-grid-account");
const indicatorGridPnl = document.getElementById("indicator-grid-pnl");
const approvalPanel = document.getElementById("approval-panel");
const approvalContent = document.getElementById("approval-card-content");
const dashboardCyclesContainer = document.getElementById("dashboard-cycles-container");

// Header Badges
const badgeTicker = document.getElementById("badge-ticker");
const badgeVersion = document.getElementById("badge-version");
const badgeMode = document.getElementById("badge-mode");
const badgeModeText = document.getElementById("badge-mode-text");

const badgeStatus = document.getElementById("badge-status");
const badgeStatusText = document.getElementById("badge-status-text");
const brokerTotal = document.getElementById("broker-total");
const brokerCash = document.getElementById("broker-cash");
const brokerBp = document.getElementById("broker-bp");
const todayPnl = document.getElementById("today-pnl");
const todayTrades = document.getElementById("today-trades");
const lossStreak = document.getElementById("loss-streak");

// Cycle Explorer Containers
const cycleListContainer = document.getElementById("explorer-cycles-container");
const explorerSelectedTitle = document.getElementById("explorer-selected-title");

// Detail Drawer — managed by drawer_controller.js

// ── AUTHENTICATION MANAGEMENT ──

async function checkAuth() {
    const token = api.getAuthToken();
    if (token) {
        if (loginModal) loginModal.classList.add("hidden");
        initDashboard();
        return;
    }

    // Proactively check if the backend has password authentication enabled.
    // Ask the login endpoint, not the portfolio: the portfolio calls the
    // broker, which can wait minutes on a pending sign-in.
    try {
        const response = await fetch("/api/auth/verify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: "" }),
        });
        if (response.status === 401) {
            // Password protection is active, show the prompt
            if (loginModal) loginModal.classList.remove("hidden");
        } else {
            // Password protection is disabled, bypass authentication
            if (loginModal) loginModal.classList.add("hidden");
            initDashboard();
        }
    } catch (err) {
        console.error("Auth capability check failed:", err);
        // Fallback: show the login modal
        if (loginModal) loginModal.classList.remove("hidden");
    }
}

if (loginForm) {
    loginForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const pwd = loginPassword.value;
        try {
            await api.verifyPassword(pwd);
            loginModal.classList.add("hidden");
            loginError.classList.add("hidden");
            loginPassword.value = "";
            initDashboard();
        } catch (err) {
            console.error("Auth error:", err);
            loginError.classList.remove("hidden");
        }
    });
}

if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
        api.clearAuthToken();
        if (eventSource) {
            eventSource.close();
        }
        location.reload();
    });
}

if (btnRefreshToken) {
    btnRefreshToken.addEventListener("click", async () => {
        if (btnRefreshToken.classList.contains("refreshing")) return; // debounce
        const icon = btnRefreshToken.querySelector("i");
        btnRefreshToken.classList.add("refreshing");
        if (icon) { icon.className = "fa-solid fa-arrows-rotate"; }
        try {
            await api.refreshOAuth();
            btnRefreshToken.classList.remove("refreshing");
            btnRefreshToken.classList.add("refresh-ok");
            if (icon) { icon.className = "fa-solid fa-check"; }
            setTimeout(() => {
                btnRefreshToken.classList.remove("refresh-ok");
                if (icon) { icon.className = "fa-solid fa-link"; }
            }, 2000);
        } catch (err) {
            console.error("Failed to refresh token:", err);
            btnRefreshToken.classList.remove("refreshing");
            if (icon) { icon.className = "fa-solid fa-link"; }
        }
    });
}

// ── TAB SELECTION ROUTER ──

function switchTab(targetTab, updateHash = true) {
    state.activeTab = targetTab;
    localStorage.setItem("gd_active_tab", targetTab);

    if (updateHash) {
        if (targetTab === "explorer" && state.activeSessionId) {
            location.hash = `explorer/${state.activeSessionId}`;
        } else if (targetTab === "memory") {
            let memoryHash = `memory/${state.memorySubTab}`;
            if (state.memorySubTab === "notes" && state.activeNoteFile) {
                memoryHash += `/${encodeURIComponent(state.activeNoteFile)}`;
            } else if (state.memorySubTab === "instructions") {
                if (state.activeAgent) {
                    memoryHash += `/${encodeURIComponent(state.activeAgent.name)}`;
                    if (state.activeInstructionVersion) {
                        memoryHash += `/${encodeURIComponent(state.activeInstructionVersion)}`;
                    }
                }
            } else if (state.memorySubTab === "algorithms" && state.selectedAlgorithmVersion) {
                memoryHash += `/${encodeURIComponent(state.selectedAlgorithmVersion)}`;
            } else if (state.memorySubTab === "reviews" && state.activeReviewId) {
                memoryHash += `/${encodeURIComponent(state.activeReviewId)}`;
            }
            location.hash = memoryHash;
        } else {
            location.hash = targetTab;
        }
    }

    // Toggle active tab buttons
    navDashboard.classList.toggle("active", targetTab === "dashboard");
    navExplorer.classList.toggle("active", targetTab === "explorer");
    if (navMemory) navMemory.classList.toggle("active", targetTab === "memory");

    // Toggle tab panels
    viewDashboard.classList.toggle("active", targetTab === "dashboard");
    viewExplorer.classList.toggle("active", targetTab === "explorer");
    if (viewMemory) viewMemory.classList.toggle("active", targetTab === "memory");

    if (targetTab === "explorer") {
        refreshExplorerData();
    } else if (targetTab === "memory") {
        if (window.loadMemoryTab) window.loadMemoryTab();
    }
}

if (navDashboard && navExplorer) {
    navDashboard.addEventListener("click", () => switchTab("dashboard"));
    navExplorer.addEventListener("click", () => switchTab("explorer"));
    if (navMemory) {
        navMemory.addEventListener("click", () => switchTab("memory"));
    }
}

// ── BOTTOM LOG DRAWER ROUTER ──

function toggleDrawer(forceState = null) {
    state.drawerExpanded = forceState !== null ? forceState : !state.drawerExpanded;
    localStorage.setItem("gd_drawer_expanded", state.drawerExpanded);
    
    if (logDrawer) {
        logDrawer.classList.toggle("collapsed", !state.drawerExpanded);
    }
    document.body.classList.toggle("drawer-expanded", state.drawerExpanded);
    
    if (btnToggleDrawer) {
        const icon = btnToggleDrawer.querySelector("i");
        if (icon) {
            if (state.drawerExpanded) {
                icon.className = "fa-solid fa-chevron-down";
            } else {
                icon.className = "fa-solid fa-chevron-up";
            }
        }
    }
}

// ── INITIALIZE DASHBOARD ──

function initDashboard() {
    // Initialize Memory Controllers
    initMemoryController(state, appendLogLine, loadDashboardData);

    // Switch to saved tab, but ensure url hash matches
    switchTab(state.activeTab, true);

    // Launch real-time stream listener
    setupSSE();

    // Setup chart plots
    techChart.init();
    perfChart.init();

    // Load static metrics
    loadDashboardData();

    // Bind event controllers
    setupEventHandlers();

    // Initialize Bottom Log Drawer
    toggleDrawer(state.drawerExpanded);
    if (btnAutoscrollDrawer) {
        btnAutoscrollDrawer.classList.toggle("active", state.autoscrollLogs);
    }

    // Periodic dashboard poll (configurable interval)
    scheduleDashboardPoll();
}

function scheduleDashboardPoll() {
    setTimeout(async () => {
        try {
            await loadDashboardData();
        } catch (e) {
            console.error("Dashboard poll failed:", e);
        }
        scheduleDashboardPoll();
    }, (state.pollIntervalSeconds || 10) * 1000);
}

// Detail Inspection Drawer — managed by drawer_controller.js

// ── EVOLUTION CONFIRMATION POPUP ──

/**
 * Show a confirmation popup for the evolution cycle, with an optional
 * textarea for user questions or comments to pass to the evolution agent.
 * Returns null if cancelled, or { comment: string } if confirmed.
 */
function showEvolutionConfirm() {
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "custom-modal-overlay";

        overlay.innerHTML = `
            <div class="custom-modal-card glass-card">
                <div class="custom-modal-header">
                    <h3>
                        <i class="fa-solid fa-dna text-purple"></i>
                        <span>Start Evolution Cycle</span>
                    </h3>
                    <button class="custom-modal-close-btn" aria-label="Close dialog">&times;</button>
                </div>
                <div class="custom-modal-body">
                    <p>Are you sure you want to start a new self-evolution cycle? The agent will evaluate recent performance and may update its trading strategy.</p>
                    <div style="margin-top: 1rem;">
                        <label for="evo-user-comment" style="display: block; font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 0.4rem;">
                            <i class="fa-solid fa-comment-dots" style="margin-right: 0.3rem; opacity: 0.7;"></i>Questions or comments for the agent <span style="opacity: 0.5;">(optional)</span>
                        </label>
                        <textarea
                            id="evo-user-comment"
                            class="form-input"
                            rows="3"
                            placeholder="e.g. There hasn't been any trade for the last 10 cycles — is the strategy too conservative?"
                            style="width: 100%; resize: vertical; min-height: 60px; max-height: 200px; font-size: 0.9rem; font-family: var(--font-sans); padding: 0.6rem 0.75rem; border-radius: var(--border-radius); border: 1px solid var(--border-color); background: var(--bg-input, var(--bg-card)); color: var(--text-primary); line-height: 1.5;"
                        ></textarea>
                    </div>
                </div>
                <div class="custom-modal-footer">
                    <button class="btn btn-secondary custom-modal-cancel">Cancel</button>
                    <button class="btn btn-purple custom-modal-confirm">Start Evolution</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        // Trigger reflow for transition
        overlay.offsetHeight;
        overlay.classList.add("active");

        let resolved = false;

        const cleanup = () => {
            overlay.classList.remove("active");
            document.removeEventListener("keydown", handleKeyDown);
            setTimeout(() => {
                overlay.remove();
            }, 250);
        };

        const getComment = () => {
            const textarea = overlay.querySelector("#evo-user-comment");
            return textarea ? textarea.value.trim() : "";
        };

        const handleConfirm = () => {
            if (resolved) return;
            resolved = true;
            resolve({ comment: getComment() });
            cleanup();
        };

        const handleCancel = () => {
            if (resolved) return;
            resolved = true;
            resolve(null);
            cleanup();
        };

        const focusableElements = overlay.querySelectorAll('button, textarea, [href], input, select, [tabindex]:not([tabindex="-1"])');
        const firstFocusable = focusableElements[0];
        const lastFocusable = focusableElements[focusableElements.length - 1];

        const handleKeyDown = (e) => {
            if (e.key === "Escape") {
                handleCancel();
            } else if (e.key === "Enter" && !e.target.matches("textarea")) {
                e.preventDefault();
                handleConfirm();
            } else if (e.key === "Tab") {
                if (e.shiftKey) {
                    if (document.activeElement === firstFocusable) {
                        lastFocusable.focus();
                        e.preventDefault();
                    }
                } else {
                    if (document.activeElement === lastFocusable) {
                        firstFocusable.focus();
                        e.preventDefault();
                    }
                }
            }
        };

        overlay.querySelector(".custom-modal-confirm").addEventListener("click", handleConfirm);
        overlay.querySelector(".custom-modal-cancel").addEventListener("click", handleCancel);
        overlay.querySelector(".custom-modal-close-btn").addEventListener("click", handleCancel);

        overlay.querySelector(".custom-modal-card").addEventListener("click", (e) => {
            e.stopPropagation();
        });

        overlay.addEventListener("click", handleCancel);
        document.addEventListener("keydown", handleKeyDown);

        // Focus the confirm button by default
        overlay.querySelector(".custom-modal-confirm").focus();
    });
}

// ── EVENT REGISTERING ──

function setupEventHandlers() {
    window.addEventListener("select-run", (e) => {
        const sessionId = e.detail.sessionId;
        switchTab("explorer");
        if (state.activeSessionId !== sessionId) {
            selectCycle(sessionId);
        }
    });

    const periodSelect = document.getElementById("performance-period");
    if (periodSelect) {
        periodSelect.addEventListener("change", (e) => {
            state.performancePeriod = e.target.value;
            loadDashboardData();
        });
    }

    if (btnTrigger) {
        btnTrigger.addEventListener("click", async () => {
            try {
                if (state.systemStatus === "running" || state.systemStatus === "waiting_approval") {
                    // It's currently running, so we should CANCEL
                    btnTrigger.disabled = true;
                    appendLogLine("[SYSTEM] Requesting cycle cancellation...");
                    const res = await api.cancelActiveCycle();
                    appendLogLine(`[SYSTEM] ${res.message}`);
                } else {
                    // It's idle, so confirm before triggering
                    const confirmed = await ui.showConfirm(
                        "Start Trade Cycle",
                        "Are you sure you want to start a new trade cycle? The agent will analyze the market and may execute live trades.",
                        { confirmText: "Start Trade" }
                    );
                    if (!confirmed) return;
                    btnTrigger.disabled = true;
                    state.shouldSwitchToActiveSession = true;
                    appendLogLine("[SYSTEM] Triggering trade...");
                    const res = await api.triggerCycle();
                    appendLogLine(`[SYSTEM] ${res.message}`);
                }
            } catch (err) {
                appendLogLine(`[ERROR] Failed to execute action: ${err.message}`);
                state.shouldSwitchToActiveSession = false;
                btnTrigger.disabled = false;
            }
        });
    }

    const btnTriggerEvolution = document.getElementById("btn-trigger-evolution");
    if (btnTriggerEvolution) {
        btnTriggerEvolution.addEventListener("click", async () => {
            try {
                if (state.systemStatus === "evolving") {
                    btnTriggerEvolution.disabled = true;
                    appendLogLine("[SYSTEM] Requesting evolution cancellation...");
                    const res = await api.cancelActiveEvolution();
                    appendLogLine(`[SYSTEM] ${res.message}`);
                } else {
                    // Show evolution confirmation popup with optional comment input
                    const result = await showEvolutionConfirm();
                    if (!result) return;
                    btnTriggerEvolution.disabled = true;
                    state.shouldSwitchToActiveSession = true;
                    const userComment = result.comment || "";
                    if (userComment) {
                        appendLogLine(`[SYSTEM] Triggering self-evolution engine with user input: "${userComment.substring(0, 80)}${userComment.length > 80 ? '…' : ''}"`);
                    } else {
                        appendLogLine("[SYSTEM] Triggering self-evolution engine...");
                    }
                    const res = await api.triggerEvolution(userComment);
                    appendLogLine(`[SYSTEM] ${res.message}`);
                }
            } catch (err) {
                appendLogLine(`[ERROR] Failed to execute action: ${err.message}`);
                state.shouldSwitchToActiveSession = false;
                btnTriggerEvolution.disabled = false;
            }
        });
    }


    if (btnSync) {
        btnSync.addEventListener("click", async () => {
            const icon = btnSync.querySelector("i");
            try {
                // Use pointer-events and opacity to prevent double clicks without natively
                // disabling the button, which would pause child CSS animations (like fa-spin) in Safari/WebKit.
                btnSync.style.pointerEvents = "none";
                btnSync.style.opacity = "0.6";
                if (icon) icon.classList.add("fa-spin");
                
                appendLogLine("[SYSTEM] Initiating position alignment reconciliation...");
                // Enforce a minimum display time of 800ms for the spinning animation to ensure visual feedback
                const [res] = await Promise.all([
                    api.reconcilePositions(),
                    new Promise(resolve => setTimeout(resolve, 800))
                ]);
                appendLogLine(`[SYSTEM] Reconciliation complete: ${res.message}`);
                loadDashboardData();
            } catch (err) {
                appendLogLine(`[ERROR] Failed to reconcile: ${err.message}`);
            } finally {
                btnSync.style.pointerEvents = "";
                btnSync.style.opacity = "";
                if (icon) icon.classList.remove("fa-spin");
            }
        });
    }

    // Chart tab toggling (2 tabs: account / pnl — tech chart is always visible separately)
    function switchChartTab(newMode) {
        if (newMode !== "account" && newMode !== "pnl") return;
        if (state.chartMode === newMode) return;
        state.chartMode = newMode;

        // Update button states
        const tabs = [
            { btn: btnChartTabAccount, mode: "account" },
            { btn: btnChartTabPnl, mode: "pnl" },
        ];
        tabs.forEach(({ btn, mode }) => {
            if (!btn) return;
            btn.classList.toggle("btn-primary", mode === newMode);
            btn.classList.toggle("btn-secondary", mode !== newMode);
        });

        // Toggle indicator grids
        if (indicatorGridAccount) indicatorGridAccount.style.display = newMode === "account" ? "" : "none";
        if (indicatorGridPnl) indicatorGridPnl.style.display = newMode === "pnl" ? "" : "none";

        // Update header title
        if (perfChartTitle) {
            const titles = {
                account: '<i class="fa-solid fa-wallet text-emerald"></i> Account Value',
                pnl: '<i class="fa-solid fa-chart-area text-cyan"></i> Trading P&L',
            };
            perfChartTitle.innerHTML = titles[newMode];
        }

        refreshPerfChart();
    }

    if (btnChartTabAccount) btnChartTabAccount.addEventListener("click", () => switchChartTab("account"));
    if (btnChartTabPnl) btnChartTabPnl.addEventListener("click", () => switchChartTab("pnl"));

    // Signal overlay on the account / P&L chart
    showSignalToggle();
    if (btnChartSignal) {
        btnChartSignal.addEventListener("click", () => {
            state.signalOverlay = !state.signalOverlay;
            localStorage.setItem("gd_perf_signal_overlay", String(state.signalOverlay));
            showSignalToggle();
            refreshPerfChart();
        });
    }

    // Technical & Signal Chart Mode Tabs (Signals vs Strategies vs Technicals)
    const tabSignals = document.getElementById("tab-chart-signals");
    const tabStrategies = document.getElementById("tab-chart-strategies");
    const tabTechnicals = document.getElementById("tab-chart-technicals");
    const techChartContainer = document.getElementById("tech-chart-container");
    const subSignalsContainer = document.getElementById("sub-signals-container");
    const technicalsMatrixContainer = document.getElementById("technicals-matrix-container");

    function setTechChartTab(mode) {
        state.techChartMode = mode;
        if (tabSignals) tabSignals.classList.toggle("active", mode === "signals");
        if (tabStrategies) tabStrategies.classList.toggle("active", mode === "strategies");
        if (tabTechnicals) tabTechnicals.classList.toggle("active", mode === "technicals");

        if (techChartContainer) techChartContainer.style.display = (mode === "signals") ? "block" : "none";
        if (subSignalsContainer) subSignalsContainer.style.display = (mode === "strategies") ? "flex" : "none";
        if (technicalsMatrixContainer) technicalsMatrixContainer.style.display = (mode === "technicals") ? "flex" : "none";

        if (mode === "signals") {
            techChart.showSignalsContext(state.latestTechPoints || []);
        }
    }

    if (tabSignals) tabSignals.addEventListener("click", () => setTechChartTab("signals"));
    if (tabStrategies) tabStrategies.addEventListener("click", () => setTechChartTab("strategies"));
    if (tabTechnicals) tabTechnicals.addEventListener("click", () => setTechChartTab("technicals"));

    // Cash adjustment modal
    if (btnLogDeposit) {
        btnLogDeposit.addEventListener("click", () => showCashAdjustmentModal());
    }

    // Toggle LLM details panel
    const btnToggleLlmDetails = document.getElementById("btn-toggle-llm-details");
    const llmDetailsGrid = document.getElementById("llm-details-grid");
    if (btnToggleLlmDetails && llmDetailsGrid) {
        btnToggleLlmDetails.addEventListener("click", () => {
            const isHidden = llmDetailsGrid.style.display === "none";
            if (isHidden) {
                llmDetailsGrid.style.display = "grid";
                btnToggleLlmDetails.innerHTML = '<i class="fa-solid fa-chevron-up"></i> Hide Details';
            } else {
                llmDetailsGrid.style.display = "none";
                btnToggleLlmDetails.innerHTML = '<i class="fa-solid fa-chevron-down"></i> Details';
            }
        });
    }

    if (btnAutoscroll) {
        btnAutoscroll.addEventListener("click", () => {
            state.autoscroll = !state.autoscroll;
            btnAutoscroll.classList.toggle("active", state.autoscroll);
        });
    }

    const btnToggleMarkdown = document.getElementById("explorer-btn-toggle-markdown");
    if (btnToggleMarkdown) {
        btnToggleMarkdown.addEventListener("click", () => {
            state.viewRawMarkdown = !state.viewRawMarkdown;
            btnToggleMarkdown.classList.toggle("active", state.viewRawMarkdown);
            btnToggleMarkdown.innerHTML = state.viewRawMarkdown
                ? `<i class="fa-brands fa-markdown text-purple"></i> <span>Rendered View</span>`
                : `<i class="fa-brands fa-markdown"></i> <span>Raw Markdown</span>`;

            if (thoughtsContainer) {
                const thoughtCards = thoughtsContainer.querySelectorAll(".thought-card");
                thoughtCards.forEach(card => {
                    const renderedView = card.querySelector(".thought-rendered-view");
                    const rawView = card.querySelector(".thought-raw-view");
                    const cardToggle = card.querySelector(".btn-toggle-card-md");
                    if (renderedView && rawView) {
                        if (state.viewRawMarkdown) {
                            renderedView.classList.add("hidden");
                            rawView.classList.remove("hidden");
                            if (cardToggle) {
                                cardToggle.querySelector(".toggle-label").textContent = "Rendered";
                                cardToggle.classList.add("active");
                            }
                        } else {
                            rawView.classList.add("hidden");
                            renderedView.classList.remove("hidden");
                            if (cardToggle) {
                                cardToggle.querySelector(".toggle-label").textContent = "Raw";
                                cardToggle.classList.remove("active");
                            }
                        }
                    }
                });
            }
        });
    }

    const btnDeleteCycle = document.getElementById("explorer-btn-delete-cycle");
    if (btnDeleteCycle) {
        btnDeleteCycle.addEventListener("click", () => {
            if (state.activeSessionId) {
                handleDeleteCycle(state.activeSessionId);
            }
        });
    }

    const btnContinueCycle = document.getElementById("explorer-btn-continue-cycle");
    if (btnContinueCycle) {
        btnContinueCycle.addEventListener("click", () => {
            if (state.activeSessionId) {
                handleContinueCycle(state.activeSessionId);
            }
        });
    }

    // Bottom Log Drawer Event Bindings
    if (logDrawerHeader) {
        logDrawerHeader.addEventListener("click", (e) => {
            if (e.target.closest(".drawer-actions") || e.target.closest("button")) {
                return;
            }
            toggleDrawer();
        });
    }

    if (btnToggleDrawer) {
        btnToggleDrawer.addEventListener("click", (e) => {
            e.stopPropagation();
            toggleDrawer();
        });
    }

    if (btnClearDrawer) {
        btnClearDrawer.addEventListener("click", (e) => {
            e.stopPropagation();
            logContainer.innerHTML = '<div class="log-line system-line">[SYSTEM] System logs cleared.</div>';
        });
    }

    if (btnAutoscrollDrawer) {
        btnAutoscrollDrawer.addEventListener("click", (e) => {
            e.stopPropagation();
            state.autoscrollLogs = !state.autoscrollLogs;
            localStorage.setItem("gd_drawer_autoscroll", state.autoscrollLogs);
            btnAutoscrollDrawer.classList.toggle("active", state.autoscrollLogs);
        });
    }

    // Detail Drawer events (close, tabs, copy) — managed by drawer_controller.js
    initDrawerEvents();

    // Escape key closes detail drawer
    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            closeDetailDrawer();
        }
    });

    // Delegate "Inspect Raw Data" clicks in thoughtsContainer
    if (thoughtsContainer) {
        thoughtsContainer.addEventListener("click", (e) => {
            const btn = e.target.closest(".btn-inspect-tool");
            if (btn) {
                const card = btn.closest(".thought-card");
                if (card) {
                    openDetailDrawer(card);
                }
            }
        });
    }

    // Indicator tooltips via modal
    document.querySelectorAll(".btn-indicator-info").forEach(btn => {
        btn.addEventListener("click", () => {
            const indicator = btn.getAttribute("data-indicator");
            let title = "Indicator";
            let msg = "";
            if (indicator === "rsi") {
                title = "RSI (14) - Relative Strength Index";
                msg = "Measures momentum and speed of price movements on a 0–100 scale.\n\n• Values > 70 generally suggest overbought conditions (potential pullback).\n• Values < 30 suggest oversold conditions (potential bounce).";
            } else if (indicator === "bb") {
                title = "Bollinger Width";
                msg = "Measures market volatility by calculating the distance between the upper and lower Bollinger Bands relative to the middle band.\n\n• Low values indicate a 'volatility squeeze' (compression often preceding a major breakout).\n• High values indicate wide volatility expansion.";
            } else if (indicator === "vwap") {
                title = "VWAP Distance";
                msg = "Volume-Weighted Average Price Distance. Shows the percentage difference between the current price and intraday VWAP.\n\n• Positive values indicate price is trading above VWAP (intraday bullish control).\n• Negative values indicate price is trading below VWAP (intraday bearish control).";
            } else if (indicator === "macd") {
                title = "MACD Histogram - Moving Average Convergence Divergence";
                msg = "Measures momentum shift and velocity between fast (12) and slow (26) moving averages relative to the 9-day signal line.\n\n• Positive / expanding green values indicate accelerating bullish momentum.\n• Negative / expanding red values indicate accelerating bearish downward momentum.";
            } else if (indicator === "adx") {
                title = "ADX (14) - Average Directional Index";
                msg = "Quantifies the overall trend strength regardless of direction on a 0–100 scale.\n\n• Values > 25 indicate a strong directional trend (favorable for momentum & trend-following strategies).\n• Values < 20 indicate weak trend or consolidation/range-bound chop (favorable for mean-reversion strategies).";
            } else if (indicator === "atr") {
                title = "ATR (14) - Average True Range";
                msg = "Measures market volatility in dollars by calculating the average range of recent price movements.\n\n• Higher values indicate larger candle ranges and expanded volatility (used for dynamic stop-loss width & position sizing).\n• Lower values indicate compressed price action.";
            }
            ui.showAlert(title, msg);
        });
    });

    const cycleToggle = document.getElementById("cycle-cron-toggle");
    if (cycleToggle) {
        cycleToggle.addEventListener("change", async () => {
            try {
                cycleToggle.disabled = true;
                const enabled = cycleToggle.checked;
                appendLogLine(`[SYSTEM] Toggling cycle scheduler auto-trigger to ${enabled ? "ON" : "OFF"}...`);
                const res = await api.toggleCron(enabled, "cycle");
                appendLogLine(`[SYSTEM] ${res.message}`);
                await loadDashboardData();
            } catch (err) {
                appendLogLine(`[ERROR] Failed to toggle cycle scheduler: ${err.message}`);
                cycleToggle.checked = !cycleToggle.checked;
            } finally {
                cycleToggle.disabled = false;
            }
        });
    }

    const evolutionToggle = document.getElementById("evolution-cron-toggle");
    if (evolutionToggle) {
        evolutionToggle.addEventListener("change", async () => {
            try {
                evolutionToggle.disabled = true;
                const enabled = evolutionToggle.checked;
                appendLogLine(`[SYSTEM] Toggling evolution scheduler auto-trigger to ${enabled ? "ON" : "OFF"}...`);
                const res = await api.toggleCron(enabled, "evolution");
                appendLogLine(`[SYSTEM] ${res.message}`);
                await loadDashboardData();
            } catch (err) {
                appendLogLine(`[ERROR] Failed to toggle evolution scheduler: ${err.message}`);
                evolutionToggle.checked = !evolutionToggle.checked;
            } finally {
                evolutionToggle.disabled = false;
            }
        });
    }
}

// ── DATA QUERY LOOPS ──

/** Refresh the performance chart (Account Value or P&L equity curve) */
async function refreshPerfChart() {
    try {
        const metrics = await api.fetchPortfolioMetrics(state.performancePeriod);
        await renderPerfChart(metrics);
    } catch (err) {
        console.error("Failed to refresh performance chart:", err);
    }
}

/** The toggle's pressed state follows state.signalOverlay. */
function showSignalToggle() {
    if (!btnChartSignal) return;
    btnChartSignal.classList.toggle("active", state.signalOverlay);
    btnChartSignal.setAttribute("aria-pressed", String(state.signalOverlay));
}

/**
 * Draw the equity curve for this period's metrics, with the combined signal
 * under it when the overlay is on, then the stat badges and the overlay's
 * next-day summary.
 */
async function renderPerfChart(metrics) {
    const history = metrics.history || [];
    const adj = metrics.adjustments || [];
    const startingValue = metrics.starting_value || 0;
    const totalCapitalBase = metrics.total_capital_base || 0;
    let signalDays = null;
    let overlayError = false;
    if (state.signalOverlay) {
        try {
            const res = await api.fetchSignalDaily(state.performancePeriod);
            signalDays = alignSignalDays(history, res.days || []);
        } catch (err) {
            console.warn("Failed to load the signal history:", err);
            overlayError = true;
        }
    }
    const container = document.getElementById("perf-chart-container");
    if (container) container.classList.toggle("with-signal", !!signalDays);
    perfChart.showEquityCurve(history, adj, state.chartMode, startingValue, signalDays);
    updateEquityStats(history, adj, state.chartMode, startingValue, totalCapitalBase);
    renderSignalInsight(history, signalDays, overlayError);
}

/**
 * The strip under the chart: after the days the signal leaned one way, how
 * often the curve moved that way by the next day the system ran.
 */
function renderSignalInsight(history, signalDays, failed = false) {
    const box = document.getElementById("signal-overlay-insight");
    if (!box) return;
    box.hidden = !state.signalOverlay;
    if (!state.signalOverlay) return;
    const body = box.querySelector(".insight-body");
    if (!body) return;
    body.replaceChildren();

    const note = (text) => {
        const span = document.createElement("span");
        span.className = "insight-muted";
        span.textContent = text;
        body.append(span);
    };
    const chip = (tone, strong, text, title) => {
        const span = document.createElement("span");
        span.className = `insight-chip insight-${tone}`;
        const dot = document.createElement("i");
        dot.className = "insight-dot";
        const b = document.createElement("b");
        b.textContent = strong;
        span.append(dot, b, document.createTextNode(" " + text));
        if (title) span.title = title;
        body.append(span);
    };

    box.title = "Each day's average combined signal against the change in the curve by the next day " +
        "the system ran (the account without deposits, or realized P&L).";
    if (failed || !signalDays) return note("Signal history unavailable.");
    if (!signalDays.some(Boolean)) return note("No signal readings in this period.");

    const stats = nextDayFollowThrough(equityValues(history, state.chartMode), signalDays);
    if (stats.days === 0) return note("Needs a second day with readings to compare.");
    const gain = state.chartMode === "pnl" ? "a realized gain" : "a gain";
    const loss = state.chartMode === "pnl" ? "a realized loss" : "a loss";
    if (stats.long.days) {
        chip("long", `${stats.long.up} of ${stats.long.days}`, `long-leaning days were followed by ${gain}`);
    } else {
        note("No long-leaning days.");
    }
    if (stats.short.days) {
        chip("short", `${stats.short.down} of ${stats.short.days}`, `short-leaning days were followed by ${loss}`);
    } else {
        note("No short-leaning days.");
    }
    const r = stats.r === null ? "n/a" : (stats.r >= 0 ? "+" : "\u2212") + Math.abs(stats.r).toFixed(2);
    chip(
        "neutral",
        r,
        `correlation of the day's signal with the next day's change · ${stats.days} day${stats.days === 1 ? "" : "s"}`,
        "Each day's average combined signal against the change in the curve to the next day " +
        "the system ran. Over a few weeks this describes the period; it is not evidence of an edge.",
    );
}

/** Refresh the technical and algorithm signals chart */
async function refreshTechChart() {
    try {
        const techData = await api.fetchTechChart(500, state.performancePeriod);
        state.latestTechPoints = techData.points || [];

        const latest = techData.latest || (techData.points && techData.points.length > 0 ? techData.points[techData.points.length - 1] : null);
        if (latest) {
            updateMarketTechnicalsAndSignals(latest);
        }

        // The chart canvas is only visible on the History tab; the other tabs
        // redraw it on switch (setTechChartTab).
        if ((state.techChartMode || 'signals') === 'signals') {
            techChart.showSignalsContext(state.latestTechPoints);
        }
    } catch (err) {
        console.error("Failed to refresh tech chart:", err);
    }
}

/** Update the stat badges below the chart for Account or P&L mode.
 *
 * Data contract from /api/portfolio-metrics:
 *   history[].portfolio_value    — raw broker account value
 *   history[].cumulative_pnl    — running realized P&L
 *   history[].cumulative_deposits — running total of all deposits/withdrawals
 *   starting_value              — broker value at period start (0 if account opened in-period)
 *   total_capital_base          — sum of ALL deposits (period-independent)
 */
function updateEquityStats(history, adjustments, mode, startingValue = 0, totalCapitalBase = 0) {
    if (!history || history.length === 0) return;

    const fmt = (v) => '$' + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const signFmt = (v) => (v >= 0 ? '+' : '-') + fmt(v);
    const colorClass = (v) => v >= 0 ? 'text-emerald' : 'text-crimson';
    const el = (id) => document.getElementById(id);

    const first = history[0];
    const last = history[history.length - 1];

    if (mode === 'account') {
        // Net Gain = current broker value - starting broker value - in-period deposits
        // For "all time" (startingValue === 0), use totalCapitalBase directly because
        // the delta formula misses deposits on the first history day (first.cumulative_deposits
        // already includes them, so the subtraction undercounts).
        const inPeriodDeposits = startingValue === 0
            ? totalCapitalBase
            : (last.cumulative_deposits || 0) - (first.cumulative_deposits || 0);
        const netGain = last.portfolio_value - startingValue - inPeriodDeposits;

        if (el('stat-account-value')) el('stat-account-value').textContent = fmt(last.portfolio_value);
        if (el('stat-total-deposits')) el('stat-total-deposits').textContent = totalCapitalBase !== 0 ? fmt(totalCapitalBase) : '--';
        const netGainEl = el('stat-net-gain');
        if (netGainEl) {
            netGainEl.textContent = signFmt(netGain);
            netGainEl.className = 'font-mono ' + colorClass(netGain);
        }
    } else if (mode === 'pnl') {
        const periodPnl = (last.cumulative_pnl || 0) - (first.cumulative_pnl || 0);
        const capitalBase = totalCapitalBase > 0 ? totalCapitalBase : (startingValue > 0 ? startingValue : 0);
        const returnPct = capitalBase > 0 ? (periodPnl / capitalBase) * 100 : 0;

        const pnlEl = el('stat-total-pnl');
        if (pnlEl) {
            pnlEl.textContent = signFmt(periodPnl);
            pnlEl.className = 'font-mono ' + colorClass(periodPnl);
        }
        if (el('stat-pnl-deposits')) el('stat-pnl-deposits').textContent = totalCapitalBase !== 0 ? fmt(totalCapitalBase) : '--';
        const retEl = el('stat-return-pct');
        if (retEl) {
            retEl.textContent = (returnPct >= 0 ? '+' : '') + returnPct.toFixed(2) + '%';
            retEl.className = 'font-mono ' + colorClass(returnPct);
        }
    }
}

// Global function to update proposal badges on navigation tabs
window.updateProposalBadges = async function() {
    try {
        const [inst, algo, rev] = await Promise.all([
            api.fetchInstructions(),
            api.fetchAlgorithms(),
            api.fetchReviews()
        ]);
        
        let pendingInst = 0;
        if (inst && inst.agents) {
            inst.agents.forEach(a => { if (a.has_proposed) pendingInst++; });
        }
        
        let pendingAlgo = 0;
        if (algo && algo.versions) {
            algo.versions.forEach(a => {
                if ((a.status === "proposed" || a.status === "PENDING_REVIEW") && a.version !== algo.active_version) pendingAlgo++;
            });
        }
        
        let pendingRev = 0;
        if (rev && rev.reviews) {
            rev.reviews.forEach(r => {
                if (r.status === "PENDING_REVIEW" || r.status === "proposed") pendingRev++;
            });
        }
        
        const totalPending = pendingInst + pendingAlgo + pendingRev;
        
        const badgeMemory = document.getElementById("badge-nav-memory");
        const badgeInst = document.getElementById("badge-nav-instructions");
        const badgeAlgo = document.getElementById("badge-nav-algorithms");
        const badgeRev = document.getElementById("badge-nav-reviews");
        
        if (badgeMemory) {
            badgeMemory.textContent = totalPending;
            badgeMemory.classList.toggle("hidden", totalPending === 0);
        }
        if (badgeInst) {
            badgeInst.textContent = pendingInst;
            badgeInst.classList.toggle("hidden", pendingInst === 0);
        }
        if (badgeAlgo) {
            badgeAlgo.textContent = pendingAlgo;
            badgeAlgo.classList.toggle("hidden", pendingAlgo === 0);
        }
        if (badgeRev) {
            badgeRev.textContent = pendingRev;
            badgeRev.classList.toggle("hidden", pendingRev === 0);
        }
    } catch (err) {
        console.warn("Failed to update proposal badges:", err);
    }
};

async function loadDashboardData() {
    try {
        const portfolioData = await api.fetchPortfolio(state.performancePeriod);
        
        if (portfolioData.local && portfolioData.local.dashboard_poll_interval_seconds) {
            state.pollIntervalSeconds = portfolioData.local.dashboard_poll_interval_seconds;
        }
        
        // Update header & cards
        ui.renderPortfolio(portfolioData, {
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
        });

        // Render Holdings & Performance
        // Fetch ALL trades (unfiltered) for position alignment — needs full history
        const allTradesData = await api.fetchTrades(50, "all");
        ui.renderHoldings(portfolioData, allTradesData.trades, holdingsTableBody, holdingsTotalPnl, syncStatus);

        // Set status badge
        updateStatusBadge(portfolioData.status);

        // Fetch trade journal to draw tables (filtered by selected period)
        const tradesData = await api.fetchTrades(500, state.performancePeriod);
        ui.renderJournal(tradesData.trades, journalTableBody);

        // Fetch dedicated market snapshots for the technical chart
        // Fetch dedicated market snapshots for the technical & signals chart
        let techPoints = [];
        let latestSnapshot = null;
        try {
            const techRes = await api.fetchTechChart(500, state.performancePeriod);
            if (techRes) {
                techPoints = techRes.points || [];
                latestSnapshot = techRes.latest || (techPoints.length > 0 ? techPoints[techPoints.length - 1] : null);
            }
        } catch (err) {
            console.warn("Failed to load tech chart data:", err);
        }

        state.latestTechPoints = techPoints;
        if (latestSnapshot) {
            updateMarketTechnicalsAndSignals(latestSnapshot);
        }

        // Populate the signal chart, drivers list or indicators matrix for the active tab
        const techChartEl = document.getElementById("tech-chart-container");
        const subSignalsEl = document.getElementById("sub-signals-container");
        const technicalsMatrixEl = document.getElementById("technicals-matrix-container");
        const techMode = state.techChartMode || 'signals';
        if (techChartEl) techChartEl.style.display = (techMode === 'signals') ? "block" : "none";
        if (subSignalsEl) subSignalsEl.style.display = (techMode === 'strategies') ? "flex" : "none";
        if (technicalsMatrixEl) technicalsMatrixEl.style.display = (techMode === 'technicals') ? "flex" : "none";

        if (techMode === 'signals') {
            techChart.showSignalsContext(techPoints);
        }

        // Always populate the performance chart (left panel — period-filtered)
        try {
            const metrics = await api.fetchPortfolioMetrics(state.performancePeriod);
            await renderPerfChart(metrics);
        } catch (chartErr) {
            console.warn("Failed to load portfolio metrics for chart:", chartErr);
        }
        state.isInitialLoad = false;

        // Render self-evolution log
        const evolutionData = await api.fetchEvolution();
        ui.renderEvolution(evolutionData.evolution, evolutionContainer, (item) => openEvolutionDetailDrawer(item, switchTab));

        // Fetch and render Token & LLM metrics analysis + Dashboard Cycles
        let cycles = [];
        try {
            const [llmMetricsData, cyclesData] = await Promise.all([
                api.fetchLlmMetrics(state.performancePeriod).catch(err => {
                    console.error("Failed to load LLM & token metrics:", err);
                    return {};
                }),
                api.fetchCycles().catch(err => {
                    console.error("Failed to load cycles:", err);
                    return { cycles: [] };
                })
            ]);
            cycles = cyclesData.cycles || [];
            state.cycles = cycles;
            window.state = state;

            ui.renderLlmMetrics(llmMetricsData, cycles);
        } catch (err) {
            console.error("Failed to load LLM metrics or cycles:", err);
        }

        // Manage pending user order approval gate
        if (portfolioData.pending_order) {
            ui.renderPendingOrder(
                portfolioData.pending_order, 
                approvalPanel, 
                approvalContent, 
                resolveOrderProposal
            );
        } else {
            approvalPanel.classList.add("hidden");
        }

        // Render recent cycles list on Dashboard summary (last 20 runs)
        const recentCycles = cycles.slice(0, 20);
        ui.renderCycleList(recentCycles, state.activeSessionId, dashboardCyclesContainer, (sessionId) => {
            switchTab("explorer");
            selectCycle(sessionId);
        }, handleDeleteCycle);

        // If cycle history explorer is active, refresh the cycles list
        if (state.activeTab === "explorer") {
            refreshCyclesListOnly();
        }

        // Fetch proposal counts
        await window.updateProposalBadges();

        // Fetch cron scheduler status
        try {
            const cronStatusData = await api.fetchCronStatus();
            updateCronUI(cronStatusData);
        } catch (cronErr) {
            console.error("Error fetching cron status:", cronErr);
        }

    } catch (err) {
        console.error("Error refreshing dashboard data:", err);
    }
}

function formatCountdown(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined || totalSeconds < 0) {
        return "--:--:--";
    }
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    
    const hh = h.toString().padStart(2, "0");
    const mm = m.toString().padStart(2, "0");
    const ss = s.toString().padStart(2, "0");
    
    return `${hh}:${mm}:${ss}`;
}

function updateCountdownElements() {
    const cycleCountdown = document.getElementById("cycle-cron-countdown");
    const evolutionCountdown = document.getElementById("evolution-cron-countdown");
    const cycleToggle = document.getElementById("cycle-cron-toggle");
    const evolutionToggle = document.getElementById("evolution-cron-toggle");
    
    const cycleEnabled = cycleToggle ? cycleToggle.checked : false;
    const evolutionEnabled = evolutionToggle ? evolutionToggle.checked : false;
    
    if (cycleCountdown) {
        if (!cycleEnabled || secondsUntilNextCycle === null) {
            cycleCountdown.textContent = "--:--:--";
        } else if (secondsUntilNextCycle > 0) {
            cycleCountdown.textContent = formatCountdown(secondsUntilNextCycle);
        } else {
            cycleCountdown.textContent = "Running...";
        }
    }
    
    if (evolutionCountdown) {
        if (!evolutionEnabled || secondsUntilNextEvolution === null) {
            evolutionCountdown.textContent = "--:--:--";
        } else if (secondsUntilNextEvolution > 0) {
            evolutionCountdown.textContent = formatCountdown(secondsUntilNextEvolution);
        } else {
            evolutionCountdown.textContent = "Running...";
        }
    }
}

function updateCronUI(data) {
    const cycleToggle = document.getElementById("cycle-cron-toggle");
    const evolutionToggle = document.getElementById("evolution-cron-toggle");
    const badgeCycleScheduler = document.getElementById("badge-cycle-scheduler");
    const badgeEvolveScheduler = document.getElementById("badge-evolve-scheduler");
    
    if (cycleToggle) {
        cycleToggle.checked = !!data.cycle_cron_enabled;
    }
    if (evolutionToggle) {
        evolutionToggle.checked = !!data.evolution_cron_enabled;
    }
    
    if (badgeCycleScheduler) {
        if (data.cron_expression) {
            badgeCycleScheduler.style.display = "flex";
            badgeCycleScheduler.title = `Automated Trading Cycle scheduler (Expression: ${data.cron_expression})`;
        } else {
            badgeCycleScheduler.style.display = "none";
        }
    }
    
    if (badgeEvolveScheduler) {
        if (data.evolution_cron_expression) {
            badgeEvolveScheduler.style.display = "flex";
            badgeEvolveScheduler.title = `Automated Self-Evolution scheduler (Expression: ${data.evolution_cron_expression})`;
        } else {
            badgeEvolveScheduler.style.display = "none";
        }
    }
    
    secondsUntilNextCycle = data.seconds_until_next_cycle;
    secondsUntilNextEvolution = data.seconds_until_next_evolution;
    
    updateCountdownElements();
    
    if (cronTimerId) {
        clearInterval(cronTimerId);
        cronTimerId = null;
    }
    
    const needsTimer = (data.cycle_cron_enabled && secondsUntilNextCycle !== null) || 
                       (data.evolution_cron_enabled && secondsUntilNextEvolution !== null);
                       
    if (needsTimer) {
        cronTimerId = setInterval(() => {
            if (secondsUntilNextCycle !== null && secondsUntilNextCycle > 0) {
                secondsUntilNextCycle--;
            }
            if (secondsUntilNextEvolution !== null && secondsUntilNextEvolution > 0) {
                secondsUntilNextEvolution--;
            }
            updateCountdownElements();
        }, 1000);
    }
}



async function handleDeleteCycle(sessionId) {
    const confirmed = await ui.showConfirm(
        "Delete & Revert Cycle Run",
        "Are you sure you want to delete and revert this cycle? This will rollback all associated database logs, trade records, filesystem edits, and vector DB memories!",
        { confirmText: "Delete", isWarning: true }
    );
    if (!confirmed) {
        return;
    }
    try {
        await api.deleteCycle(sessionId);
        if (state.activeSessionId === sessionId) {
            state.activeSessionId = null;
            localStorage.removeItem("gd_active_session_id");
        }
        await loadDashboardData();
        if (state.activeTab === "explorer") {
            await refreshExplorerData();
        }
        await ui.showAlert("Reverted Successfully", `Cycle session '${sessionId}' was successfully deleted and all associated changes rolled back.`);
    } catch (err) {
        await ui.showAlert("Revert Failed", "Failed to delete and revert cycle: " + err.message, { isError: true });
    }
}

async function handleContinueCycle(sessionId) {
    const confirmed = await ui.showConfirm(
        "Continue Evolution Cycle",
        `Resume the timed-out evolution cycle? The agent's previous analysis will be restored and it will continue where it left off.`,
        { confirmText: "Continue" }
    );
    if (!confirmed) return;

    try {
        const result = await api.continueEvolution(sessionId);

        // Immediately update UI — hide Continue button, swap badge to running
        const continueBtn = document.getElementById("explorer-btn-continue-cycle");
        if (continueBtn) continueBtn.classList.add("hidden");

        // The SSE "evolving" status event will refresh the dashboard automatically,
        // but give instant visual feedback on the selected card's status badge
        const activeCard = document.querySelector(".cycle-item-card.active .cycle-status-badge.status-error");
        if (activeCard) {
            activeCard.classList.remove("status-error");
            activeCard.classList.add("status-running");
            activeCard.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Continuing';
        }
    } catch (err) {
        await ui.showAlert(
            "Continue Failed",
            "Failed to resume evolution cycle: " + err.message,
            { isError: true },
        );
    }
}

function updateEvolutionBadge(status) {
    // Intentionally left blank to align with Trade button experience (no running badge)
}

// ── CYCLE HISTORY VIEW CONTROLLER ──

async function refreshExplorerData() {
    try {
        const cyclesData = await api.fetchCycles();
        const cycles = cyclesData.cycles || [];
        
        if (state.systemStatus !== "idle" && cycles.length > 0) {
            state.currentOnlineSessionId = cycles[0].session_id;
        }
        
        // If there's no active selected session ID yet, default to the first one
        if (!state.activeSessionId && cycles.length > 0) {
            state.activeSessionId = cycles[0].session_id;
            localStorage.setItem("gd_active_session_id", state.activeSessionId);
        }

        // Align hash if we have a session selected
        if (state.activeSessionId && state.activeTab === "explorer") {
            const expectedHash = `#explorer/${state.activeSessionId}`;
            if (location.hash !== expectedHash) {
                location.hash = `explorer/${state.activeSessionId}`;
            }
        }

        ui.renderCycleList(cycles, state.activeSessionId, cycleListContainer, selectCycle, handleDeleteCycle);

        const deleteBtn = document.getElementById("explorer-btn-delete-cycle");
        if (deleteBtn) {
            deleteBtn.classList.toggle("hidden", !state.activeSessionId);
        }
        const continueBtn = document.getElementById("explorer-btn-continue-cycle");
        if (continueBtn) {
            // Hide continue button until selectCycle determines if it's resumable
            continueBtn.classList.add("hidden");
        }

        if (state.activeSessionId) {
            loadSelectedCycleThoughts(state.activeSessionId);
        } else {
            thoughtsContainer.innerHTML = `
                <div class="empty-thoughts-msg text-dim text-small">
                    No cycle selected. Select a cycle from the sidebar to inspect.
                </div>
            `;
            explorerSelectedTitle.textContent = "No Cycle Selected";
        }
    } catch (err) {
        console.error("Error loading explorer runs:", err);
    }
}

async function refreshCyclesListOnly() {
    try {
        const cyclesData = await api.fetchCycles();
        ui.renderCycleList(cyclesData.cycles || [], state.activeSessionId, cycleListContainer, selectCycle, handleDeleteCycle);
    } catch (err) {
        console.error("Error refreshing cycles list:", err);
    }
}

async function selectCycle(sessionId) {
    state.activeSessionId = sessionId;
    localStorage.setItem("gd_active_session_id", sessionId);

    // Update URL hash to reflect selected session
    if (state.activeTab === "explorer") {
        const expectedHash = `#explorer/${sessionId}`;
        if (location.hash !== expectedHash) {
            location.hash = `explorer/${sessionId}`;
        }
    }

    // Re-render sidebar to highlight active
    const cyclesData = await api.fetchCycles();
    const cycles = cyclesData.cycles || [];
    ui.renderCycleList(cycles, sessionId, cycleListContainer, selectCycle, handleDeleteCycle);

    // Show/hide the Continue button based on whether this is a resumable evolution cycle
    const continueBtn = document.getElementById("explorer-btn-continue-cycle");
    if (continueBtn) {
        const selectedCycle = cycles.find(c => c.session_id === sessionId);
        const isResumable = selectedCycle
            && (selectedCycle.is_evolution === 1 || selectedCycle.is_evolution === true)
            && selectedCycle.cycle_status === "error";
        continueBtn.classList.toggle("hidden", !isResumable);
    }

    loadSelectedCycleThoughts(sessionId);
}

async function loadSelectedCycleThoughts(sessionId) {
    const deleteBtn = document.getElementById("explorer-btn-delete-cycle");
    if (deleteBtn) {
        deleteBtn.classList.remove("hidden");
    }

    try {
        explorerSelectedTitle.innerHTML = `Cycle Explorer (Session: ${sessionId}) <i class="fa-solid fa-spinner fa-spin" style="margin-left: 0.5rem; font-size: 0.85rem; color: var(--text-secondary);"></i>`;
        thoughtsContainer.classList.add("loading-thoughts");

        const thoughtsData = await api.fetchThoughts(sessionId);
        
        thoughtsContainer.classList.remove("loading-thoughts");
        explorerSelectedTitle.textContent = `Cycle Explorer (Session: ${sessionId})`;
        thoughtsContainer.innerHTML = "";
        
        const thoughts = thoughtsData.thoughts || [];
        if (thoughts.length === 0) {
            thoughtsContainer.innerHTML = `
                <div class="empty-thoughts-msg text-dim text-small">
                    No thoughts or tool logs found for this cycle.
                </div>
            `;
        } else {
            // Merge tool calls and responses into unified cards
            const isSessionActive = (sessionId === state.currentOnlineSessionId && state.systemStatus !== "idle");
            const processedThoughts = ui.mergeToolCallsAndResponses(thoughts, isSessionActive);
            // Render newest thoughts first at the top
            processedThoughts.reverse();
            processedThoughts.forEach(t => ui.renderThoughtCard(t, thoughtsContainer, false, false));
            // Keep scroll position at the top (newest thoughts)
            thoughtsContainer.scrollTop = 0;
        }
    } catch (err) {
        thoughtsContainer.classList.remove("loading-thoughts");
        explorerSelectedTitle.textContent = `Cycle Explorer (Session: ${sessionId})`;
        console.error("Failed to load selected cycle thoughts:", err);
        thoughtsContainer.innerHTML = `
            <div class="empty-thoughts-msg text-crimson text-small">
                Error loading cycle logs from database: ${err.message}
            </div>
        `;
    }
}

// ── REAL-TIME EVENT HANDLERS (SSE) ──

function setupSSE() {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource("/api/events");

    eventSource.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleIncomingEvent(msg);
        } catch (err) {
            console.error("Error parsing event payload:", err);
        }
    };

    eventSource.onerror = () => {
        console.warn("SSE connection interrupted. Attempting reconnect...");
        if (badgeStatusText) badgeStatusText.textContent = "DISCONNECTED";
        if (badgeStatus) badgeStatus.className = "badge status-idle";
    };
}

function handleIncomingEvent(event) {
    switch (event.type) {
        case "status":
            updateStatusBadge(event.data);
            if (event.data === "idle") {
                state.currentOnlineSessionId = null;
                // Instantly sync dashboards once processing ends
                loadDashboardData();
                if (state.activeTab === "explorer") {
                    refreshExplorerData();
                }
            }
            break;
        case "log":
            appendLogLine(event.data);
            break;
        case "thought":
            const thoughtSessionId = event.data.session_id;
            state.currentOnlineSessionId = thoughtSessionId;
            if (thoughtSessionId && state.activeSessionId !== thoughtSessionId) {
                state.activeSessionId = thoughtSessionId;
                localStorage.setItem("gd_active_session_id", thoughtSessionId);
                
                // Update hash to reflect new running session
                if (state.activeTab === "explorer" || state.shouldSwitchToActiveSession) {
                    const expectedHash = `#explorer/${thoughtSessionId}`;
                    if (location.hash !== expectedHash) {
                        location.hash = `explorer/${thoughtSessionId}`;
                    }
                }
                
                // Clear explorer thoughts stream for the new session
                thoughtsContainer.innerHTML = "";
                if (logContainer) {
                    appendLogLine(`[SYSTEM] New cycle started (Session: ${thoughtSessionId}).`);
                }
                
                // Refresh cycles list sidebar
                refreshCyclesListOnly();

                if (state.shouldSwitchToActiveSession) {
                    state.shouldSwitchToActiveSession = false;
                    switchTab("explorer");
                    selectCycle(thoughtSessionId);
                }
            }
            
            // Stream thoughts to thoughtsContainer if the thought's session ID matches active (newest on top)
            if (thoughtSessionId === state.activeSessionId) {
                const incoming = event.data;
                if (incoming.type === "tool_response") {
                    // Update Technical & Signal panel in real-time when market data is gathered
                    if (incoming.tool_name === "gather_market_data" && incoming.response) {
                        const resp = incoming.response;
                        updateMarketTechnicalsAndSignals(resp);
                        const rsiVal = resp.indicators ? resp.indicators.rsi_14 : null;
                        const compositeSig = resp.algo_signal ? resp.algo_signal.composite_signal : null;
                        techChart.updatePoint(rsiVal, compositeSig, resp);
                    }

                    const existingCard = thoughtsContainer.querySelector(`[data-tool-key="${incoming.agent}_${incoming.tool_name}"]`);
                    if (existingCard) {
                        const merged = {
                            agent: incoming.agent,
                            type: "tool_execution",
                            tool_name: incoming.tool_name,
                            timestamp: existingCard.getAttribute("data-timestamp") || incoming.timestamp,
                            args: JSON.parse(existingCard.getAttribute("data-args") || "{}"),
                            response: incoming.response,
                            response_timestamp: incoming.timestamp
                        };
                        const tempContainer = document.createElement("div");
                        ui.renderThoughtCard(merged, tempContainer, false, false);
                        const newCard = tempContainer.firstElementChild;
                        if (newCard) {
                            thoughtsContainer.replaceChild(newCard, existingCard);
                        }
                        return;
                    }
                }
                ui.renderThoughtCard(incoming, thoughtsContainer, state.autoscroll, true);
            }
            break;
        case "clear_thoughts":
            thoughtsContainer.innerHTML = `
                <div class="empty-thoughts-msg text-dim text-small">
                    Waiting for next cycle. Trigger a trading cycle or evolution run to see agent thoughts!
                </div>
            `;
            break;
        case "pending_order":
            ui.renderPendingOrder(event.data, approvalPanel, approvalContent, resolveOrderProposal);
            break;
        case "reconciled":
            appendLogLine(`[SYSTEM] Reconciliation complete: ${event.data.message}`);
            loadDashboardData();
            break;
        case "indicators":
            updateMarketTechnicalsAndSignals(event.data);
            techChart.updatePoint(event.data.rsi_14, event.data.composite_signal, event.data);
            break;
        case "cron_toggle":
            const cycleToggle = document.getElementById("cycle-cron-toggle");
            if (cycleToggle) {
                cycleToggle.checked = !!event.data.cycle_cron_enabled;
            }
            const evolutionToggle = document.getElementById("evolution-cron-toggle");
            if (evolutionToggle) {
                evolutionToggle.checked = !!event.data.evolution_cron_enabled;
            }
            loadDashboardData();
            break;
        case "oauth_required":
            showOAuthModal(event.data.url);
            break;
        case "oauth_complete":
            if (oauthModal) { oauthModal.style.display = "none"; }
            setTimeout(() => location.reload(), 1500);
            break;
    }
}

function updateStatusBadge(status) {
    if (!badgeStatusText || !badgeStatus) return;
    badgeStatusText.textContent = status.toUpperCase();
    state.systemStatus = status;
    
    let disableCycle = false;
    let disableEvolve = false;
    let evolveStatusText = "Idle";
    
    if (status === "idle") {
        badgeStatus.className = "badge status-idle";
    } else if (status === "running") {
        badgeStatus.className = "badge status-running";
        disableCycle = true;
        disableEvolve = true;
    } else if (status === "waiting_approval") {
        badgeStatus.className = "badge status-waiting_approval";
        disableCycle = true;
        disableEvolve = true;
    } else if (status === "evolving") {
        badgeStatus.className = "badge status-evolving";
        disableCycle = true;
        disableEvolve = true;
        evolveStatusText = "Running...";
    }
    
    if (status === "running") {
        if (btnTrigger) {
            btnTrigger.innerHTML = `<i class="fa-solid fa-stop"></i> Stop`;
            btnTrigger.className = "btn btn-sm btn-danger";
            btnTrigger.disabled = false;
        }
    } else {
        if (btnTrigger) {
            btnTrigger.innerHTML = `<i class="fa-solid fa-play"></i> Trade`;
            btnTrigger.className = "btn btn-sm btn-primary";
            btnTrigger.disabled = disableCycle;
        }
    }
    
    const btnTriggerEvolution = document.getElementById("btn-trigger-evolution");
    if (btnTriggerEvolution) {
        if (status === "evolving") {
            btnTriggerEvolution.innerHTML = `<i class="fa-solid fa-stop"></i> Stop`;
            btnTriggerEvolution.disabled = false;
        } else {
            btnTriggerEvolution.innerHTML = `<i class="fa-solid fa-play"></i> Evolve`;
            btnTriggerEvolution.disabled = disableEvolve;
        }
    }
    
    // evolutionStatusVal removed

    // Update Self-Evolution Engine run status badge
    updateEvolutionBadge(status);
}

function appendLogLine(line) {
    if (!logContainer) return;
    const div = document.createElement("div");
    div.className = "log-line";

    if (line.includes("│ INFO    │")) {
        div.classList.add("info-line");
    } else if (line.includes("│ WARNING │")) {
        div.classList.add("warn-line");
    } else if (line.includes("│ ERROR   │") || line.includes("│ CRITICAL│")) {
        div.classList.add("error-line");
    } else if (line.includes("[SYSTEM]")) {
        div.classList.add("system-line");
    } else if (line.includes("Agent:")) {
        div.classList.add("agent-line");
    } else {
        div.classList.add("system-line");
    }

    div.textContent = line;
    logContainer.appendChild(div);

    // Protect DOM size limit
    if (logContainer.children.length > 500) {
        logContainer.removeChild(logContainer.firstChild);
    }

    if (state.autoscrollLogs) {
        logContainer.scrollTop = logContainer.scrollHeight;
    }
}

function updateMarketTechnicalsAndSignals(data) {
    if (!data) return;

    state.latestMarketSnapshot = data;

    // The two sources of this panel carry the same facts in different shapes:
    // a stored snapshot (page load) and the live market-data response (cycle).
    const regimeName = typeof data.regime === "string"
        ? data.regime
        : (data.regime && (data.regime.regime || data.regime.name)) || "";
    const closePrice = data.close ?? data.close_price ?? (data.quote ? data.quote.last : null);
    const panelIndicators = data.indicators || data;

    // 1. Ticker badge
    const tickerBadge = document.getElementById("tech-ticker-badge");
    if (tickerBadge) {
        const ticker =
            data.ticker || (data.quote && data.quote.ticker) || ui.getDisplayTicker();
        tickerBadge.textContent = ticker;
    }

    // 1b. Price, the day's move, and when this reading was taken — a reading
    //     from the 17:00 cycle is still on screen at 08:00 the next day.
    const priceEl = document.getElementById("tech-price");
    if (priceEl) {
        priceEl.textContent = closePrice ? `$${Number(closePrice).toFixed(2)}` : "--";
    }
    const dayEl = document.getElementById("tech-day-change");
    if (dayEl) {
        const day = formatPct(data.daily_change_pct);
        dayEl.textContent = day ? `${day} today` : "";
        dayEl.className = "font-mono tech-day-change " + (
            Number(data.daily_change_pct) > 0 ? "pos" : (Number(data.daily_change_pct) < 0 ? "neg" : "")
        );
    }
    const asofEl = document.getElementById("tech-asof");
    if (asofEl) {
        const when = data.timestamp ? etLong(data.timestamp) : "";
        const session = isLiveSession(panelIndicators) ? "" : " · no live session data";
        asofEl.textContent = when ? `as of ${when}${session}` : "";
    }

    // 2. Market Regime Badge
    const regimeBadge = document.getElementById("market-regime-badge");
    const regimeText = document.getElementById("market-regime-text");
    if (regimeBadge && regimeText) {
        let regimeStr = null;
        let regimeConf = null;
        if (typeof data.regime === "string") {
            regimeStr = data.regime;
            regimeConf = data.regime_confidence;
        } else if (data.regime && typeof data.regime === "object") {
            regimeStr = data.regime.regime || data.regime.name;
            regimeConf = data.regime.confidence;
        }

        if (regimeStr) {
            regimeBadge.style.display = "inline-flex";
            const cleanRegime = regimeStr.replace(/_/g, " ").toUpperCase();
            regimeText.textContent = cleanRegime;

            regimeBadge.className = "badge";
            if (cleanRegime.includes("BULL") || cleanRegime.includes("UP")) {
                regimeBadge.classList.add("badge-emerald");
            } else if (cleanRegime.includes("BEAR") || cleanRegime.includes("DOWN") || cleanRegime.includes("GRIND")) {
                regimeBadge.classList.add("badge-crimson");
            } else if (cleanRegime.includes("VOLATILITY")) {
                regimeBadge.classList.add("badge-amber");
            } else {
                regimeBadge.classList.add("badge-cyan");
            }

            if (regimeConf) {
                regimeBadge.title = `Regime: ${cleanRegime} (Confidence: ${Math.round(regimeConf * 100)}%)`;
            }
        }
    }

    // 3. Composite Score & Gauge Meter
    let compositeScore = null;
    if (data.composite_signal !== undefined && data.composite_signal !== null) {
        compositeScore = Number(data.composite_signal);
    } else if (data.algo_signal && data.algo_signal.composite_signal !== undefined) {
        compositeScore = Number(data.algo_signal.composite_signal);
    }

    const scoreValEl = document.getElementById("composite-score-val");
    const actionBadge = document.getElementById("composite-action-badge");
    const gaugeBar = document.getElementById("composite-gauge-bar");
    const gaugeMarker = document.getElementById("composite-gauge-marker");

    if (compositeScore !== null) {
        // The algorithm's lean on the strategy's own thresholds. It is not the
        // decision — the trading agent makes that — so no BUY / PUT verbs.
        const lean = signalLean(compositeScore, regimeName);
        const toneText = { bull: "text-emerald", "bull-soft": "text-emerald", bear: "text-crimson", "bear-soft": "text-crimson" };
        const toneBadge = { bull: "badge-emerald", "bull-soft": "badge-emerald-soft", bear: "badge-crimson", "bear-soft": "badge-crimson-soft" };
        if (scoreValEl) {
            scoreValEl.textContent = (compositeScore >= 0 ? "+" : "") + compositeScore.toFixed(2);
            scoreValEl.className = "font-mono font-bold text-lg " + (toneText[lean.tone] || "text-cyan");
        }

        if (actionBadge) {
            actionBadge.className = "badge " + (toneBadge[lean.tone] || "badge-outline");
            actionBadge.textContent = lean.label;
            actionBadge.title = "The algorithm's lean, not a trade decision";
        }

        const clamped = Math.max(-1.0, Math.min(1.0, compositeScore));
        const markerPercent = ((clamped + 1.0) / 2.0) * 100.0;
        if (gaugeMarker) {
            gaugeMarker.style.left = `${markerPercent}%`;
        }

        if (gaugeBar) {
            if (clamped >= 0) {
                gaugeBar.className = "composite-gauge-bar positive";
                gaugeBar.style.left = "50%";
                gaugeBar.style.width = `${(clamped / 2.0) * 100.0}%`;
                gaugeBar.style.right = "auto";
            } else {
                gaugeBar.className = "composite-gauge-bar negative";
                gaugeBar.style.left = "auto";
                gaugeBar.style.right = "50%";
                gaugeBar.style.width = `${(Math.abs(clamped) / 2.0) * 100.0}%`;
            }
        }
    }

    // 4. Sub-Signals List
    let subSignals = [];
    if (data.sub_signals && Array.isArray(data.sub_signals)) {
        subSignals = data.sub_signals;
    } else if (data.algo_signal && Array.isArray(data.algo_signal.sub_signals)) {
        subSignals = data.algo_signal.sub_signals;
    }

    const algoVersionTag = document.getElementById("algo-version-tag");
    if (algoVersionTag) {
        const v = data.algo_version || (data.algo_signal ? data.algo_signal.algo_version : null);
        // "composite_v030_session_relative_volume_for_volume_readers" -> "v030"
        const short = v ? (String(v).match(/v\d{3}/) || [String(v)])[0] : "";
        algoVersionTag.textContent = short ? `Algorithm ${short}` : "";
        algoVersionTag.title = v || "Version not recorded for this reading";
    }

    const subSignalsList = document.getElementById("sub-signals-list");
    if (subSignalsList) {
        if (subSignals.length === 0) {
            subSignalsList.innerHTML = `<div class="empty-sub-signals text-dim text-xs">No channel data for this reading yet.</div>`;
        } else {
            const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => (
                { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
            ));
            const { voting, silent } = channelBreakdown(subSignals, compositeScore);
            const rows = voting.map(v => {
                const tone = v.value >= 0 ? "pos" : "neg";
                const pct = Math.round(v.share * 100);
                const vote = (v.value >= 0 ? "+" : "") + v.value.toFixed(2);
                const shareText = v.against ? `${pct}% against` : `${pct}% of push`;
                const tip = `${v.name}: vote ${vote} × weight ${v.weight.toFixed(3)}` +
                    (v.reason ? ` (${v.reason})` : "");
                return `
                    <div class="driver-row${v.against ? " against" : ""}" title="${esc(tip)}">
                        <span class="driver-name">${esc(v.label)}</span>
                        <div class="driver-share-track"><div class="driver-share-fill ${tone}" style="width: ${pct}%;"></div></div>
                        <span class="driver-vote ${tone}">${vote}</span>
                        <span class="driver-share">${shareText}</span>
                    </div>
                `;
            }).join("");
            const quiet = silent.map(s => `
                <div class="quiet-channel" title="${esc(s.name + (s.code ? ": " + s.code : ""))}">
                    <span>${esc(s.label)}</span><span class="quiet-reason">${esc(s.reason)}</span>
                </div>
            `).join("");
            subSignalsList.innerHTML =
                (rows || `<div class="empty-sub-signals text-dim text-xs">No channel is voting on this reading.</div>`) +
                (quiet ? `<div class="drivers-quiet-title">Not voting (${silent.length})</div><div class="drivers-quiet">${quiet}</div>` : "");
        }
    }

    // 5. Single-Row Anchor KPI Badges
    const indicators = data.indicators || data;
    const anchorRsi = document.getElementById("anchor-rsi-val") || document.getElementById("rsi-val");
    const anchorVwap = document.getElementById("anchor-vwap-val") || document.getElementById("vwap-val");
    const anchorMacd = document.getElementById("anchor-macd-val");
    const anchorAtr = document.getElementById("anchor-atr-val") || document.getElementById("atr-val");

    if (anchorRsi) {
        anchorRsi.textContent = (indicators.rsi_14 !== undefined && indicators.rsi_14 !== null)
            ? Number(indicators.rsi_14).toFixed(1)
            : "--";
    }

    // Distance from today's VWAP, as a fraction. Null outside the live session:
    // the backend then computes VWAP from daily bars ($114 against a $161 price
    // on the 17:00 cycle of 2026-09-24), which is not today's average price.
    const vwapDist = vwapDistance(indicators, closePrice);
    if (anchorVwap) {
        anchorVwap.textContent = vwapDist === null
            ? (isLiveSession(indicators) ? "--" : "closed")
            : (vwapDist >= 0 ? "+" : "") + (vwapDist * 100).toFixed(2) + "%";
    }

    if (anchorMacd) {
        let macdHist = "--";
        if (indicators.macd_histogram !== undefined && indicators.macd_histogram !== null) {
            const v = Number(indicators.macd_histogram);
            macdHist = (v >= 0 ? "+" : "") + v.toFixed(2);
            anchorMacd.className = "font-mono " + (v > 0 ? "text-emerald" : (v < 0 ? "text-crimson" : ""));
        }
        anchorMacd.textContent = macdHist;
    }

    if (anchorAtr) {
        anchorAtr.textContent = (indicators.atr_14 !== undefined && indicators.atr_14 !== null)
            ? `$${Number(indicators.atr_14).toFixed(2)}`
            : (indicators.atr !== undefined ? `$${Number(indicators.atr).toFixed(2)}` : "--");
    }

    // 6. Categorized Technicals Matrix Updates
    const mEma = document.getElementById("matrix-ema");
    const mSma = document.getElementById("matrix-sma");
    const mRsi = document.getElementById("matrix-rsi");
    const mMacdLine = document.getElementById("matrix-macd-line");
    const mMacdHist = document.getElementById("matrix-macd-hist");
    const mIbs = document.getElementById("matrix-ibs");
    const mVwap = document.getElementById("matrix-vwap");
    const mVwapDist = document.getElementById("matrix-vwap-dist");
    const mBbWidth = document.getElementById("matrix-bb-width");
    const mAtr = document.getElementById("matrix-atr");

    if (mEma) {
        const e9 = indicators.ema_9 ? `$${Number(indicators.ema_9).toFixed(1)}` : "--";
        const e21 = indicators.ema_21 ? `$${Number(indicators.ema_21).toFixed(1)}` : "--";
        mEma.textContent = `${e9} / ${e21}`;
    }
    if (mSma) {
        const s20 = indicators.sma_20 ? `$${Number(indicators.sma_20).toFixed(1)}` : "--";
        const s50 = indicators.sma_50 ? `$${Number(indicators.sma_50).toFixed(1)}` : "--";
        mSma.textContent = `${s20} / ${s50}`;
    }
    if (mRsi) {
        mRsi.textContent = indicators.rsi_14 ? Number(indicators.rsi_14).toFixed(1) : "--";
    }
    if (mMacdLine) {
        const line = indicators.macd_line ? Number(indicators.macd_line).toFixed(2) : "--";
        const sig = indicators.macd_signal ? Number(indicators.macd_signal).toFixed(2) : "--";
        mMacdLine.textContent = `${line} / ${sig}`;
    }
    if (mMacdHist) {
        if (indicators.macd_histogram !== undefined && indicators.macd_histogram !== null) {
            const h = Number(indicators.macd_histogram);
            mMacdHist.textContent = (h >= 0 ? "+" : "") + h.toFixed(2);
            mMacdHist.className = "font-mono font-bold " + (h > 0 ? "text-emerald" : (h < 0 ? "text-crimson" : ""));
        } else {
            mMacdHist.textContent = "--";
        }
    }
    // IBS and VWAP describe today's session; outside it they are artefacts of
    // daily bars (IBS read 0.00 and VWAP $114 on the 17:00 cycle), so say so.
    const live = isLiveSession(indicators);
    const closedCell = (el) => { el.textContent = "closed"; el.className = "font-mono matrix-closed"; };
    if (mIbs) {
        if (!live) closedCell(mIbs);
        else {
            mIbs.className = "font-mono";
            mIbs.textContent = indicators.ibs !== undefined && indicators.ibs !== null
                ? Number(indicators.ibs).toFixed(2) : "--";
        }
    }
    if (mVwap) {
        if (!live) closedCell(mVwap);
        else {
            mVwap.className = "font-mono";
            mVwap.textContent = indicators.vwap ? `$${Number(indicators.vwap).toFixed(2)}` : "--";
        }
    }
    if (mVwapDist) {
        if (!live) closedCell(mVwapDist);
        else if (vwapDist !== null) {
            const vd = vwapDist * 100;
            mVwapDist.textContent = (vd >= 0 ? "+" : "") + vd.toFixed(2) + "%";
            mVwapDist.className = "font-mono " + (vd >= 0 ? "text-emerald" : "text-crimson");
        } else {
            mVwapDist.className = "font-mono";
            mVwapDist.textContent = "--";
        }
    }
    if (mBbWidth) {
        if (indicators.bollinger_width !== undefined && indicators.bollinger_width !== null) {
            mBbWidth.textContent = (Number(indicators.bollinger_width) * 100).toFixed(2) + "%";
        } else if (indicators.bollinger_upper && indicators.bollinger_lower && indicators.bollinger_middle) {
            mBbWidth.textContent = ((indicators.bollinger_upper - indicators.bollinger_lower) / indicators.bollinger_middle * 100).toFixed(2) + "%";
        } else {
            mBbWidth.textContent = "--";
        }
    }
    if (mAtr) {
        const atr = indicators.atr_14 || indicators.atr;
        mAtr.textContent = atr ? `$${Number(atr).toFixed(2)}` : "--";
    }
    const mRvol = document.getElementById("matrix-rvol");
    if (mRvol) {
        // Today's time-of-day reading when there is one (what the volume
        // channels read since v030); otherwise yesterday's, labelled as such.
        const rv = relativeVolume(indicators);
        mRvol.textContent = rv ? `${rv.value.toFixed(2)}x ${rv.label}` : "--";
    }

    // 7. Dynamic / Evolved Indicators Sub-Tray
    const dynamicWrapper = document.getElementById("dynamic-indicators-wrapper");
    const dynamicGrid = document.getElementById("dynamic-indicators-grid");
    if (dynamicGrid && typeof indicators === "object") {
        const standardKeys = new Set([
            "rsi_14", "bollinger_upper", "bollinger_middle", "bollinger_lower",
            "bollinger_width", "vwap", "vwap_dist", "adx_14", "adx", "atr_14", "atr",
            "ema_9", "ema_21", "sma_20", "sma_50", "macd_line", "macd_signal", "macd_histogram",
            "ibs", "supertrend", "relative_volume", "vwap_anchor", "day_change_pct", "gap_pct",
            "daily_change_pct", "open_positions", "portfolio_state", "level_ii", "recent_candles_count",
            "errors", "close", "timestamp", "ticker", "session_relative_volume"
        ]);
        // Bookkeeping that travels with an indicator (which day or anchor it
        // describes, how many sessions it averaged) is not an indicator itself.
        const isBookkeeping = (k) => /_(source|anchor|sessions)$/.test(k);

        const extraEntries = Object.entries(indicators).filter(
            ([k, v]) => !standardKeys.has(k) && !isBookkeeping(k) && v !== null && v !== undefined
                && typeof v !== "object",
        );
        if (extraEntries.length > 0) {
            if (dynamicWrapper) dynamicWrapper.style.display = "block";
            dynamicGrid.innerHTML = extraEntries.map(([k, v]) => {
                const label = k.replace(/_/g, " ").toUpperCase();
                const formatted = formatIndicatorVal(k, v);
                return `
                    <div class="dynamic-indicator-chip" title="${label}: ${formatted}">
                        <span>${label}</span>
                        <span>${formatted}</span>
                    </div>
                `;
            }).join("");
        } else {
            if (dynamicWrapper) dynamicWrapper.style.display = "none";
            dynamicGrid.innerHTML = "";
        }
    }
}

function formatIndicatorVal(key, val) {
    if (val === undefined || val === null) return "--";
    if (typeof val === "boolean") return val ? "TRUE" : "FALSE";
    const num = Number(val);
    if (isNaN(num)) return String(val);

    const low = key.toLowerCase();
    if (low === "rsi_14" || low === "adx_14" || low === "adx" || low === "ibs") {
        return num.toFixed(2);
    }
    if (low.includes("pct_norm") || low === "bollinger_width") {
        return (num * 100).toFixed(2) + "%";
    }
    if (low.includes("pct") || low.includes("percent") || low.includes("rate") || low.includes("dist")) {
        return (num >= 0 && low.includes("dist") ? "+" : "") + num.toFixed(2) + "%";
    }
    if (low.includes("price") || low.includes("vwap") || low.includes("band") || low.includes("upper") || low.includes("lower") || low.includes("atr") || low.includes("sma") || low.includes("ema")) {
        return "$" + num.toFixed(2);
    }
    if (low.includes("vol") || low.includes("count") || low.includes("shares")) {
        return Math.round(num).toLocaleString();
    }
    return (Math.abs(num) < 0.001 && Math.abs(num) > 0) ? num.toExponential(2) : num.toFixed(2);
}

function updateIndicatorBadges(indicators) {
    updateMarketTechnicalsAndSignals({ indicators });
}

async function resolveOrderProposal(id, decision) {
    try {
        appendLogLine(`[SYSTEM] Resolving order proposal #${id}: ${decision.toUpperCase()}...`);
        const res = decision === "approve" ? await api.approveOrder(id) : await api.rejectOrder(id);
        appendLogLine(`[SYSTEM] Response: ${res.message}`);
        approvalPanel.classList.add("hidden");
        loadDashboardData();
    } catch (err) {
        appendLogLine(`[ERROR] Failed to resolve order: ${err.message}`);
    }
}


// Kick off credentials check on loading content
window.addEventListener("DOMContentLoaded", checkAuth);

// Listen to URL hash changes for routing
window.addEventListener("hashchange", () => {
    const parsed = parseHash();
    if (parsed.tab) {
        const tabChanged = state.activeTab !== parsed.tab;
        const sessionChanged = parsed.sessionId && state.activeSessionId !== parsed.sessionId;
        
        if (parsed.sessionId) {
            state.activeSessionId = parsed.sessionId;
            localStorage.setItem("gd_active_session_id", parsed.sessionId);
        }
        
        if (tabChanged) {
            switchTab(parsed.tab, false);
        }
        
        if (sessionChanged || (tabChanged && parsed.tab === "explorer")) {
            if (parsed.sessionId) {
                selectCycle(parsed.sessionId);
            } else {
                refreshExplorerData();
            }
        }

        if (parsed.tab === "memory") {
            let memoryChanged = false;
            
            if (state.memorySubTab !== parsed.memorySubTab) {
                state.memorySubTab = parsed.memorySubTab;
                memoryChanged = true;
            }
            
            if (parsed.memorySubTab === "notes") {
                if (state.activeNoteFile !== parsed.memorySelection) {
                    state.activeNoteFile = parsed.memorySelection;
                    memoryChanged = true;
                }
            } else if (parsed.memorySubTab === "instructions") {
                const parsedAgentName = parsed.memorySelection;
                const currentAgentName = state.activeAgent ? state.activeAgent.name : null;
                if (currentAgentName !== parsedAgentName) {
                    state.activeAgent = parsedAgentName ? { name: parsedAgentName } : null;
                    memoryChanged = true;
                }
                if (state.activeInstructionVersion !== parsed.memoryExtra) {
                    state.activeInstructionVersion = parsed.memoryExtra;
                    memoryChanged = true;
                }
            } else if (parsed.memorySubTab === "algorithms") {
                if (state.selectedAlgorithmVersion !== parsed.memorySelection) {
                    state.selectedAlgorithmVersion = parsed.memorySelection;
                    memoryChanged = true;
                }
            } else if (parsed.memorySubTab === "reviews") {
                if (state.activeReviewId !== parsed.memorySelection) {
                    state.activeReviewId = parsed.memorySelection;
                    memoryChanged = true;
                }
            }
            
            if (memoryChanged || tabChanged) {
                if (window.loadMemoryTab) {
                    window.loadMemoryTab();
                }
            }
        }
    }
});

/* ═══════════════════════════════════════════════════════════════════════
   Remote OAuth Authentication (SSE-driven, no polling)
   ═══════════════════════════════════════════════════════════════════════ */
const oauthModal = document.getElementById("oauth-modal");
const oauthLink = document.getElementById("oauth-link");
const oauthCallbackUrl = document.getElementById("oauth-callback-url");
const oauthSubmitBtn = document.getElementById("oauth-submit-btn");
const oauthError = document.getElementById("oauth-error");

function showOAuthModal(url) {
    if (!oauthModal) return;
    oauthLink.href = url;
    oauthModal.style.display = "flex";
    oauthError.style.display = "none";
    oauthCallbackUrl.value = "";
}

// Closing only hides it: the sign-in stays open on the server, and the prompt
// comes back when the console reconnects or the app next needs Robinhood.
function hideOAuthModal() {
    if (oauthModal) oauthModal.style.display = "none";
}
document.getElementById("oauth-close-btn")?.addEventListener("click", hideOAuthModal);
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && oauthModal && oauthModal.style.display !== "none") hideOAuthModal();
});

if (oauthSubmitBtn) {
    oauthSubmitBtn.addEventListener("click", async () => {
        const url = oauthCallbackUrl.value.trim();
        if (!url) {
            oauthError.textContent = "Please enter the callback URL.";
            oauthError.style.display = "block";
            return;
        }
        oauthSubmitBtn.disabled = true;
        oauthSubmitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Submitting...';
        try {
            const res = await api.submitOAuthCallback(url);
            if (res && res.status === "ok") {
                oauthModal.style.display = "none";
                setTimeout(() => location.reload(), 2000);
            } else {
                oauthError.textContent = "Failed to submit. " + (res.detail || "");
                oauthError.style.display = "block";
            }
        } catch (e) {
            oauthError.textContent = "Error: " + e.message;
            oauthError.style.display = "block";
        } finally {
            oauthSubmitBtn.disabled = false;
            oauthSubmitBtn.innerHTML = "Submit";
        }
    });
}

// ── Cash Adjustment Modal ────────────────────────────────────────────

async function showCashAdjustmentModal() {
    const today = new Date().toISOString().split("T")[0];

    // Fetch existing adjustments to show in the modal
    let existing = [];
    try {
        const res = await api.fetchCashAdjustments();
        existing = res.adjustments || [];
    } catch { /* ignore */ }

    const overlay = document.createElement("div");
    overlay.className = "custom-modal-overlay";

    // Build existing adjustments list HTML
    let listHtml = "";
    if (existing.length > 0) {
        listHtml = `
            <div class="adj-list-section">
                <h4 class="adj-list-title">
                    <i class="fa-solid fa-clock-rotate-left"></i> Logged Adjustments
                </h4>
                <div class="adj-list">
                    ${existing.map(a => {
                        const isDeposit = a.amount > 0;
                        const icon = isDeposit ? "fa-arrow-down" : "fa-arrow-up";
                        const cls = isDeposit ? "deposit" : "withdrawal";
                        const label = isDeposit ? "Deposit" : "Withdrawal";
                        return `
                            <div class="adj-item ${cls}" data-adj-id="${a.id}">
                                <div class="adj-item-info">
                                    <span class="adj-item-icon"><i class="fa-solid ${icon}"></i></span>
                                    <span class="adj-item-date">${a.date}</span>
                                    <span class="adj-item-amount">${isDeposit ? '+' : ''}$${Math.abs(a.amount).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</span>
                                    <span class="adj-item-label">${label}</span>
                                    ${a.note ? `<span class="adj-item-note">${a.note}</span>` : ''}
                                </div>
                                <button class="btn-adj-delete" title="Delete this adjustment" data-adj-id="${a.id}">
                                    <i class="fa-solid fa-trash-can"></i>
                                </button>
                            </div>`;
                    }).join("")}
                </div>
            </div>`;
    }

    overlay.innerHTML = `
        <div class="custom-modal-card glass-card adj-modal">
            <div class="custom-modal-header">
                <h3>
                    <i class="fa-solid fa-money-bill-transfer text-amber"></i>
                    <span>Cash Adjustment</span>
                </h3>
                <button class="custom-modal-close-btn" aria-label="Close dialog">&times;</button>
            </div>
            <div class="custom-modal-body">
                <p class="adj-modal-subtitle">Log a deposit or withdrawal to calibrate the equity curve so it reflects actual trading performance.</p>

                <form id="adj-form" class="adj-form">
                    <div class="adj-form-row">
                        <label for="adj-date">Date</label>
                        <input type="date" id="adj-date" value="${today}" required />
                    </div>
                    <div class="adj-form-row">
                        <label for="adj-type">Type</label>
                        <div class="adj-type-toggle">
                            <button type="button" class="adj-type-btn active" data-type="deposit">
                                <i class="fa-solid fa-arrow-down"></i> Deposit
                            </button>
                            <button type="button" class="adj-type-btn" data-type="withdrawal">
                                <i class="fa-solid fa-arrow-up"></i> Withdrawal
                            </button>
                        </div>
                    </div>
                    <div class="adj-form-row">
                        <label for="adj-amount">Amount ($)</label>
                        <input type="number" id="adj-amount" min="0.01" step="0.01" placeholder="500.00" required />
                    </div>
                    <div class="adj-form-row">
                        <label for="adj-note">Note <span class="text-dim">(optional)</span></label>
                        <input type="text" id="adj-note" placeholder="e.g. Monthly deposit" maxlength="200" />
                    </div>
                </form>

                ${listHtml}
            </div>
            <div class="custom-modal-footer">
                <button class="btn btn-secondary custom-modal-cancel">Cancel</button>
                <button class="btn btn-primary custom-modal-confirm" id="adj-submit-btn">
                    <i class="fa-solid fa-check"></i> Save Adjustment
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
    overlay.offsetHeight; // trigger reflow
    overlay.classList.add("active");

    // State
    let adjType = "deposit"; // or "withdrawal"
    let resolved = false;

    const cleanup = () => {
        overlay.classList.remove("active");
        document.removeEventListener("keydown", handleKeyDown);
        setTimeout(() => overlay.remove(), 250);
    };

    const handleCancel = () => {
        if (resolved) return;
        resolved = true;
        cleanup();
    };

    const handleKeyDown = (e) => {
        if (e.key === "Escape") handleCancel();
    };

    // Type toggle
    overlay.querySelectorAll(".adj-type-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            overlay.querySelectorAll(".adj-type-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            adjType = btn.dataset.type;
        });
    });

    // Delete handlers
    overlay.querySelectorAll(".btn-adj-delete").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const id = parseInt(btn.dataset.adjId);
            const confirmed = await ui.showConfirm(
                "Delete Adjustment",
                "Are you sure you want to delete this cash adjustment? The equity curve will be recalculated.",
                { isWarning: true, confirmText: "Delete" }
            );
            if (!confirmed) return;
            try {
                await api.deleteCashAdjustment(id);
                // Remove from DOM
                const item = btn.closest(".adj-item");
                if (item) {
                    item.style.opacity = "0";
                    item.style.transform = "translateX(20px)";
                    setTimeout(() => item.remove(), 200);
                }
                // Refresh chart in background
                refreshPerfChart();
            } catch (err) {
                await ui.showAlert("Delete Failed", err.message, { isError: true });
            }
        });
    });

    // Submit
    overlay.querySelector(".custom-modal-confirm").addEventListener("click", async () => {
        const dateInput = overlay.querySelector("#adj-date");
        const amountInput = overlay.querySelector("#adj-amount");
        const noteInput = overlay.querySelector("#adj-note");
        const submitBtn = overlay.querySelector("#adj-submit-btn");

        const date = dateInput.value;
        let amount = parseFloat(amountInput.value);
        const note = noteInput.value.trim();

        if (!date || isNaN(amount) || amount <= 0) {
            amountInput.classList.add("input-error");
            setTimeout(() => amountInput.classList.remove("input-error"), 1500);
            return;
        }

        // Apply sign based on type
        if (adjType === "withdrawal") {
            amount = -amount;
        }

        submitBtn.disabled = true;
        submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

        try {
            await api.createCashAdjustment(date, amount, note);
            resolved = true;
            cleanup();
            refreshPerfChart();
        } catch (err) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i class="fa-solid fa-check"></i> Save Adjustment';
            await ui.showAlert("Save Failed", err.message, { isError: true });
        }
    });

    // Event bindings
    overlay.querySelector(".custom-modal-cancel").addEventListener("click", handleCancel);
    overlay.querySelector(".custom-modal-close-btn").addEventListener("click", handleCancel);
    overlay.querySelector(".custom-modal-card").addEventListener("click", (e) => e.stopPropagation());
    overlay.addEventListener("click", handleCancel);
    document.addEventListener("keydown", handleKeyDown);

    // Focus amount input
    setTimeout(() => overlay.querySelector("#adj-amount")?.focus(), 100);
}
