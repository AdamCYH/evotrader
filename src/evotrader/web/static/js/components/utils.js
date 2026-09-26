/**
 * Escapes HTML characters to prevent XSS issues when rendering logs/arguments
 */
export function escapeHtml(text) {
    if (!text) return "";
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

/**
 * Pretty-prints and syntax highlights JSON for display
 */
export function syntaxHighlightJson(json) {
    if (!json) return "No data";
    
    let jsonObj = json;
    if (typeof json === "string") {
        try {
            jsonObj = JSON.parse(json);
        } catch (e) {
            // Not valid JSON, just return escaped string
            return escapeHtml(json);
        }
    }
    
    let jsonStr = JSON.stringify(jsonObj, null, 2);
    
    // Replace literal '\n', '\r', and '\t' to format multi-line strings beautifully
    jsonStr = jsonStr.replace(/\\n/g, '\n').replace(/\\r/g, '\r').replace(/\\t/g, '\t');
    
    // Escape &, <, and > for XSS protection, but keep " so the regex can match JSON strings
    jsonStr = jsonStr.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    
    return jsonStr.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
        let cls = 'json-number';
        if (/^"/.test(match)) {
            if (/:$/.test(match)) {
                cls = 'json-key';
            } else {
                cls = 'json-string';
            }
        } else if (/true|false/.test(match)) {
            cls = 'json-boolean';
        } else if (/null/.test(match)) {
            cls = 'json-null';
        }
        return '<span class="' + cls + '">' + match + '</span>';
    });
}

/**
 * Clean formatting for monetary values
 */
export function formatCurrency(amount) {
    const value = parseFloat(amount || 0);
    return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * Format timestamp cleanly (e.g. HH:MM:SS or short local date-time)
 */
export function formatTime(isoString, includeDate = false) {
    if (!isoString) return "--:--:--";
    try {
        const date = new Date(isoString);
        if (isNaN(date.getTime())) return "--:--:--";
        if (includeDate) {
            return date.toLocaleString("en-US", {
                timeZone: "America/Los_Angeles",
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            });
        }
        return date.toLocaleTimeString("en-US", {
            timeZone: "America/Los_Angeles",
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
        });
    } catch (e) {
        return "--:--:--";
    }
}

/**
/**
 * Render rich Markdown with GFM tables, headers, lists, code blocks, and trading keyword badges.
 * Uses window.marked if loaded, with robust internal fallback.
 */
export function renderMarkdown(text) {
    if (!text) return "";

    let html = "";
    if (window.marked && typeof window.marked.parse === "function") {
        try {
            window.marked.setOptions({
                gfm: true,
                breaks: true
            });
            html = window.marked.parse(text);
        } catch (e) {
            console.error("Marked parsing error, falling back:", e);
            html = parseBasicMarkdown(text);
        }
    } else {
        html = parseBasicMarkdown(text);
    }

    // Auto-highlight key trading actions and states outside HTML tags
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
        const regex = new RegExp(`(?<!<[^>]*)\\b(${kw.word})\\b(?![^<]*>)`, "g");
        html = html.replace(regex, `<span class="thought-kw ${kw.class}">$1</span>`);
    });

    return html;
}

export function parseBasicMarkdown(text) {
    if (!text) return "";
    let escaped = escapeHtml(text);
    escaped = escaped.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
    escaped = escaped.replace(/`(.*?)`/g, '<code class="inline-code">$1</code>');
    return escaped.replace(/\n/g, "<br>");
}

/**
 * Custom confirmation popup (replaces window.confirm)
 * Returns a Promise resolving to true (Confirm) or false (Cancel/Close)
 */
export function showConfirm(title, message, options = {}) {
    const {
        confirmText = "Confirm",
        cancelText = "Cancel",
        isWarning = false
    } = options;

    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "custom-modal-overlay";
        
        const iconClass = isWarning 
            ? "fa-solid fa-circle-exclamation text-crimson" 
            : "fa-solid fa-circle-question text-amber";

        overlay.innerHTML = `
            <div class="custom-modal-card glass-card ${isWarning ? 'warning' : ''}">
                <div class="custom-modal-header">
                    <h3>
                        <i class="${iconClass}"></i>
                        <span>${escapeHtml(title)}</span>
                    </h3>
                    <button class="custom-modal-close-btn" aria-label="Close dialog">&times;</button>
                </div>
                <div class="custom-modal-body">
                    <p>${escapeHtml(message)}</p>
                </div>
                <div class="custom-modal-footer">
                    <button class="btn btn-secondary custom-modal-cancel">${escapeHtml(cancelText)}</button>
                    <button class="btn btn-primary custom-modal-confirm">${escapeHtml(confirmText)}</button>
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

        const handleConfirm = () => {
            if (resolved) return;
            resolved = true;
            resolve(true);
            cleanup();
        };

        const handleCancel = () => {
            if (resolved) return;
            resolved = true;
            resolve(false);
            cleanup();
        };

        const focusableElements = overlay.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        const firstFocusable = focusableElements[0];
        const lastFocusable = focusableElements[focusableElements.length - 1];

        const handleKeyDown = (e) => {
            if (e.key === "Escape") {
                handleCancel();
            } else if (e.key === "Enter") {
                e.preventDefault();
                handleConfirm();
            } else if (e.key === "Tab") {
                if (e.shiftKey) { // Shift + Tab
                    if (document.activeElement === firstFocusable) {
                        lastFocusable.focus();
                        e.preventDefault();
                    }
                } else { // Tab
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

        // Set default focus to the confirm button
        overlay.querySelector(".custom-modal-confirm").focus();
    });
}

/**
 * Custom alert popup (replaces window.alert)
 * Returns a Promise resolving when closed
 */
export function showAlert(title, message, options = {}) {
    const {
        okText = "OK",
        isError = false
    } = options;

    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "custom-modal-overlay";
        
        const iconClass = isError 
            ? "fa-solid fa-circle-xmark text-crimson" 
            : "fa-solid fa-circle-info text-cyan";

        overlay.innerHTML = `
            <div class="custom-modal-card glass-card ${isError ? 'error' : ''}">
                <div class="custom-modal-header">
                    <h3>
                        <i class="${iconClass}"></i>
                        <span>${escapeHtml(title)}</span>
                    </h3>
                    <button class="custom-modal-close-btn" aria-label="Close dialog">&times;</button>
                </div>
                <div class="custom-modal-body">
                    <p>${escapeHtml(message)}</p>
                </div>
                <div class="custom-modal-footer">
                    <button class="btn btn-primary custom-modal-confirm">${escapeHtml(okText)}</button>
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

        const handleClose = () => {
            if (resolved) return;
            resolved = true;
            resolve();
            cleanup();
        };

        const focusableElements = overlay.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        const firstFocusable = focusableElements[0];
        const lastFocusable = focusableElements[focusableElements.length - 1];

        const handleKeyDown = (e) => {
            if (e.key === "Escape" || e.key === "Enter") {
                e.preventDefault();
                handleClose();
            } else if (e.key === "Tab") {
                if (e.shiftKey) { // Shift + Tab
                    if (document.activeElement === firstFocusable) {
                        lastFocusable.focus();
                        e.preventDefault();
                    }
                } else { // Tab
                    if (document.activeElement === lastFocusable) {
                        firstFocusable.focus();
                        e.preventDefault();
                    }
                }
            }
        };

        overlay.querySelector(".custom-modal-confirm").addEventListener("click", handleClose);
        overlay.querySelector(".custom-modal-close-btn").addEventListener("click", handleClose);
        
        overlay.querySelector(".custom-modal-card").addEventListener("click", (e) => {
            e.stopPropagation();
        });

        overlay.addEventListener("click", handleClose);
        document.addEventListener("keydown", handleKeyDown);

        // Set default focus to the OK button
        overlay.querySelector(".custom-modal-confirm").focus();
    });
}

/**
 * Renders unified diff output as color-coded lines
 */
export function renderDiff(diffText) {
    if (!diffText) return `<div class="text-dim text-center py-4">No changes detected.</div>`;
    const lines = diffText.split("\n");
    let html = '<div class="diff-container">';
    lines.forEach(line => {
        let className = "diff-line";
        if (line.startsWith("+") && !line.startsWith("+++")) {
            className += " diff-line-added";
        } else if (line.startsWith("-") && !line.startsWith("---")) {
            className += " diff-line-removed";
        } else if (line.startsWith("@@")) {
            className += " diff-line-header";
        } else if (line.startsWith("---") || line.startsWith("+++")) {
            className += " diff-line-file";
        }
        // Escape HTML characters
        const escaped = escapeHtml(line);
        html += `<div class="${className}">${escaped}</div>`;
    });
    html += '</div>';
    return html;
}

/**
 * Truncate or format version strings beautifully.
 * Handles the pattern "YYYYMMDD_HHMMSS_description" to render as "YYYY-MM-DD (description)"
 */
export function formatVersion(version, maxLen = 24) {
    if (!version) return "";
    const match = String(version).match(/^(\d{8})_(\d{6})_(.+)$/);
    if (match) {
        const date = match[1];
        const desc = match[3];
        const formattedDate = `${date.substring(0, 4)}-${date.substring(4, 6)}-${date.substring(6, 8)}`;
        const remainingSpace = maxLen - formattedDate.length - 4; // space for " (...)"
        const truncatedDesc = desc.length > remainingSpace 
            ? desc.substring(0, Math.max(5, remainingSpace)) + "..."
            : desc;
        return `${formattedDate} (${truncatedDesc})`;
    }
    if (version.length > maxLen) {
        return version.substring(0, maxLen - 3) + "...";
    }
    return version;
}

/**
 * The instrument the system is configured to trade, as reported by
 * GET /api/portfolio -> local.ticker.
 *
 * Display code used to fall back to a literal ("SPY" beside a pending order,
 * "QQQ" on the technicals badge). That is worse than showing nothing: a real
 * MSTR order labelled SPY reads as an order in an instrument you do not trade.
 * Set once per poll, read by any component that needs a fallback.
 */
let displayTicker = null;

export function setDisplayTicker(ticker) {
    if (ticker) displayTicker = ticker;
}

/** Configured ticker, or `placeholder` when the first poll has not landed. */
export function getDisplayTicker(placeholder = "\u2014") {
    return displayTicker || placeholder;
}
