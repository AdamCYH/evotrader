import * as api from "./api.js";
import * as ui from "./components.js";

export function initMemoryController(state, appendLogLine, refreshDashboardFn) {
    // ── DOM Elements ──
    const memNavNotes = document.getElementById("mem-nav-notes");
    const memNavInstructions = document.getElementById("mem-nav-instructions");
    const memNavAlgorithms = document.getElementById("mem-nav-algorithms");
    const memNavReviews = document.getElementById("mem-nav-reviews");
    const memNavVector = document.getElementById("mem-nav-vector");
    const panelNotes = document.getElementById("panel-notes");
    const panelInstructions = document.getElementById("panel-instructions");
    const panelAlgorithms = document.getElementById("panel-algorithms");
    const panelReviews = document.getElementById("panel-reviews");
    const panelVector = document.getElementById("panel-vector");

    // Notes Panel Elements
    const notesBtnNew = document.getElementById("notes-btn-new");
    const notesListContainer = document.getElementById("notes-list-container");
    const editorHeader = document.getElementById("editor-header");
    const editorSettingsRow = document.getElementById("editor-settings-row");
    const editorBodySplit = document.getElementById("editor-body-split");
    const editorEmptyState = document.getElementById("editor-empty-state");

    const editorNoteTitle = document.getElementById("editor-note-title");
    const editorNoteTarget = document.getElementById("editor-note-target");
    const editorNoteCategory = document.getElementById("editor-note-category");
    const editorNotePriority = document.getElementById("editor-note-priority");
    const editorNoteBody = document.getElementById("editor-note-body");
    const editorNotePreview = document.getElementById("editor-note-preview");

    const editorBtnSave = document.getElementById("editor-btn-save");
    const editorBtnDelete = document.getElementById("editor-btn-delete");
    const editorNoteSourceBadge = document.getElementById("editor-note-source-badge");

    // Instructions Panel Elements
    const instructionsListContainer = document.getElementById("instructions-list-container");
    const instructionHeader = document.getElementById("instruction-header");
    const instructionAgentName = document.getElementById("instruction-agent-name");
    const instructionActiveBadge = document.getElementById("instruction-active-badge");
    const instructionVersionSelect = document.getElementById("instruction-version-select");
    const instructionBodySplit = document.getElementById("instruction-body-split");
    const instructionContentPreview = document.getElementById("instruction-content-preview");
    const instructionEmptyState = document.getElementById("instruction-empty-state");

    const btnActivateInstruction = document.getElementById("btn-activate-instruction");
    const btnRejectInstruction = document.getElementById("btn-reject-instruction");
    const instructionTabsContainer = document.getElementById("instruction-tabs-container");
    const btnInstructionTabSummary = document.getElementById("btn-instruction-tab-summary");
    const btnInstructionTabFull = document.getElementById("btn-instruction-tab-full");
    const btnInstructionTabDiff = document.getElementById("btn-instruction-tab-diff");
    const instructionSummaryPreview = document.getElementById("instruction-summary-preview");
    const instructionDiffPreview = document.getElementById("instruction-diff-preview");
    const instructionActionsSeparator = document.getElementById("instruction-actions-separator");

    // Algorithms Explorer Elements
    const algorithmsListContainer = document.getElementById("algorithms-list-container");
    const algorithmHeader = document.getElementById("algorithm-header");
    const algorithmVersionName = document.getElementById("algorithm-version-name");
    const algorithmActiveBadge = document.getElementById("algorithm-active-badge");
    const btnActivateAlgorithm = document.getElementById("btn-activate-algorithm");
    const btnRejectAlgorithm = document.getElementById("btn-reject-algorithm");
    const algorithmActionsSeparator = document.getElementById("algorithm-actions-separator");
    const algorithmTabsContainer = document.getElementById("algorithm-tabs-container");
    const btnAlgorithmTabSummary = document.getElementById("btn-algorithm-tab-summary");
    const btnAlgorithmTabFull = document.getElementById("btn-algorithm-tab-full");
    const btnAlgorithmTabDiff = document.getElementById("btn-algorithm-tab-diff");
    const algorithmBodySplit = document.getElementById("algorithm-body-split");
    const algorithmSummaryPreview = document.getElementById("algorithm-summary-preview");
    const algorithmContentPreview = document.getElementById("algorithm-content-preview");
    const algorithmDiffPreview = document.getElementById("algorithm-diff-preview");
    const algorithmEmptyState = document.getElementById("algorithm-empty-state");

    // Code Reviews Elements
    const reviewsListContainer = document.getElementById("reviews-list-container");
    const reviewHeader = document.getElementById("review-header");
    const reviewName = document.getElementById("review-name");
    const reviewActiveBadge = document.getElementById("review-active-badge");
    const reviewPendingBadge = document.getElementById("review-pending-badge");
    const reviewRejectedBadge = document.getElementById("review-rejected-badge");
    const btnApplyReview = document.getElementById("btn-apply-review");
    const btnRejectReview = document.getElementById("btn-reject-review");
    const reviewBodySplit = document.getElementById("review-body-split");
    const reviewContentPreview = document.getElementById("review-content-preview");
    const reviewEmptyState = document.getElementById("review-empty-state");
    const reviewCreatedTime = document.getElementById("review-created-time");

    // Vector Explorer Elements
    const vstatTrades = document.getElementById("vstat-trades");
    const vstatNotes = document.getElementById("vstat-notes");
    const vstatPatterns = document.getElementById("vstat-patterns");
    const vecBtnPrune = document.getElementById("vec-btn-prune");
    const vecBtnClear = document.getElementById("vec-btn-clear");
    const vecNotesTableBody = document.getElementById("vec-notes-table-body");
    const vecTradesTableBody = document.getElementById("vec-trades-table-body");

    // Detail Drawer Elements (repurposed for memory inspection)
    const detailDrawer = document.getElementById("detail-drawer");
    const detailDrawerBackdrop = document.getElementById("detail-drawer-backdrop");
    const tabArgs = document.getElementById("drawer-tab-args");
    const tabResp = document.getElementById("drawer-tab-resp");
    const panelArgs = document.getElementById("drawer-panel-args");
    const panelResp = document.getElementById("drawer-panel-resp");
    const detailLabel1 = document.getElementById("detail-label-1");
    const detailLabel2 = document.getElementById("detail-label-2");
    const detailLabel3 = document.getElementById("detail-label-3");
    const detailLabel4 = document.getElementById("detail-label-4");
    const detailAgentBadge = document.getElementById("detail-agent-badge");
    const detailToolName = document.getElementById("detail-tool-name");
    const detailToolSource = document.getElementById("detail-tool-source");
    const detailTimestamp = document.getElementById("detail-timestamp");
    const detailMcpAddressContainer = document.getElementById("detail-mcp-address-container");
    const detailArgsPre = document.getElementById("detail-args-pre");
    const detailRespPre = document.getElementById("detail-resp-pre");

    function setDrawerMetaLabels(l1 = "Agent", l2 = "Tool", l3 = "Type", l4 = "Timestamp") {
        if (detailLabel1) detailLabel1.textContent = l1;
        if (detailLabel2) detailLabel2.textContent = l2;
        if (detailLabel3) detailLabel3.textContent = l3;
        if (detailLabel4) detailLabel4.textContent = l4;
    }

    // ── Navigation Bindings ──
    if (memNavNotes) memNavNotes.addEventListener("click", () => switchMemorySubTab("notes"));
    if (memNavInstructions) memNavInstructions.addEventListener("click", () => switchMemorySubTab("instructions"));
    if (memNavAlgorithms) memNavAlgorithms.addEventListener("click", () => switchMemorySubTab("algorithms"));
    if (memNavReviews) memNavReviews.addEventListener("click", () => switchMemorySubTab("reviews"));
    if (memNavVector) memNavVector.addEventListener("click", () => switchMemorySubTab("vector"));

    function updateMemoryHash() {
        if (state.activeTab !== "memory") return;
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
        
        const expectedHash = `#${memoryHash}`;
        if (location.hash !== expectedHash) {
            location.hash = memoryHash;
        }
    }

    function switchMemorySubTab(subTab) {
        state.memorySubTab = subTab;
        updateMemoryHash();
        
        if (memNavNotes) memNavNotes.classList.toggle("active", subTab === "notes");
        if (memNavInstructions) memNavInstructions.classList.toggle("active", subTab === "instructions");
        if (memNavAlgorithms) memNavAlgorithms.classList.toggle("active", subTab === "algorithms");
        if (memNavReviews) memNavReviews.classList.toggle("active", subTab === "reviews");
        if (memNavVector) memNavVector.classList.toggle("active", subTab === "vector");
        
        if (panelNotes) panelNotes.classList.toggle("active", subTab === "notes");
        if (panelInstructions) panelInstructions.classList.toggle("active", subTab === "instructions");
        if (panelAlgorithms) panelAlgorithms.classList.toggle("active", subTab === "algorithms");
        if (panelReviews) panelReviews.classList.toggle("active", subTab === "reviews");
        if (panelVector) panelVector.classList.toggle("active", subTab === "vector");

        if (subTab === "notes") {
            loadNoteFilesList();
        } else if (subTab === "instructions") {
            loadInstructionsList();
        } else if (subTab === "algorithms") {
            loadAlgorithmsList();
        } else if (subTab === "reviews") {
            loadReviewsList();
        } else if (subTab === "vector") {
            loadVectorExplorerData();
        }
    }

    // ── Expose loadMemoryTab publicly ──
    window.loadMemoryTab = function() {
        switchMemorySubTab(state.memorySubTab || "vector");
    };

    window.showInstructionProposal = async function(agentName, proposedVersion) {
        switchMemorySubTab("instructions");
        await loadInstructionsList();
        const agentObj = state.instructionsAgents.find(a => a.name === agentName);
        if (agentObj) {
            await selectInstructionsAgentVersion(agentObj, proposedVersion);
        }
    };

    window.showAlgorithmProposal = async function(versionName) {
        // Route to the Algorithms panel and select the matching version
        switchMemorySubTab("algorithms");
        await loadAlgorithmsList();
        const algo = state.algorithms.find(a => a.version === versionName);
        if (algo) {
            await selectAlgorithm(algo);
        }
    };

    window.showReviewProposal = async function(reviewId) {
        switchMemorySubTab("reviews");
        await loadReviewsList();
        state.activeReviewId = reviewId;
        await selectReview(reviewId);
    };

    // ── NOTES CRUD MANAGEMENT ──

    async function loadNoteFilesList() {
        try {
            const files = await api.fetchNoteFiles();
            state.notesFiles = files;
            ui.renderNotesFileList(files, state.activeNoteFile, notesListContainer, selectNoteFile);
            
            if (state.activeNoteFile) {
                try {
                    const fileData = await api.fetchNoteFile(state.activeNoteFile, state.activeNoteSource);
                    showNoteEditor(fileData);
                } catch (err) {
                    appendLogLine(`[ERROR] Failed to load note content for ${state.activeNoteFile}: ${err.message}`);
                }
            } else if (!state.isEditingNewNote) {
                clearNoteEditor();
            }
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load note files list: ${err.message}`);
        }
    }

    async function selectNoteFile(filename, source) {
        state.activeNoteFile = filename;
        state.activeNoteSource = source || "user";
        state.isEditingNewNote = false;
        
        updateMemoryHash();
        
        // Refresh sidebar selection highlight
        ui.renderNotesFileList(state.notesFiles, filename, notesListContainer, selectNoteFile);
        
        try {
            const fileData = await api.fetchNoteFile(filename, state.activeNoteSource);
            showNoteEditor(fileData);
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load note content for ${filename}: ${err.message}`);
        }
    }

    function showNoteEditor(fileData) {
        // Unhide editor elements
        editorEmptyState.classList.add("hidden");
        editorHeader.classList.remove("hidden");
        editorSettingsRow.classList.remove("hidden");
        editorBodySplit.classList.remove("hidden");
        
        const isEvolution = (fileData.source === "evolution");
        const isHandoff = (fileData.source === "trading");
        state.activeNoteSource = fileData.source || "user";
        
        // Populate inputs
        editorNoteTitle.value = fileData.title || (fileData.filename ? fileData.filename.replace(/\.md$/, "") : "");
        editorNoteTitle.disabled = !state.isEditingNewNote; // Title only editable for new notes
        
        if (editorNoteTarget) {
            editorNoteTarget.value = state.activeNoteSource;
            editorNoteTarget.disabled = !state.isEditingNewNote;
        }

        // Populate category options based on note target audience
        if (editorNoteCategory) {
            if (isHandoff) {
                editorNoteCategory.innerHTML = `
                    <option value="trading_handoff">Cycle Handoff</option>
                `;
                editorNoteCategory.value = "trading_handoff";
            } else if (isEvolution) {
                editorNoteCategory.innerHTML = `
                    <option value="carry_forward">Carry-Forward / Backlog</option>
                    <option value="evolution">Evolution Note</option>
                    <option value="instruction">Instruction</option>
                    <option value="general">General</option>
                `;
                editorNoteCategory.value = (fileData.category === "evolution" || !fileData.category) ? "carry_forward" : fileData.category;
            } else {
                editorNoteCategory.innerHTML = `
                    <option value="instruction">Instruction (Rules)</option>
                    <option value="market_insight">Market Insight</option>
                    <option value="strategy_hint">Strategy Hint</option>
                    <option value="observation">Observation</option>
                    <option value="general">General</option>
                `;
                editorNoteCategory.value = fileData.category || "general";
            }
        }
        
        editorNotePriority.value = fileData.priority || "normal";
        
        // Raw content (no frontmatter) for direct editing. The handoff carries a
        // stamped header the agent wrote — show it as-is rather than parsing it away.
        if (isEvolution || isHandoff) {
            editorNoteBody.value = fileData.raw || fileData.content || "";
        } else {
            editorNoteBody.value = fileData.content || "";
        }
        
        // Update target badge
        if (editorNoteSourceBadge) {
            editorNoteSourceBadge.classList.remove("hidden");
            if (isHandoff) {
                editorNoteSourceBadge.className = "badge badge-xs badge-cyan";
                editorNoteSourceBadge.innerHTML = `<i class="fa-solid fa-share-from-square"></i> Written BY the trading agent for its next cycle — rewritten every cycle, injected into the prompt (data/trading/notes)`;
            } else if (isEvolution) {
                editorNoteSourceBadge.className = "badge badge-xs badge-evolution";
                editorNoteSourceBadge.innerHTML = `<i class="fa-solid fa-dna"></i> Target: Self-Evolution Agent (data/evolution/notes)`;
            } else {
                editorNoteSourceBadge.className = "badge badge-xs badge-amber";
                editorNoteSourceBadge.innerHTML = `<i class="fa-solid fa-chart-line"></i> Target: Trading Loop Agents (Embedded in Memory)`;
            }
        }
        
        updateNotePreview();
    }

    function updateNotePreview() {
        if (editorNotePreview) {
            editorNotePreview.innerHTML = ui.renderMarkdown(editorNoteBody.value);
            if (window.renderMathInElement) {
                window.renderMathInElement(editorNotePreview, {
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
    }

    if (editorNoteTarget) {
        editorNoteTarget.addEventListener("change", () => {
            const isEvol = (editorNoteTarget.value === "evolution");
            state.activeNoteSource = editorNoteTarget.value;
            if (isEvol) {
                editorNoteCategory.innerHTML = `
                    <option value="carry_forward">Carry-Forward / Backlog</option>
                    <option value="evolution">Evolution Note</option>
                    <option value="instruction">Instruction</option>
                    <option value="general">General</option>
                `;
                editorNoteCategory.value = "carry_forward";
                if (editorNoteSourceBadge) {
                    editorNoteSourceBadge.className = "badge badge-xs badge-evolution";
                    editorNoteSourceBadge.innerHTML = `<i class="fa-solid fa-dna"></i> Target: Self-Evolution Agent (data/evolution/notes)`;
                }
            } else {
                editorNoteCategory.innerHTML = `
                    <option value="instruction">Instruction (Rules)</option>
                    <option value="market_insight">Market Insight</option>
                    <option value="strategy_hint">Strategy Hint</option>
                    <option value="observation">Observation</option>
                    <option value="general">General</option>
                `;
                editorNoteCategory.value = "general";
                if (editorNoteSourceBadge) {
                    editorNoteSourceBadge.className = "badge badge-xs badge-amber";
                    editorNoteSourceBadge.innerHTML = `<i class="fa-solid fa-chart-line"></i> Target: Trading Loop Agents (Embedded in Memory)`;
                }
            }
        });
    }

    if (editorNoteBody) {
        editorNoteBody.addEventListener("input", updateNotePreview);
    }

    if (notesBtnNew) {
        notesBtnNew.addEventListener("click", createNewNote);
    }

    function createNewNote() {
        state.isEditingNewNote = true;
        state.activeNoteFile = null;
        state.activeNoteSource = "user";
        
        updateMemoryHash();
        
        // Clear highlights in sidebar
        ui.renderNotesFileList(state.notesFiles, null, notesListContainer, selectNoteFile);
        
        showNoteEditor({
            title: "",
            filename: "",
            category: "general",
            priority: "normal",
            content: "",
            source: "user",
        });
        
        editorNoteTitle.focus();
    }

    if (editorBtnSave) {
        editorBtnSave.addEventListener("click", saveCurrentNote);
    }

    async function saveCurrentNote() {
        let filename = editorNoteTitle.value.trim();
        if (!filename) {
            await ui.showAlert("Invalid Filename", "Please specify a filename for this note.", { isError: true });
            return;
        }
        
        if (!filename.endsWith(".md") && !filename.endsWith(".txt")) {
            filename += ".md";
        }
        
        const payload = {
            category: editorNoteCategory.value,
            priority: editorNotePriority.value,
            content: editorNoteBody.value,
            source: (editorNoteTarget ? editorNoteTarget.value : state.activeNoteSource) || "user",
        };
        
        try {
            editorBtnSave.disabled = true;
            editorBtnSave.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Saving...`;
            
            appendLogLine(`[SYSTEM] Saving note file '${filename}' to disk...`);
            const res = await api.saveNoteFile(filename, payload);
            appendLogLine(`[SYSTEM] Note saved: ${res.message}`);
            
            state.activeNoteFile = filename;
            state.isEditingNewNote = false;
            
            updateMemoryHash();
            
            // Reload files
            await loadNoteFilesList();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to save note: ${err.message}`);
            await ui.showAlert("Save Failed", `Could not save note: ${err.message}`, { isError: true });
        } finally {
            editorBtnSave.disabled = false;
            editorBtnSave.innerHTML = `<i class="fa-solid fa-floppy-disk"></i> Save Note`;
        }
    }

    if (editorBtnDelete) {
        editorBtnDelete.addEventListener("click", deleteCurrentNote);
    }

    async function deleteCurrentNote() {
        const filename = state.activeNoteFile;
        if (!filename) {
            if (state.isEditingNewNote) {
                clearNoteEditor();
            }
            return;
        }
        
        const confirmed = await ui.showConfirm(
            "Delete Note File",
            `Are you sure you want to delete note file '${filename}'? This cannot be undone.`,
            { confirmText: "Delete", isWarning: true }
        );
        if (confirmed) {
            try {
                editorBtnDelete.disabled = true;
                editorBtnDelete.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Deleting...`;
                
                appendLogLine(`[SYSTEM] Deleting note file '${filename}'...`);
                const res = await api.deleteNoteFile(filename, state.activeNoteSource);
                appendLogLine(`[SYSTEM] ${res.message}`);
                
                clearNoteEditor();
                await loadNoteFilesList();
            } catch (err) {
                appendLogLine(`[ERROR] Failed to delete note: ${err.message}`);
                await ui.showAlert("Delete Failed", `Could not delete note: ${err.message}`, { isError: true });
            } finally {
                editorBtnDelete.disabled = false;
                editorBtnDelete.innerHTML = `<i class="fa-solid fa-trash"></i> Delete`;
            }
        }
    }

    function clearNoteEditor() {
        state.activeNoteFile = null;
        state.activeNoteSource = null;
        state.isEditingNewNote = false;
        
        updateMemoryHash();
        
        editorEmptyState.classList.remove("hidden");
        editorHeader.classList.add("hidden");
        editorSettingsRow.classList.add("hidden");
        editorBodySplit.classList.add("hidden");
        
        editorNoteTitle.value = "";
        editorNoteBody.value = "";
        if (editorNotePreview) editorNotePreview.innerHTML = "";
        if (editorNoteSourceBadge) editorNoteSourceBadge.classList.add("hidden");
    }

    // ── SYSTEM INSTRUCTIONS MANAGEMENT ──

    if (btnInstructionTabSummary) {
        btnInstructionTabSummary.addEventListener("click", () => {
            btnInstructionTabSummary.classList.add("active");
            btnInstructionTabFull.classList.remove("active");
            btnInstructionTabDiff.classList.remove("active");
            
            instructionSummaryPreview.classList.remove("hidden");
            instructionContentPreview.classList.add("hidden");
            instructionDiffPreview.classList.add("hidden");
        });
    }

    if (btnInstructionTabFull) {
        btnInstructionTabFull.addEventListener("click", () => {
            if (btnInstructionTabSummary) btnInstructionTabSummary.classList.remove("active");
            btnInstructionTabFull.classList.add("active");
            btnInstructionTabDiff.classList.remove("active");
            
            if (instructionSummaryPreview) instructionSummaryPreview.classList.add("hidden");
            instructionContentPreview.classList.remove("hidden");
            instructionDiffPreview.classList.add("hidden");
        });
    }
    
    if (btnInstructionTabDiff) {
        btnInstructionTabDiff.addEventListener("click", () => {
            if (btnInstructionTabSummary) btnInstructionTabSummary.classList.remove("active");
            btnInstructionTabFull.classList.remove("active");
            btnInstructionTabDiff.classList.add("active");
            
            if (instructionSummaryPreview) instructionSummaryPreview.classList.add("hidden");
            instructionContentPreview.classList.add("hidden");
            instructionDiffPreview.classList.remove("hidden");
        });
    }

    // Algorithm View Tab Selectors
    if (btnAlgorithmTabSummary) {
        btnAlgorithmTabSummary.addEventListener("click", () => {
            btnAlgorithmTabSummary.classList.add("active");
            btnAlgorithmTabFull.classList.remove("active");
            btnAlgorithmTabDiff.classList.remove("active");
            
            algorithmSummaryPreview.classList.remove("hidden");
            algorithmContentPreview.classList.add("hidden");
            algorithmDiffPreview.classList.add("hidden");
        });
    }

    if (btnAlgorithmTabFull) {
        btnAlgorithmTabFull.addEventListener("click", () => {
            if (btnAlgorithmTabSummary) btnAlgorithmTabSummary.classList.remove("active");
            btnAlgorithmTabFull.classList.add("active");
            btnAlgorithmTabDiff.classList.remove("active");
            
            if (algorithmSummaryPreview) algorithmSummaryPreview.classList.add("hidden");
            algorithmContentPreview.classList.remove("hidden");
            algorithmDiffPreview.classList.add("hidden");
        });
    }
    
    if (btnAlgorithmTabDiff) {
        btnAlgorithmTabDiff.addEventListener("click", () => {
            if (btnAlgorithmTabSummary) btnAlgorithmTabSummary.classList.remove("active");
            btnAlgorithmTabFull.classList.remove("active");
            btnAlgorithmTabDiff.classList.add("active");
            
            if (algorithmSummaryPreview) algorithmSummaryPreview.classList.add("hidden");
            algorithmContentPreview.classList.add("hidden");
            algorithmDiffPreview.classList.remove("hidden");
        });
    }


    if (btnActivateInstruction) {
        btnActivateInstruction.addEventListener("click", async () => {
            if (!state.activeAgent || !state.activeInstructionVersion) return;
            const agentName = state.activeAgent.name;
            const version = state.activeInstructionVersion;
            
            const confirmed = await ui.showConfirm(
                "Activate Agent Instructions",
                `Are you sure you want to promote and activate version "${version}" for the ${agentName.replace("_", " ")} agent?`,
                { confirmText: "Activate", isWarning: false }
            );
            if (confirmed) {
                try {
                    btnActivateInstruction.disabled = true;
                    btnActivateInstruction.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Activating...`;
                    appendLogLine(`[SYSTEM] Activating instructions "${version}" for agent "${agentName}"...`);
                    const res = await api.activateInstructionVersion(agentName, version);
                    appendLogLine(`[SYSTEM] ${res.message}`);
                    
                    // Refresh agents list and reload selected agent details
                    await loadInstructionsList();
                    if (refreshDashboardFn) {
                        await refreshDashboardFn();
                    }
                } catch (err) {
                    appendLogLine(`[ERROR] Failed to activate instructions: ${err.message}`);
                } finally {
                    btnActivateInstruction.disabled = false;
                    btnActivateInstruction.innerHTML = `<i class="fa-solid fa-circle-check"></i> Activate Version`;
                }
            }
        });
    }

    if (btnRejectInstruction) {
        btnRejectInstruction.addEventListener("click", async () => {
            if (!state.activeInstructionAgent || !state.activeInstructionVersion) return;
            const agentName = state.activeInstructionAgent;
            const version = state.activeInstructionVersion;
            
            const confirmed = await ui.showConfirm(
                "Reject Instruction Version",
                `Are you sure you want to reject instruction version "${version}" for agent "${agentName}"?`,
                { confirmText: "Reject", isWarning: true }
            );
            if (confirmed) {
                try {
                    btnRejectInstruction.disabled = true;
                    btnRejectInstruction.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Rejecting...`;
                    appendLogLine(`[SYSTEM] Rejecting instruction version "${version}" for agent "${agentName}"...`);
                    const res = await api.rejectInstructionVersion(agentName, version);
                    appendLogLine(`[SYSTEM] ${res.message}`);
                    
                    await loadInstructionsList();
                } catch (err) {
                    appendLogLine(`[ERROR] Failed to reject instruction: ${err.message}`);
                    await ui.showAlert("Action Failed", `Could not reject instruction: ${err.message}`, { isError: true });
                } finally {
                    btnRejectInstruction.disabled = false;
                    btnRejectInstruction.innerHTML = `<i class="fa-solid fa-circle-xmark"></i> Reject`;
                }
            }
        });
    }

    async function loadInstructionsList() {
        try {
            const data = await api.fetchInstructions();
            state.instructionsAgents = data.agents || [];
            
            let selectedAgentName = state.activeInstructionAgent || (state.activeAgent ? state.activeAgent.name : null);
            let selectedVersion = state.activeInstructionVersion || null;
            
            ui.renderInstructionsAgentList(
                state.instructionsAgents, 
                selectedAgentName, 
                selectedVersion, 
                instructionsListContainer, 
                selectInstructionsAgentVersion
            );
            
            if (selectedAgentName && selectedVersion) {
                await selectInstructionsAgentVersion({ name: selectedAgentName }, selectedVersion);
            } else if (selectedAgentName) {
                // Find agent and select its active version
                const agentObj = state.instructionsAgents.find(a => a.name === selectedAgentName);
                if (agentObj) {
                    await selectInstructionsAgentVersion(agentObj, agentObj.active_version);
                }
            } else {
                instructionEmptyState.classList.remove("hidden");
                instructionHeader.classList.add("hidden");
                instructionBodySplit.classList.add("hidden");
                if (instructionTabsContainer) instructionTabsContainer.classList.add("hidden");
            }
            if (window.updateProposalBadges) window.updateProposalBadges();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load agents list for instructions: ${err.message}`);
        }
    }

    async function selectInstructionsAgentVersion(agent, version) {
        state.activeAgent = agent;
        state.activeInstructionAgent = agent.name;
        state.activeInstructionVersion = version;
        
        // Refresh sidebar selection highlight
        ui.renderInstructionsAgentList(
            state.instructionsAgents, 
            agent.name, 
            version, 
            instructionsListContainer, 
            selectInstructionsAgentVersion
        );
        
        // Load content for version
        await selectInstructionVersion(agent.name, version);
    }

    async function selectInstructionVersion(agentName, version) {
        state.activeInstructionAgent = agentName;
        state.activeInstructionVersion = version;
        
        updateMemoryHash();
        
        try {
            const data = await api.fetchInstructionVersion(agentName, version);
            
            // Show header/split elements
            instructionEmptyState.classList.add("hidden");
            instructionHeader.classList.remove("hidden");
            instructionBodySplit.classList.remove("hidden");
            
            // Update labels
            if (instructionAgentName) {
                instructionAgentName.textContent = agentName.replace("_", " ");
            }
            if (instructionActiveBadge) {
                if (data.is_active === true) {
                    instructionActiveBadge.classList.remove("hidden");
                } else {
                    instructionActiveBadge.classList.add("hidden");
                }
            }
            
            // Handle buttons
            const isProposed = !data.is_active && data.metadata && data.metadata.status === "proposed";
            
            if (btnActivateInstruction) {
                btnActivateInstruction.classList.toggle("hidden", data.is_active);
            }
            if (btnRejectInstruction) {
                btnRejectInstruction.classList.toggle("hidden", !isProposed);
            }
            if (instructionActionsSeparator) {
                instructionActionsSeparator.style.display = isProposed ? "inline" : "none";
            }

            // Update preview content
            if (instructionContentPreview) {
                instructionContentPreview.innerHTML = ui.renderMarkdown(data.content || "");
            }

            // Setup Diff and Summary Tabs
            
            if (instructionTabsContainer) {
                if (data.diff) {
                    instructionTabsContainer.classList.remove("hidden");
                    if (instructionDiffPreview) {
                        instructionDiffPreview.innerHTML = ui.renderDiff(data.diff);
                    }
                } else {
                    instructionTabsContainer.classList.add("hidden");
                    if (instructionDiffPreview) {
                        instructionDiffPreview.innerHTML = "";
                    }
                }
                
                const hasSummary = data.metadata && data.metadata.reasoning;
                
                if (hasSummary) {
                    btnInstructionTabSummary.classList.remove("hidden");
                    
                    // Generate summary HTML
                    const reasoning = data.metadata.reasoning || "No reasoning provided.";
                    const proposedAt = data.metadata.proposed_at ? ui.formatTime(data.metadata.proposed_at, true) : "Unknown date";
                    const diffSummary = data.metadata.diff_summary || "See diff view";
                    const titleText = isProposed ? "Proposed Version Summary" : "Version Summary";
                    
                    if (instructionSummaryPreview) {
                        instructionSummaryPreview.innerHTML = `
                            <h2>${titleText}</h2>
                            <div style="display: flex; gap: 1rem; color: var(--text-dim); margin-bottom: 1.5rem; font-size: 0.9rem;">
                                <span><i class="fa-regular fa-clock"></i> Proposed: ${proposedAt}</span>
                                <span>│</span>
                                <span><i class="fa-solid fa-code-compare"></i> ${diffSummary}</span>
                            </div>
                            <div class="proposed-reasoning-body">
                                ${ui.renderMarkdown(reasoning)}
                            </div>
                        `;
                    }
                    
                    if (isProposed) {
                        // Auto-select summary tab
                        btnInstructionTabSummary.click();
                    } else {
                        // Auto-select full content tab if it's already active/archived
                        btnInstructionTabFull.click();
                    }
                } else {
                    btnInstructionTabSummary.classList.add("hidden");
                    // Auto-select full content tab
                    btnInstructionTabFull.click();
                }
            }
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load instructions for ${agentName} (${version}): ${err.message}`);
        }
    }

    // ── VECTOR EXPLORER ACTIONS ──

    async function loadVectorExplorerData() {
        try {
            // 1. Fetch Stats
            const stats = await api.fetchMemoryStats();
            if (vstatTrades) {
                const val = stats.trade_experiences || 0;
                vstatTrades.textContent = val;
                vstatTrades.title = val;
            }
            if (vstatNotes) {
                const val = stats.user_notes || 0;
                vstatNotes.textContent = val;
                vstatNotes.title = val;
            }
            if (vstatPatterns) {
                const val = stats.market_patterns || 0;
                vstatPatterns.textContent = val;
                vstatPatterns.title = val;
            }
            
            // 2. Fetch Stored Notes Collection
            const notesRes = await api.fetchMemoryNotes();
            ui.renderMemoryNotesTable(notesRes.notes || [], vecNotesTableBody);
            
            // 3. Fetch Stored Trades Collection
            const tradesRes = await api.fetchMemoryTrades();
            ui.renderMemoryTradesTable(tradesRes.trades || [], vecTradesTableBody);
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load vector explorer data: ${err.message}`);
        }
    }

    if (vecBtnPrune) {
        vecBtnPrune.addEventListener("click", pruneMemoryData);
    }

    async function pruneMemoryData() {
        const confirmed = await ui.showConfirm(
            "Prune Stale Memories",
            "Are you sure you want to prune stale memories? This runs a routine to delete experiences older than 90 days from the vector DB.",
            { confirmText: "Prune", isWarning: true }
        );
        if (!confirmed) {
            return;
        }
        
        try {
            appendLogLine("[SYSTEM] Pruning stale memory entries (>90 days old)...");
            const res = await api.pruneMemory();
            appendLogLine(`[SYSTEM] Pruning complete. Pruned counts: ${JSON.stringify(res.pruned || {})}`);
            await loadVectorExplorerData();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to prune memory: ${err.message}`);
        }
    }

    if (vecBtnClear) {
        vecBtnClear.addEventListener("click", clearAllMemoryCollections);
    }

    async function clearAllMemoryCollections() {
        const confirmed = await ui.showConfirm(
            "Clear Memory Collections",
            "WARNING: Are you sure you want to clear all memory collections? This wipes all vector database collections completely, then re-embeds user note files from disk fresh.",
            { confirmText: "Reset All", isWarning: true }
        );
        if (!confirmed) {
            return;
        }
        
        try {
            appendLogLine("[SYSTEM] Resetting ChromaDB memory collections and re-indexing user notes...");
            const res = await api.clearMemory();
            appendLogLine(`[SYSTEM] Reset complete: ${res.message}`);
            await loadVectorExplorerData();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to reset memory: ${err.message}`);
        }
    }

    // ── ALGORITHM EXPLORER ACTIONS ──

    // Algorithms Explorer Tab Selectors
    if (btnAlgorithmTabFull) {
        btnAlgorithmTabFull.addEventListener("click", () => {
            btnAlgorithmTabFull.classList.add("active");
            btnAlgorithmTabDiff.classList.remove("active");
            algorithmContentPreview.classList.remove("hidden");
            algorithmDiffPreview.classList.add("hidden");
        });
    }
    if (btnAlgorithmTabDiff) {
        btnAlgorithmTabDiff.addEventListener("click", () => {
            btnAlgorithmTabFull.classList.remove("active");
            btnAlgorithmTabDiff.classList.add("active");
            algorithmContentPreview.classList.add("hidden");
            algorithmDiffPreview.classList.remove("hidden");
        });
    }

    // Activate Algorithm Click Handler
    if (btnActivateAlgorithm) {
        btnActivateAlgorithm.addEventListener("click", async () => {
            if (!state.activeAlgorithm) return;
            const version = state.activeAlgorithm.version;
            const confirmed = await ui.showConfirm(
                "Activate Algorithm Version",
                `Are you sure you want to promote and activate algorithm version "${version}"? This updates the active configuration.`,
                { confirmText: "Activate", isWarning: false }
            );
            if (confirmed) {
                try {
                    btnActivateAlgorithm.disabled = true;
                    btnActivateAlgorithm.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Activating...`;
                    appendLogLine(`[SYSTEM] Activating algorithm version "${version}"...`);
                    const res = await api.activateAlgorithmVersion(version);
                    appendLogLine(`[SYSTEM] ${res.message}`);
                    
                    // Refresh list and reload details
                    await loadAlgorithmsList();
                    if (refreshDashboardFn) {
                        await refreshDashboardFn();
                    }
                } catch (err) {
                    appendLogLine(`[ERROR] Failed to activate algorithm version: ${err.message}`);
                } finally {
                    btnActivateAlgorithm.disabled = false;
                    btnActivateAlgorithm.innerHTML = `<i class="fa-solid fa-circle-check"></i> Activate Version`;
                }
            }
        });
    }

    // Reject Algorithm Click Handler
    if (btnRejectAlgorithm) {
        btnRejectAlgorithm.addEventListener("click", async () => {
            if (!state.activeAlgorithm) return;
            const version = state.activeAlgorithm.version;
            const confirmed = await ui.showConfirm(
                "Reject Algorithm Version",
                `Are you sure you want to reject algorithm version "${version}"?`,
                { confirmText: "Reject", confirmClass: "btn-danger", isWarning: true }
            );
            if (confirmed) {
                try {
                    btnRejectAlgorithm.disabled = true;
                    btnRejectAlgorithm.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Rejecting...`;
                    appendLogLine(`[SYSTEM] Rejecting algorithm version "${version}"...`);
                    const res = await api.rejectAlgorithmVersion(version);
                    appendLogLine(`[SYSTEM] ${res.message}`);
                    
                    // Refresh list and reload details
                    await loadAlgorithmsList();
                    if (refreshDashboardFn) {
                        await refreshDashboardFn();
                    }
                } catch (err) {
                    appendLogLine(`[ERROR] Failed to reject algorithm version: ${err.message}`);
                    await ui.showAlert("Action Failed", `Could not reject algorithm: ${err.message}`, { isError: true });
                } finally {
                    btnRejectAlgorithm.disabled = false;
                    btnRejectAlgorithm.innerHTML = `<i class="fa-solid fa-xmark"></i> Reject`;
                }
            }
        });
    }

    async function loadAlgorithmsList() {
        try {
            const data = await api.fetchAlgorithms();
            state.algorithms = data.versions || [];
            state.activeAlgorithmVersion = data.active_version;
            
            // Find selected version details if already active, or default selected
            let selectedVer = state.selectedAlgorithmVersion;
            if (!selectedVer && state.algorithms.length > 0) {
                // Pick active one first, otherwise first one
                const activeAlgo = state.algorithms.find(a => a.is_active);
                selectedVer = activeAlgo ? activeAlgo.version : state.algorithms[0].version;
            }
            
            ui.renderAlgorithmsList(state.algorithms, selectedVer, algorithmsListContainer, (algo) => {
                selectAlgorithm(algo);
            });

            // If a version is selected, update details
            if (selectedVer) {
                const currentSelected = state.algorithms.find(a => a.version === selectedVer);
                if (currentSelected) {
                    await selectAlgorithm(currentSelected);
                }
            }
            if (window.updateProposalBadges) window.updateProposalBadges();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load algorithms list: ${err.message}`);
        }
    }

    async function selectAlgorithm(algo) {
        state.selectedAlgorithmVersion = algo.version;
        state.activeAlgorithm = algo;

        updateMemoryHash();

        // Re-render list to update highlights
        ui.renderAlgorithmsList(state.algorithms, algo.version, algorithmsListContainer, (a) => {
            selectAlgorithm(a);
        });

        try {
            const data = await api.fetchAlgorithmVersion(algo.version);
            
            // Unhide elements
            algorithmEmptyState.classList.add("hidden");
            algorithmHeader.classList.remove("hidden");
            algorithmBodySplit.classList.remove("hidden");

            // Set version name & active badge
            if (algorithmVersionName) {
                algorithmVersionName.textContent = algo.version;
            }
            if (algorithmActiveBadge) {
                if (data.is_active === true) {
                    algorithmActiveBadge.classList.remove("hidden");
                } else {
                    algorithmActiveBadge.classList.add("hidden");
                }
            }

            // Handle buttons
            const isProposed = !data.is_active && data.metadata && data.metadata.status === "proposed";
            if (btnActivateAlgorithm) {
                btnActivateAlgorithm.classList.toggle("hidden", data.is_active);
            }
            if (btnRejectAlgorithm) {
                btnRejectAlgorithm.classList.toggle("hidden", !isProposed);
            }
            if (algorithmActionsSeparator) {
                algorithmActionsSeparator.style.display = isProposed ? "inline" : "none";
            }

            // Populate content text
            if (algorithmContentPreview) {
                algorithmContentPreview.textContent = data.content || "";
            }

            // Setup Diff and Summary Tabs
            
            if (algorithmTabsContainer) {
                if (data.diff) {
                    algorithmTabsContainer.classList.remove("hidden");
                    if (algorithmDiffPreview) {
                        algorithmDiffPreview.innerHTML = ui.renderDiff(data.diff);
                    }
                } else {
                    algorithmTabsContainer.classList.add("hidden");
                    if (algorithmDiffPreview) {
                        algorithmDiffPreview.innerHTML = "";
                    }
                }
                
                const hasSummary = data.metadata && (data.metadata.description || data.metadata.reasoning);
                
                if (hasSummary) {
                    btnAlgorithmTabSummary.classList.remove("hidden");
                    
                    // Generate summary HTML
                    const reasoning = data.metadata.description || data.metadata.reasoning || "No reasoning provided.";
                    const proposedAt = data.metadata.created_at ? ui.formatTime(data.metadata.created_at, true) : "Unknown date";
                    const author = data.metadata.created_by || "Unknown";
                    const titleText = isProposed ? "Proposed Version Summary" : "Version Summary";
                    
                    if (algorithmSummaryPreview) {
                        algorithmSummaryPreview.innerHTML = `
                            <h2>${titleText}</h2>
                            <div style="display: flex; gap: 1rem; color: var(--text-dim); margin-bottom: 1.5rem; font-size: 0.9rem;">
                                <span><i class="fa-regular fa-clock"></i> Proposed: ${proposedAt}</span>
                                <span>│</span>
                                <span><i class="fa-regular fa-user"></i> Author: ${author}</span>
                            </div>
                            <div class="proposed-reasoning-body">
                                ${ui.renderMarkdown(reasoning)}
                            </div>
                        `;
                    }
                    
                    if (isProposed) {
                        // Auto-select summary tab
                        btnAlgorithmTabSummary.click();
                    } else {
                        // Auto-select full content tab if it's already active/archived
                        btnAlgorithmTabFull.click();
                    }
                } else {
                    btnAlgorithmTabSummary.classList.add("hidden");
                    // Auto-select full content tab
                    btnAlgorithmTabFull.click();
                }
            }

        } catch (err) {
            appendLogLine(`[ERROR] Failed to load algorithm version details: ${err.message}`);
        }
    }

    // Vector Table Event Handlers for click-to-expand drawer
    if (vecNotesTableBody) {
        vecNotesTableBody.addEventListener("click", (e) => {
            const tr = e.target.closest("tr");
            if (!tr || !tr.dataset.item) return;
            try {
                const note = JSON.parse(tr.dataset.item);
                openMemoryNoteDrawer(note);
            } catch (err) {
                console.error("Failed to parse note data from row:", err);
            }
        });
    }

    if (vecTradesTableBody) {
        vecTradesTableBody.addEventListener("click", (e) => {
            const tr = e.target.closest("tr");
            if (!tr || !tr.dataset.item) return;
            try {
                const trade = JSON.parse(tr.dataset.item);
                openMemoryTradeDrawer(trade);
            } catch (err) {
                console.error("Failed to parse trade data from row:", err);
            }
        });
    }

    function openMemoryNoteDrawer(note) {
        if (!detailDrawer) return;

        setDrawerMetaLabels("Category", "Source", "Priority", "Timestamp");

        // Set Title
        const titleEl = detailDrawer.querySelector(".drawer-title");
        if (titleEl) {
            titleEl.innerHTML = `<i class="fa-solid fa-note-sticky text-cyan"></i> <span>Inspect Memory Note</span>`;
        }

        // Set Metadata Badges
        if (detailAgentBadge) {
            const category = note.metadata.category || "general";
            const iconClass = ui.getAgentIcon(category === "instruction" ? "orchestrator" : category === "market_insight" ? "market_intelligence" : "system");
            detailAgentBadge.innerHTML = `<i class="fa-solid ${iconClass}"></i> ${category.replace("_", " ")}`;
            detailAgentBadge.className = `agent-chip note-category-${category}`;
        }

        if (detailToolName) {
            detailToolName.innerHTML = `<i class="fa-solid fa-file-invoice text-dim"></i> Source: ${note.metadata.source || "unknown"}`;
        }

        if (detailTimestamp) {
            const ts = note.metadata.timestamp || "";
            detailTimestamp.innerHTML = `<i class="fa-solid fa-clock text-dim"></i> ${ts ? ui.formatTime(ts, true) : "N/A"}`;
        }

        if (detailToolSource) {
            const priority = note.metadata.priority || "normal";
            detailToolSource.textContent = priority;
            detailToolSource.className = `type-badge priority-${priority}`;
        }

        if (detailMcpAddressContainer) {
            detailMcpAddressContainer.classList.add("hidden");
        }

        // Set Tabs
        if (tabArgs) {
            tabArgs.innerHTML = `<i class="fa-solid fa-file-lines"></i> Note Content`;
        }
        if (tabResp) {
            tabResp.innerHTML = `<i class="fa-solid fa-tags"></i> Metadata`;
        }

        // Set Content
        if (detailArgsPre) {
            detailArgsPre.className = "proposal-view";
            detailArgsPre.innerHTML = `
                <div class="proposal-container">
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-file-lines text-purple"></i> Document Content</span>
                        <div class="proposal-card-content">
                            <p style="white-space: pre-wrap; font-family: var(--font-sans);">${ui.escapeHtml(note.text)}</p>
                        </div>
                    </div>
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-fingerprint text-purple"></i> Embedding ID</span>
                        <div class="proposal-card-content font-mono" style="font-size: 0.8rem; word-break: break-all;">
                            ${ui.escapeHtml(note.id)}
                        </div>
                    </div>
                </div>
            `;
        }

        if (detailRespPre) {
            detailRespPre.className = "font-mono json-display";
            detailRespPre.textContent = JSON.stringify(note.metadata || {}, null, 2);
        }

        // Activate Tab 1
        if (tabArgs) tabArgs.classList.add("active");
        if (tabResp) tabResp.classList.remove("active");
        if (panelArgs) panelArgs.classList.add("active");
        if (panelResp) panelResp.classList.remove("active");

        // Show Delete Button and configure click handler
        const btnDelete = document.getElementById("detail-drawer-delete");
        if (btnDelete) {
            btnDelete.classList.remove("hidden");
            // Remove old listeners by cloning
            const newBtn = btnDelete.cloneNode(true);
            btnDelete.parentNode.replaceChild(newBtn, btnDelete);
            
            newBtn.addEventListener("click", async () => {
                const confirmed = await ui.showConfirm(
                    "Delete Memory Note",
                    "Are you sure you want to permanently delete this memory note?",
                    { confirmText: "Delete", isWarning: true }
                );
                if (confirmed) {
                    try {
                        newBtn.disabled = true;
                        newBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i>`;
                        await api.apiDeleteNote(note.id);
                        // Close drawer
                        const closeBtn = document.getElementById("detail-drawer-close");
                        if (closeBtn) {
                            closeBtn.click();
                        }
                        // Refresh data
                        await loadVectorExplorerData();
                    } catch (err) {
                        await ui.showAlert("Error", `Failed to delete note: ${err.message}`, { isError: true });
                    } finally {
                        newBtn.disabled = false;
                        newBtn.innerHTML = `<i class="fa-solid fa-trash-can"></i>`;
                    }
                }
            });
        }

        // Open Drawer
        detailDrawer.classList.remove("collapsed");
        detailDrawer.classList.add("open");
        if (detailDrawerBackdrop) {
            detailDrawerBackdrop.classList.add("active");
            detailDrawerBackdrop.classList.remove("hidden");
        }
    }

    function openMemoryTradeDrawer(trade) {
        if (!detailDrawer) return;

        // Set Title
        setDrawerMetaLabels("Regime", "Outcome", "Action", "Timestamp");

        const titleEl = detailDrawer.querySelector(".drawer-title");
        if (titleEl) {
            titleEl.innerHTML = `<i class="fa-solid fa-chart-line text-cyan"></i> <span>Inspect Trade Experience</span>`;
        }

        // Set Metadata Badges
        if (detailAgentBadge) {
            const regime = trade.metadata.regime || "general";
            detailAgentBadge.innerHTML = `<i class="fa-solid fa-chart-line"></i> ${regime}`;
            detailAgentBadge.className = `agent-chip trade-regime`;
        }

        if (detailToolName) {
            const pnl = parseFloat(trade.metadata.outcome_pnl || 0);
            let pnlText = pnl > 0 ? `+$${pnl.toFixed(2)}` : pnl < 0 ? `-$${Math.abs(pnl).toFixed(2)}` : "$0.00";
            detailToolName.innerHTML = `<i class="fa-solid fa-money-bill-trend-up text-dim"></i> P&L: ${pnlText}`;
        }

        if (detailTimestamp) {
            const ts = trade.metadata.timestamp || "";
            detailTimestamp.innerHTML = `<i class="fa-solid fa-clock text-dim"></i> ${ts ? ui.formatTime(ts, true) : "N/A"}`;
        }

        if (detailToolSource) {
            const ticker = trade.metadata.ticker || "unknown";
            const action = trade.metadata.action || "TRADE";
            detailToolSource.textContent = `${ticker} ${action}`;
            detailToolSource.className = `type-badge trade-action-${action.toLowerCase()}`;
        }

        if (detailMcpAddressContainer) {
            detailMcpAddressContainer.classList.add("hidden");
        }

        // Set Tabs
        if (tabArgs) {
            tabArgs.innerHTML = `<i class="fa-solid fa-receipt"></i> Details`;
        }
        if (tabResp) {
            tabResp.innerHTML = `<i class="fa-solid fa-tags"></i> Metadata`;
        }

        // Set Content
        if (detailArgsPre) {
            detailArgsPre.className = "proposal-view";
            
            let detailsHtml = `
                <div class="proposal-container">
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-message text-purple"></i> Experience Summary</span>
                        <div class="proposal-card-content">
                            <p style="white-space: pre-wrap; font-family: var(--font-sans);">${ui.escapeHtml(trade.text)}</p>
                        </div>
                    </div>
            `;
            
            if (trade.metadata.reasoning) {
                detailsHtml += `
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-brain text-purple"></i> Agent Reasoning</span>
                        <div class="proposal-card-content">
                            <p style="white-space: pre-wrap; font-family: var(--font-sans);">${ui.escapeHtml(trade.metadata.reasoning)}</p>
                        </div>
                    </div>
                `;
            }
            
            detailsHtml += `
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-circle-info text-purple"></i> Trade Execution Parameters</span>
                        <div class="proposal-card-content">
                            <table class="comparison-table" style="width: 100%; border-collapse: collapse; font-size: 0.85rem;">
                                <tbody>
                                    <tr>
                                        <td style="padding: 0.4rem; font-weight: 600; width: 120px; border-bottom: 1px solid var(--border-color);">Ticker</td>
                                        <td style="padding: 0.4rem; border-bottom: 1px solid var(--border-color);">${ui.escapeHtml(trade.metadata.ticker || "N/A")}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 0.4rem; font-weight: 600; border-bottom: 1px solid var(--border-color);">Action</td>
                                        <td style="padding: 0.4rem; border-bottom: 1px solid var(--border-color);">${ui.escapeHtml(trade.metadata.action || "N/A")}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 0.4rem; font-weight: 600; border-bottom: 1px solid var(--border-color);">Quantity</td>
                                        <td style="padding: 0.4rem; border-bottom: 1px solid var(--border-color);">${ui.escapeHtml(trade.metadata.quantity !== undefined ? String(trade.metadata.quantity) : "N/A")}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 0.4rem; font-weight: 600; border-bottom: 1px solid var(--border-color);">Price</td>
                                        <td style="padding: 0.4rem; border-bottom: 1px solid var(--border-color);">$${parseFloat(trade.metadata.price || 0).toFixed(2)}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 0.4rem; font-weight: 600;">Hybrid Score</td>
                                        <td style="padding: 0.4rem;">${parseFloat(trade.metadata.hybrid_score || 0).toFixed(4)}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                    <div class="proposal-card">
                        <span class="proposal-card-title"><i class="fa-solid fa-fingerprint text-purple"></i> Embedding ID</span>
                        <div class="proposal-card-content font-mono" style="font-size: 0.8rem; word-break: break-all;">
                            ${ui.escapeHtml(trade.id)}
                        </div>
                    </div>
                </div>
            `;
            
            detailArgsPre.innerHTML = detailsHtml;
        }

        if (detailRespPre) {
            detailRespPre.className = "font-mono json-display";
            detailRespPre.textContent = JSON.stringify(trade.metadata || {}, null, 2);
        }

        // Activate Tab 1
        if (tabArgs) tabArgs.classList.add("active");
        if (tabResp) tabResp.classList.remove("active");
        if (panelArgs) panelArgs.classList.add("active");
        if (panelResp) panelResp.classList.remove("active");

        // Hide Delete Button for trade experiences
        const btnDelete = document.getElementById("detail-drawer-delete");
        if (btnDelete) {
            btnDelete.classList.add("hidden");
        }

        // Open Drawer
        detailDrawer.classList.remove("collapsed");
        detailDrawer.classList.add("open");
        if (detailDrawerBackdrop) {
            detailDrawerBackdrop.classList.add("active");
            detailDrawerBackdrop.classList.remove("hidden");
        }
    }

    // ── CODE REVIEWS MANAGEMENT ──

    async function loadReviewsList() {
        try {
            const res = await api.fetchReviews();
            state.reviews = res.reviews || [];
            ui.renderReviewsList(state.reviews, state.activeReviewId, reviewsListContainer, selectReview);
            
            if (state.activeReviewId) {
                const currentSelected = state.reviews.find(r => r.id === state.activeReviewId);
                if (currentSelected) {
                    await selectReview(state.activeReviewId);
                } else {
                    clearReviewDetails();
                }
            } else {
                clearReviewDetails();
            }
            if (window.updateProposalBadges) window.updateProposalBadges();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load reviews list: ${err.message}`);
        }
    }

    async function selectReview(reviewId) {
        state.activeReviewId = reviewId;
        updateMemoryHash();
        
        // Highlight in sidebar
        ui.renderReviewsList(state.reviews, reviewId, reviewsListContainer, selectReview);
        
        try {
            const data = await api.fetchReview(reviewId);
            
            // Show workspace components
            reviewEmptyState.classList.add("hidden");
            reviewHeader.classList.remove("hidden");
            reviewBodySplit.classList.remove("hidden");
            
            if (reviewName) reviewName.textContent = data.title || reviewId;
            if (reviewCreatedTime) {
                const dateStr = data.generated ? ui.formatTime(data.generated, true) : "Unknown Date";
                reviewCreatedTime.innerHTML = `<i class="fa-regular fa-clock"></i> Generated: ${dateStr}`;
            }
            
            const isApplied = data.status === "APPLIED" || data.status === "ACTIVE";
            const isRejected = data.status === "REJECTED";
            const isResolved = isApplied || isRejected;
            
            if (reviewActiveBadge) reviewActiveBadge.classList.toggle("hidden", !isApplied);
            if (reviewPendingBadge) reviewPendingBadge.classList.toggle("hidden", isResolved);
            if (reviewRejectedBadge) reviewRejectedBadge.classList.toggle("hidden", !isRejected);
            
            if (btnApplyReview) btnApplyReview.classList.toggle("hidden", isResolved);
            if (btnRejectReview) btnRejectReview.classList.toggle("hidden", isResolved);
            
            if (reviewContentPreview) {
                reviewContentPreview.innerHTML = ui.renderMarkdown(data.content || "");
            }
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load review details for ${reviewId}: ${err.message}`);
        }
    }

    function clearReviewDetails() {
        state.activeReviewId = null;
        updateMemoryHash();
        
        reviewEmptyState.classList.remove("hidden");
        reviewHeader.classList.add("hidden");
        reviewBodySplit.classList.add("hidden");
        
        if (reviewName) reviewName.textContent = "";
        if (reviewContentPreview) reviewContentPreview.innerHTML = "";
    }

    if (btnApplyReview) {
        btnApplyReview.addEventListener("click", applySelectedReview);
    }

    async function applySelectedReview() {
        if (!state.activeReviewId) return;
        const reviewId = state.activeReviewId;
        
        const confirmed = await ui.showConfirm(
            "Mark Proposal as Applied",
            `Are you sure you want to mark "${reviewId}" as applied? This indicates you have implemented these changes.`,
            { confirmText: "Mark Applied", isWarning: false }
        );
        if (confirmed) {
            try {
                btnApplyReview.disabled = true;
                btnApplyReview.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Saving...`;
                appendLogLine(`[SYSTEM] Marking proposal "${reviewId}" as applied...`);
                const res = await api.applyReview(reviewId);
                appendLogLine(`[SYSTEM] ${res.message}`);
                
                await loadReviewsList();
                if (refreshDashboardFn) {
                    await refreshDashboardFn();
                }
            } catch (err) {
                appendLogLine(`[ERROR] Failed to apply code review: ${err.message}`);
                await ui.showAlert("Action Failed", `Could not mark proposal as applied: ${err.message}`, { isError: true });
            } finally {
                btnApplyReview.disabled = false;
                btnApplyReview.innerHTML = `<i class="fa-solid fa-circle-check"></i> Mark as Applied`;
            }
        }
    }

    if (btnRejectReview) {
        btnRejectReview.addEventListener("click", async () => {
            if (!state.activeReviewId) return;
            const reviewId = state.activeReviewId;
            
            const confirmed = await ui.showConfirm(
                "Reject Proposal",
                `Are you sure you want to reject "${reviewId}"?`,
                { confirmText: "Reject", isWarning: true }
            );
            if (confirmed) {
                try {
                    btnRejectReview.disabled = true;
                    btnRejectReview.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Rejecting...`;
                    appendLogLine(`[SYSTEM] Rejecting proposal "${reviewId}"...`);
                    const res = await api.rejectReview(reviewId);
                    appendLogLine(`[SYSTEM] ${res.message}`);
                    
                    await loadReviewsList();
                    if (refreshDashboardFn) {
                        await refreshDashboardFn();
                    }
                } catch (err) {
                    appendLogLine(`[ERROR] Failed to reject code review: ${err.message}`);
                    await ui.showAlert("Action Failed", `Could not reject proposal: ${err.message}`, { isError: true });
                } finally {
                    btnRejectReview.disabled = false;
                    btnRejectReview.innerHTML = `<i class="fa-solid fa-circle-xmark"></i> Reject`;
                }
            }
        });
    }
}
