/* ═══════════════════════════════════════════════════════════════════════
   EvoTrader — API Service Module (ES6 Module)
   ═══════════════════════════════════════════════════════════════════════ */

let authToken = localStorage.getItem("gd_token");

export function getAuthToken() {
    return authToken;
}

export function setAuthToken(token) {
    authToken = token;
    if (token) {
        localStorage.setItem("gd_token", token);
    } else {
        localStorage.removeItem("gd_token");
    }
}

export function clearAuthToken() {
    setAuthToken(null);
}

/**
 * Base fetch wrapper injecting credentials and handling API responses
 */
async function apiFetch(url, options = {}) {
    const headers = options.headers || {};
    const token = getAuthToken();
    if (token) {
        headers["Authorization"] = `Bearer ${token}`;
    }
    headers["Content-Type"] = "application/json";

    const response = await fetch(url, { ...options, headers });
    
    if (response.status === 401) {
        clearAuthToken();
        location.reload();
        throw new Error("Unauthorized");
    }
    
    if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP Error ${response.status}`);
    }
    
    return response.json();
}

/**
 * Verify access password
 */
export async function verifyPassword(password) {
    const data = await fetch("/api/auth/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password })
    });
    
    if (!data.ok) {
        throw new Error("Invalid password");
    }
    
    const result = await data.json();
    if (result.token) {
        setAuthToken(result.token);
    }
    return result;
}

/**
 * Fetch portfolio metrics (broker + local stats)
 */
export async function fetchPortfolio(period = "today") {
    return apiFetch(`/api/portfolio?period=${encodeURIComponent(period)}`);
}

/**
 * Fetch trade journal entries
 */
export async function fetchTrades(limit = 50, period = "all") {
    return apiFetch(`/api/trades?limit=${limit}&period=${encodeURIComponent(period)}`);
}

/**
 * Fetch self-evolution logs
 */
export async function fetchEvolution() {
    return apiFetch("/api/evolution");
}

/**
 * Fetch agent thoughts (optionally filter by session_id)
 */
export async function fetchThoughts(sessionId = null) {
    let url = "/api/thoughts";
    if (sessionId) {
        url += `?session_id=${encodeURIComponent(sessionId)}`;
    }
    return apiFetch(url);
}

/**
 * One instrument's recent snapshots for the market panel and its chart.
 * Without a ticker, the server answers with the configured primary's; the
 * answer names the instrument it is for and lists the ones to choose from.
 */
export async function fetchTechChart(limit = 500, period = "all", ticker = null) {
    const pick = ticker ? `&ticker=${encodeURIComponent(ticker)}` : "";
    return apiFetch(`/api/chart/tech?limit=${limit}&period=${encodeURIComponent(period)}${pick}`);
}

/**
 * The combined signal per trading day for the period (average, range, count),
 * for the account chart's signal overlay.
 */
export async function fetchSignalDaily(period = "all") {
    return apiFetch(`/api/chart/signal-daily?period=${encodeURIComponent(period)}`);
}

/**
 * Fetch unique past sessions (cycles)
 */
export async function fetchCycles() {
    return apiFetch("/api/cycles");
}

/**
 * Fetch detail metadata for a single cycle session
 */
export async function fetchCycleDetail(sessionId) {
    return apiFetch(`/api/cycles/${encodeURIComponent(sessionId)}`);
}

/**
 * Submit manual OAuth callback URL
 */
export async function submitOAuthCallback(url) {
    return apiFetch("/api/auth/oauth_callback", {
        method: "POST",
        body: JSON.stringify({ url })
    });
}

/**
 * Manually trigger a trading cycle
 */
export async function triggerCycle() {
    return apiFetch("/api/trigger", { method: "POST" });
}

/**
 * Clear cached OAuth tokens and trigger re-authentication
 */
export async function refreshOAuth() {
    return apiFetch("/api/oauth/refresh", { method: "POST" });
}

/**
 * Manually trigger position alignment sync
 */
export async function reconcilePositions() {
    return apiFetch("/api/reconcile", { method: "POST" });
}

/**
 * Approve a pending trade proposal
 */
export async function approveOrder(orderId) {
    return apiFetch(`/api/trades/approve/${orderId}`, { method: "POST" });
}

/**
 * Reject a pending trade proposal
 */
export async function rejectOrder(orderId) {
    return apiFetch(`/api/trades/reject/${orderId}`, { method: "POST" });
}

/**
 * Fetch LLM logs for a session
 */
export async function fetchLlmLogs(sessionId, limit = 500) {
    let url = `/api/telemetry/llm?limit=${limit}`;
    if (sessionId) url += `&session_id=${encodeURIComponent(sessionId)}`;
    return apiFetch(url);
}

/**
 * Fetch ChromaDB memory collection statistics
 */
export async function fetchMemoryStats() {
    return apiFetch("/api/memory/stats");
}

/**
 * Fetch all stored semantic notes from ChromaDB
 */
export async function fetchMemoryNotes() {
    return apiFetch("/api/memory/notes");
}

export function apiDeleteNote(noteId) {
    return apiFetch(`/api/memory/notes/${encodeURIComponent(noteId)}`, { method: "DELETE" });
}

/**
 * Fetch all stored trade experiences from ChromaDB
 */
export async function fetchMemoryTrades() {
    return apiFetch("/api/memory/trades");
}

/**
 * Trigger vector memory pruning for entries older than 90 days
 */
export async function pruneMemory() {
    return apiFetch("/api/memory/prune", { method: "POST" });
}

/**
 * Wipe ChromaDB vector memory completely and reload notes from disk
 */
export async function clearMemory() {
    return apiFetch("/api/memory/clear", { method: "POST" });
}

/**
 * List all notes files currently saved on disk
 */
export async function fetchNoteFiles() {
    return apiFetch("/api/notes/files");
}

/**
 * Fetch contents of a specific notes file
 */
export async function fetchNoteFile(filename, source) {
    const params = source ? `?source=${encodeURIComponent(source)}` : "";
    return apiFetch(`/api/notes/files/${encodeURIComponent(filename)}${params}`);
}

/**
 * Create or save a notes file on disk and re-embed its contents
 */
export async function saveNoteFile(filename, payload) {
    return apiFetch(`/api/notes/files/${encodeURIComponent(filename)}`, {
        method: "POST",
        body: JSON.stringify(payload)
    });
}

/**
 * Delete a notes file from disk and remove its embeddings from ChromaDB
 */
export async function deleteNoteFile(filename, source) {
    const params = source ? `?source=${encodeURIComponent(source)}` : "";
    return apiFetch(`/api/notes/files/${encodeURIComponent(filename)}${params}`, {
        method: "DELETE"
    });
}

/**
 * Fetch list of all system instructions and available versions
 */
export async function fetchInstructions() {
    return apiFetch("/api/instructions");
}

/**
 * Fetch a specific agent's instruction version content
 */
export async function fetchInstructionVersion(agentName, version) {
    return apiFetch(`/api/instructions/${encodeURIComponent(agentName)}/${encodeURIComponent(version)}`);
}

/**
 * Manually trigger a self-evolution cycle
 * @param {string} [userComment] - Optional question or comment for the evolution agent
 */
export async function triggerEvolution(userComment = "") {
    const options = { method: "POST" };
    if (userComment) {
        options.body = JSON.stringify({ user_comment: userComment });
        console.log("[triggerEvolution] Sending body:", options.body);
    } else {
        console.log("[triggerEvolution] No user comment provided.");
    }
    return apiFetch("/api/evolution/trigger", options);
}

/**
 * Fetch the status of the evolution service
 */
export async function fetchEvolutionStatus() {
    return apiFetch("/api/evolution/status");
}

/**
 * Activate a specific instruction version for an agent
 */
export async function activateInstructionVersion(agentName, version) {
    return apiFetch(`/api/instructions/${encodeURIComponent(agentName)}/${encodeURIComponent(version)}/activate`, {
        method: "POST"
    });
}

export async function rejectInstructionVersion(agentName, version) {
    return apiFetch(`/api/instructions/${encodeURIComponent(agentName)}/${encodeURIComponent(version)}/reject`, {
        method: "POST"
    });
}

/**
 * Fetch all recorded self-evolution runs
 */
export async function fetchEvolutionRuns() {
    return apiFetch("/api/evolution/runs");
}

/**
 * Delete and revert a cycle and its side effects
 */
export async function deleteCycle(sessionId) {
    return apiFetch(`/api/cycles/${encodeURIComponent(sessionId)}`, {
        method: "DELETE"
    });
}

/**
 * Continue a previously timed-out or failed evolution cycle
 */
export async function continueEvolution(sessionId) {
    return apiFetch(`/api/evolution/continue/${encodeURIComponent(sessionId)}`, {
        method: "POST"
    });
}

/**
 * Fetch all available algorithm versions and active version
 */
export async function fetchAlgorithms() {
    return apiFetch("/api/algorithms");
}

/**
 * Fetch detailed parameter configuration, metadata, and diff for an algorithm version
 */
export async function fetchAlgorithmVersion(version) {
    return apiFetch(`/api/algorithms/${encodeURIComponent(version)}`);
}

/**
 * Activate a specific version of algorithm parameters
 */
export async function activateAlgorithmVersion(version) {
    return apiFetch(`/api/algorithms/${encodeURIComponent(version)}/activate`, {
        method: "POST"
    });
}

/**
 * Reject a specific version of algorithm parameters
 */
export async function rejectAlgorithmVersion(version) {
    return apiFetch(`/api/algorithms/${encodeURIComponent(version)}/reject`, {
        method: "POST"
    });
}

/**
 * Fetch status of the automated cron scheduler
 */
export async function fetchCronStatus() {
    return apiFetch("/api/cron/status");
}

export async function toggleCron(enabled, type = "cycle") {
    return apiFetch("/api/cron/toggle", {
        method: "POST",
        body: JSON.stringify({ enabled, type })
    });
}

/**
 * Cancel the active trading cycle task
 */
export async function cancelActiveCycle() {
    return apiFetch("/api/cycles/cancel", { method: "POST" });
}

/**
 * Cancel the active evolution cycle task
 */
export async function cancelActiveEvolution() {
    return apiFetch("/api/evolution/cancel", { method: "POST" });
}

/**
 * Fetch all available infrastructure code reviews
 */
export async function fetchReviews() {
    return apiFetch("/api/reviews");
}

/**
 * Fetch detailed content for a specific code review
 */
export async function fetchReview(reviewId) {
    return apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}`);
}

/**
 * Mark a code review as applied
 */
export async function applyReview(reviewId) {
    return apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}/apply`, {
        method: "POST"
    });
}

export async function rejectReview(reviewId) {
    return apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}/reject`, {
        method: "POST"
    });
}

/**
 * Fetch token and LLM usage analytics metrics
 */
export async function fetchLlmMetrics(period = "all") {
    return apiFetch(`/api/metrics/llm?period=${encodeURIComponent(period)}`);
}

/**
 * Fetch portfolio metrics (historical daily values for equity curve)
 */
export async function fetchPortfolioMetrics(period = "all") {
    return apiFetch(`/api/metrics/portfolio?period=${encodeURIComponent(period)}`);
}

/**
 * Fetch all cash adjustments (deposits / withdrawals)
 */
export async function fetchCashAdjustments() {
    return apiFetch("/api/adjustments");
}

/**
 * Create a new cash adjustment (deposit or withdrawal)
 * @param {string} date - YYYY-MM-DD
 * @param {number} amount - positive = deposit, negative = withdrawal
 * @param {string} note - optional description
 */
export async function createCashAdjustment(date, amount, note = "") {
    return apiFetch("/api/adjustments", {
        method: "POST",
        body: JSON.stringify({ date, amount, note }),
    });
}

/**
 * Delete a cash adjustment by ID
 * @param {number} id
 */
export async function deleteCashAdjustment(id) {
    return apiFetch(`/api/adjustments/${id}`, { method: "DELETE" });
}
