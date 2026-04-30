"""Controls JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_CONTROLS = r"""function renderNotes(node) {
  const canSubmit = canPostToGraphServer();
  const disabled = canSubmit ? "" : " disabled aria-disabled=\"true\"";
  return `
    <p class="summary-text">Notes save locally in this browser and, when served by graph-server, sync into .code_index/graph-notes.json for agent review.</p>
    <textarea class="note-box" id="node-note" placeholder="Leave instructions, questions, review notes, or constraints for this node.">${escapeHtml(noteText(node))}</textarea>
    <div class="actions">
      <button class="small-button" id="save-note" type="button">Save note</button>
      <button class="small-button" id="copy-task-json" type="button">Copy task JSON</button>
      <button class="small-button" id="export-notes-json" type="button">Export notes JSON</button>
    </div>
    <div class="section">
      <h3>Task</h3>
      <textarea class="note-box task-box" id="agent-task" placeholder="Ask the coding agent to work on this node.">${escapeHtml(noteText(node))}</textarea>
      <div class="actions">
        <button class="small-button" id="submit-agent-task" type="button"${disabled}>Submit task</button>
        <span class="inline-status" id="agent-task-status">${canSubmit ? "" : "Graph server required"}</span>
      </div>
    </div>
    <div class="section">
      <h3>Agent Context</h3>
      <p class="summary-text">${escapeHtml(node.summary)}</p>
    </div>
  `;
}
function bindNotesPanel(node) {
  const noteBox = document.getElementById("node-note");
  const saveButton = document.getElementById("save-note");
  const copyButton = document.getElementById("copy-task-json");
  const exportButton = document.getElementById("export-notes-json");
  const taskBox = document.getElementById("agent-task");
  const submitTaskButton = document.getElementById("submit-agent-task");
  const taskStatus = document.getElementById("agent-task-status");
  if (saveButton && noteBox) {
    saveButton.addEventListener("click", () => {
      const value = noteBox.value.trim();
      if (value) {
        notes[node.id] = {
          note: value,
          path: node.path,
          kind: node.kind,
          care_level: node.care_level,
          summary: node.summary,
          updated_at: new Date().toISOString()
        };
      } else {
        delete notes[node.id];
      }
      saveNotes();
      postNoteToServer(node, notes[node.id] || {
        note: "",
        path: node.path,
        kind: node.kind,
        care_level: node.care_level,
        summary: node.summary,
        updated_at: new Date().toISOString()
      });
      saveButton.textContent = "Saved";
      setTimeout(() => { saveButton.textContent = "Save note"; }, 900);
    });
  }
  if (copyButton) {
    copyButton.addEventListener("click", async () => {
      await copyJson(agentTaskPayload(node, taskBox ? taskBox.value.trim() : noteText(node)));
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy task JSON"; }, 900);
    });
  }
  if (exportButton) {
    exportButton.addEventListener("click", () => {
      exportJson(notePayload(), "code-index-graph-notes.json");
    });
  }
  if (submitTaskButton && taskBox && taskStatus) {
    submitTaskButton.addEventListener("click", async () => {
      const message = taskBox.value.trim();
      if (!message) {
        taskStatus.textContent = "Task text required";
        return;
      }
      submitTaskButton.disabled = true;
      taskStatus.textContent = "Submitting";
      try {
        const payload = applyPreflightConfirmation(
          agentTaskPayload(node, message),
          submitTaskButton
        );
        const result = await postAgentTaskToServer(payload);
        if (handlePreflightResult(result, submitTaskButton, taskStatus, "Submit anyway")) return;
        if (result.ok) {
          taskStatus.textContent = result.dispatch && result.dispatch.configured
            ? `Submitted ${result.run.run_id.slice(0, 8)}`
            : `Queued ${result.run.run_id.slice(0, 8)}`;
          applyAgentRunResponse(result);
          resetPreflightButton(submitTaskButton, "Submit task");
          openRunTranscriptFromResponse(result);
        } else if (result.copied) {
          taskStatus.textContent = "Copied task JSON";
        }
      } catch (err) {
        taskStatus.textContent = err.message || "Task failed";
      } finally {
        submitTaskButton.disabled = !canPostToGraphServer();
      }
    });
  }
}
function parentNode(node) {
  if (!node) return null;
  if (node.kind === "file") return nodeById.get(directoryNodeId(node.directory || "."));
  if (node.path === ".") return null;
  return nodeById.get(directoryNodeId(node.directory || "."));
}
function clampZoom(value) {
  return Math.max(0.25, Math.min(3.5, value));
}
function zoomAt(clientX, clientY, factor) {
  const rect = svg.getBoundingClientRect();
  const nextK = clampZoom(transform.k * factor);
  const graphX = (clientX - rect.left - transform.x) / transform.k;
  const graphY = (clientY - rect.top - transform.y) / transform.k;
  transform.x = clientX - rect.left - graphX * nextK;
  transform.y = clientY - rect.top - graphY * nextK;
  transform.k = nextK;
  setViewportTransform();
}
function zoomBy(factor) {
  const rect = svg.getBoundingClientRect();
  zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
}
function fitVisibleNodes() {
  const visibleNodes = nodes.filter(node => node.visible);
  const targetNodes = visibleNodes.length ? visibleNodes : nodes;
  if (!targetNodes.length) return;
  const rect = svg.getBoundingClientRect();
  const width = rect.width || 900;
  const height = rect.height || 600;
  const padding = 60;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  targetNodes.forEach(node => {
    const radius = fileRadius(node) + 42;
    minX = Math.min(minX, node.x - radius);
    minY = Math.min(minY, node.y - radius);
    maxX = Math.max(maxX, node.x + radius);
    maxY = Math.max(maxY, node.y + radius);
  });
  const graphWidth = Math.max(1, maxX - minX);
  const graphHeight = Math.max(1, maxY - minY);
  transform.k = clampZoom(Math.min((width - padding) / graphWidth, (height - padding) / graphHeight));
  transform.x = width / 2 - ((minX + maxX) / 2) * transform.k;
  transform.y = height / 2 - ((minY + maxY) / 2) * transform.k;
  setViewportTransform();
}
function selectedTreeIndex() {
  const rows = treeRows();
  const index = rows.findIndex(row => selected && row.node.id === selected.id);
  return { rows, index };
}
function selectTreeOffset(delta) {
  const { rows, index } = selectedTreeIndex();
  if (!rows.length) return;
  const nextIndex = index < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, index + delta));
  selectNode(rows[nextIndex].node, { center: true });
}
function relatedNodes() {
  return relatedRowsForSelected()
    .map(row => nodeById.get(row.id))
    .filter(Boolean);
}
function selectRelatedOffset(delta) {
  const related = relatedNodes();
  if (!related.length) return;
  const currentIndex = selected ? related.findIndex(node => node.id === selected.id) : -1;
  const nextIndex = currentIndex < 0 ? 0 : (currentIndex + delta + related.length) % related.length;
  selectNode(related[nextIndex], { center: true });
}
function isTypingTarget(target) {
  const tag = target && target.tagName ? target.tagName.toLowerCase() : "";
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
}
function handleGraphKeydown(event) {
  if (isTypingTarget(event.target)) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    selectTreeOffset(1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    selectTreeOffset(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    selectRelatedOffset(1);
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    const parent = parentNode(selected);
    if (parent) selectNode(parent, { center: true });
  } else if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    zoomBy(1.18);
  } else if (event.key === "-" || event.key === "_") {
    event.preventDefault();
    zoomBy(0.85);
  } else if (event.key.toLowerCase() === "f") {
    event.preventDefault();
    fitVisibleNodes();
  } else if (event.key.toLowerCase() === "c") {
    event.preventDefault();
    centerNode(selected);
  }
}
function centerNode(node, zoom = 1.15) {
  if (!node) return;
  const rect = svg.getBoundingClientRect();
  const width = rect.width || 900;
  const height = rect.height || 600;
  transform.k = Math.max(0.7, Math.min(2.4, Math.max(transform.k, zoom)));
  transform.x = width / 2 - node.x * transform.k;
  transform.y = height / 2 - node.y * transform.k;
  setViewportTransform();
}
function selectNode(node, options = {}) {
  selectedRunTranscript = null;
  selected = node;
  neighborhoodCache = null;
  ensureParentDirectoriesExpanded(node);
  if (node.kind === "directory" && layerMode.value === "focus") {
    layerMode.value = "directory";
  } else if (node.kind === "file" && layerMode.value === "directory") {
    layerMode.value = "focus";
  }
  renderGraph();
  renderInspector();
  if (options.center) centerNode(node);
  scheduleViewStateSave();
}
function resetTransform() {
  transform = { x: 0, y: 0, k: 1 };
  setViewportTransform();
}
function workspaceWidths() {
  const columns = workspace ? getComputedStyle(workspace).gridTemplateColumns.split(" ") : [];
  return {
    nav: parseFloat(columns[0]) || 280,
    panel: parseFloat(columns[4]) || 430
  };
}
function setWorkspaceWidths(navWidthPx, panelWidthPx) {
  if (!workspace || window.matchMedia("(max-width: 900px)").matches) return;
  const navWidth = Math.max(220, Math.min(560, Number(navWidthPx) || 280));
  const workspaceWidth = workspace.getBoundingClientRect().width || window.innerWidth || 1440;
  const panelMax = Math.max(520, Math.min(1120, workspaceWidth - navWidth - 220));
  const panelWidth = Math.max(320, Math.min(panelMax, Number(panelWidthPx) || 430));
  workspace.style.gridTemplateColumns = `${navWidth}px 7px minmax(0, 1fr) 7px ${panelWidth}px`;
  localStorage.setItem(navWidthKey, String(navWidth));
  localStorage.setItem(panelWidthKey, String(panelWidth));
  updatePanelExpandButton(panelWidth, panelMax);
}
function setNavWidth(widthPx) {
  const widths = workspaceWidths();
  setWorkspaceWidths(widthPx, widths.panel);
}
function setPanelWidth(widthPx) {
  const widths = workspaceWidths();
  setWorkspaceWidths(widths.nav, widthPx);
}
function expandAllDirectories() {
  setDirectoryExpansionMode("all");
  expandedDirs = new Set(defaultExpandedDirectoryIds());
  renderNavigator();
  scheduleViewStateSave();
}
function collapseAllDirectories() {
  setDirectoryExpansionMode("custom");
  expandedDirs = new Set(["dir:."]);
  ensureParentDirectoriesExpanded(selected);
  renderNavigator();
  scheduleViewStateSave();
}
function restorePanelWidth() {
  const savedNav = localStorage.getItem(navWidthKey);
  const savedPanel = localStorage.getItem(panelWidthKey);
  if (savedNav || savedPanel) {
    setWorkspaceWidths(
      savedNav ? Number(savedNav) : workspaceWidths().nav,
      savedPanel ? Number(savedPanel) : workspaceWidths().panel
    );
  } else {
    updatePanelExpandButton(workspaceWidths().panel);
  }
}
function updatePanelExpandButton(panelWidth = null, panelMax = null) {
  if (!panelExpandButton) return;
  const widths = workspaceWidths();
  const currentPanel = panelWidth == null ? widths.panel : Number(panelWidth);
  const currentNav = widths.nav;
  const workspaceWidth = workspace ? workspace.getBoundingClientRect().width : window.innerWidth;
  const maxPanel = panelMax == null
    ? Math.max(520, Math.min(1120, (workspaceWidth || 1440) - currentNav - 220))
    : Number(panelMax);
  const expanded = currentPanel >= maxPanel - 24;
  panelExpandButton.textContent = expanded ? "Restore" : "Expand";
  panelExpandButton.setAttribute("aria-pressed", expanded ? "true" : "false");
}
function togglePanelExpanded() {
  if (!workspace) return;
  const widths = workspaceWidths();
  const workspaceWidth = workspace.getBoundingClientRect().width || window.innerWidth || 1440;
  const maxPanel = Math.max(520, Math.min(1120, workspaceWidth - widths.nav - 220));
  const expanded = widths.panel >= maxPanel - 24;
  setPanelWidth(expanded ? 430 : maxPanel);
}
function bindNavResizer() {
  if (!navResizer || !workspace) return;
  let startX = 0;
  let startWidth = 0;
  const onMove = event => {
    setNavWidth(startWidth + event.clientX - startX);
  };
  const onUp = () => {
    document.body.classList.remove("resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  navResizer.addEventListener("pointerdown", event => {
    event.preventDefault();
    startWidth = workspaceWidths().nav;
    startX = event.clientX;
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}
function bindPanelResizer() {
  if (!panelResizer || !workspace) return;
  let startX = 0;
  let startWidth = 0;
  const onMove = event => {
    const dx = startX - event.clientX;
    setPanelWidth(startWidth + dx);
  };
  const onUp = () => {
    document.body.classList.remove("resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  panelResizer.addEventListener("pointerdown", event => {
    event.preventDefault();
    startWidth = workspaceWidths().panel;
    startX = event.clientX;
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}
if (panelExpandButton) panelExpandButton.addEventListener("click", togglePanelExpanded);
if (navExpandAll) navExpandAll.addEventListener("click", expandAllDirectories);
if (navCollapseAll) navCollapseAll.addEventListener("click", collapseAllDirectories);
function sidecarUrl() {
  const current = new URL(window.location.href);
  if (current.pathname.endsWith(".html")) {
    current.pathname = current.pathname.replace(/\.html$/, ".json");
  } else {
    current.pathname = current.pathname.replace(/\/?$/, "/repo-graph.json");
  }
  current.searchParams.set("t", String(Date.now()));
  return current.toString();
}
async function refreshGraphData(options = {}) {
  if (refreshing) return;
  const silent = !!options.silent;
  refreshing = true;
  const original = refreshGraph.textContent;
  if (!silent) refreshGraph.textContent = "Refreshing";
  try {
    const response = await fetchGraphGet(sidecarUrl(), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const nextData = await response.json();
    const nextSignature = graphDataSignature(nextData);
    if (silent && nextSignature === lastGraphSignature) {
      return;
    }
    hydrateData(nextData, { preserveSelection: true });
    syncAgentProviderControls();
    seedMissingPositions();
    tickSimulation(silent ? 45 : 120);
    renderGraph();
    renderInspector();
    if (!silent) {
      refreshGraph.textContent = "Updated";
      setTimeout(() => { refreshGraph.textContent = original; }, 1000);
    }
  } catch (_err) {
    // file:// pages often cannot fetch sibling files. Reloading still picks
    // up the latest HTML if a watcher or agent regenerated it.
    if (!liveRefresh.checked && !silent) window.location.reload();
    if (!silent) refreshGraph.textContent = original;
  } finally {
    refreshing = false;
  }
}
function setLiveRefresh(enabled) {
  if (!enabled) {
    closeEventStream();
    updateAgentHeader();
    return;
  }
  bindEventStream();
  updateAgentHeader();
}
function closeEventStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  liveConnected = false;
}
function bindEventStream() {
  if (!canPostToGraphServer() || !window.EventSource) return;
  try {
    closeEventStream();
    eventSource = new EventSource((data.live && data.live.events_path) || "/events");
    eventSource.onopen = () => {
      liveConnected = true;
      refreshAgentProviders({ force: true });
      updateAgentHeader();
    };
    eventSource.onerror = () => {
      liveConnected = false;
      updateAgentHeader();
    };
    eventSource.addEventListener("agent", event => {
      try {
        handleAgentSnapshot(JSON.parse(event.data || "{}"));
      } catch (_err) {
        // Ignore malformed event data; the next full graph refresh can recover.
      }
    });
    eventSource.addEventListener("graph", () => {
      if (document.visibilityState === "visible") refreshGraphData({ silent: true });
    });
    eventSource.addEventListener("perf:tick", event => {
      try {
        handlePerfTick(JSON.parse(event.data || "{}"));
      } catch (_err) {
        // Perf ticks are advisory; debug polling remains the fallback.
      }
    });
  } catch (_err) {
    closeEventStream();
    // The static HTML still works through manual refresh.
  }
}
svg.addEventListener("pointerdown", event => {
  if (event.button !== 0) return;
  if (event.target.closest && event.target.closest(".node")) return;
  panState = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    transformX: transform.x,
    transformY: transform.y
  };
  svg.setPointerCapture(event.pointerId);
  document.body.classList.add("panning");
});
svg.addEventListener("pointermove", event => {
  if (!panState || panState.pointerId !== event.pointerId) return;
  transform.x = panState.transformX + event.clientX - panState.startX;
  transform.y = panState.transformY + event.clientY - panState.startY;
  setViewportTransform();
});
function endPan(event) {
  if (!panState || panState.pointerId !== event.pointerId) return;
  panState = null;
  document.body.classList.remove("panning");
  try {
    svg.releasePointerCapture(event.pointerId);
  } catch (_err) {
    // Pointer capture may already be released by the browser.
  }
}
svg.addEventListener("pointerup", endPan);
svg.addEventListener("pointercancel", endPan);
svg.addEventListener("wheel", event => {
  event.preventDefault();
  const factor = event.deltaY < 0 ? 1.08 : 0.92;
  zoomAt(event.clientX, event.clientY, factor);
  transform.x -= event.deltaX;
  setViewportTransform();
}, { passive: false });
searchInput.addEventListener("input", () => {
  renderGraph();
  scheduleServerSearch();
  scheduleViewStateSave();
});
layerMode.addEventListener("change", () => {
  neighborhoodCache = null;
  tickSimulation(["communities", "roles"].includes(layerMode.value) ? 100 : 45);
  renderGraph();
  renderInspector();
  if (["structure", "flow", "layered"].includes(layerMode.value)) fitVisibleNodes();
  scheduleViewStateSave();
});
careFilter.addEventListener("change", () => {
  renderGraph();
  scheduleViewStateSave();
});
showDirs.addEventListener("change", () => {
  renderGraph();
  scheduleViewStateSave();
});
showRelations.addEventListener("change", () => {
  renderGraph();
  scheduleViewStateSave();
});
refreshGraph.addEventListener("click", refreshGraphData);
liveRefresh.addEventListener("change", () => {
  setLiveRefresh(liveRefresh.checked);
  scheduleViewStateSave();
});
if (zoomOutButton) zoomOutButton.addEventListener("click", () => zoomBy(0.82));
if (zoomInButton) zoomInButton.addEventListener("click", () => zoomBy(1.22));
if (fitViewButton) fitViewButton.addEventListener("click", fitVisibleNodes);
if (focusViewButton) focusViewButton.addEventListener("click", () => centerNode(selected));
function updateNeighborhoodStatus() {
  if (neighborhoodStatus) neighborhoodStatus.textContent = `${neighborhoodDepth} hop${neighborhoodDepth === 1 ? "" : "s"}`;
  if (collapseNeighborhoodButton) collapseNeighborhoodButton.disabled = neighborhoodDepth <= 1;
  if (expandNeighborhoodButton) expandNeighborhoodButton.disabled = neighborhoodDepth >= 3;
}
function setNeighborhoodDepth(nextDepth) {
  neighborhoodDepth = Math.max(1, Math.min(3, Number(nextDepth) || 1));
  neighborhoodCache = null;
  updateNeighborhoodStatus();
  if (layerMode.value === "focus" || layerMode.value === "layered") {
    tickSimulation(20);
    renderGraph();
    fitVisibleNodes();
  }
  scheduleViewStateSave();
}
if (collapseNeighborhoodButton) collapseNeighborhoodButton.addEventListener("click", () => setNeighborhoodDepth(neighborhoodDepth - 1));
if (expandNeighborhoodButton) expandNeighborhoodButton.addEventListener("click", () => {
  if (layerMode.value !== "layered") layerMode.value = "layered";
  setNeighborhoodDepth(neighborhoodDepth + 1);
});
if (navParent) {
  navParent.addEventListener("click", () => {
    const parent = parentNode(selected);
    if (parent) selectNode(parent, { center: true });
  });
}
if (navCenter) {
  navCenter.addEventListener("click", () => {
    if (selected) centerNode(selected);
  });
}
resetView.addEventListener("click", () => {
  searchInput.value = "";
  layerMode.value = "overview";
  neighborhoodDepth = 1;
  neighborhoodCache = null;
  searchResults = { query: "", status: "idle", files: [], transcripts: [], counts: {} };
  updateNeighborhoodStatus();
  careFilter.value = "all";
  showDirs.checked = true;
  showRelations.checked = true;
  viewState = {
    ...viewState,
    directoryExpansionDefaultVersion: DIRECTORY_EXPANSION_DEFAULT_VERSION,
    directoryExpansionMode: "all"
  };
  expandedDirs = new Set(defaultExpandedDirectoryIds());
  ensureParentDirectoriesExpanded(selected);
  resetTransform();
  renderGraph();
  scheduleViewStateSave();
});
window.addEventListener("keydown", handleGraphKeydown);
tabSummary.addEventListener("click", () => { activeTab = "summary"; renderInspector(); });
if (tabChat) tabChat.addEventListener("click", () => { activeTab = "chat"; renderInspector(); });
tabEdits.addEventListener("click", () => { activeTab = "edits"; renderInspector(); });
tabNotes.addEventListener("click", () => { activeTab = "notes"; renderInspector(); });
tabCode.addEventListener("click", () => { activeTab = "code"; renderInspector(); });
if (tabDebug) tabDebug.addEventListener("click", () => { activeTab = "debug"; renderInspector(); });

syncGraphTokenFromUrl();
applyViewStateControls();
updateNeighborhoodStatus();
if (canPostToGraphServer() && typeof viewState.liveRefresh !== "boolean") {
  liveRefresh.checked = true;
}
hydrateData(data);
restorePanelWidth();
bindNavResizer();
bindPanelResizer();
initPositions();
tickSimulation(220);
renderGraph();
if (viewState.transform && Number.isFinite(viewState.transform.k)) {
  transform = {
    x: Number(viewState.transform.x) || 0,
    y: Number(viewState.transform.y) || 0,
    k: clampZoom(Number(viewState.transform.k) || 1)
  };
  setViewportTransform();
} else {
  fitVisibleNodes();
}
renderInspector();
if (liveRefresh.checked) setLiveRefresh(true);
"""

__all__ = ["GRAPH_SCRIPT_CONTROLS"]
