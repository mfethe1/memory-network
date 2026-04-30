"""Navigator JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_NAVIGATOR = r"""function renderNavigator() {
  if (!recentFilesEl || !treeViewEl) return;
  const recentFiles = (data.summary && data.summary.recent_files) || [];
  const selectedLabel = selected ? selected.path : "repo";
  navigatorSummary.textContent = `${data.summary.file_count} files · ${recentFiles.length} recent · ${selectedLabel}`;
  renderBreadcrumbs();
  renderActiveFiles();
  renderFileClaims();
  renderAgentRuns();
  renderTaskBoard();
  renderSearchResults();
  renderRelatedFiles();
  recentFilesEl.innerHTML = recentFiles.length
    ? recentFiles.map(item => navButtonHtml({
        id: fileNodeId(item.file_path),
        icon: String(item.rank),
        label: item.file_path,
        badge: `${item.edit_count} edits`,
        recent: true
      })).join("")
    : `<div class="empty">No recent edits indexed.</div>`;

  const rows = treeRows();
  treeViewEl.innerHTML = rows.length
    ? rows.map(({ node, depth, searching }) => {
        const expanded = expandedDirs.has(node.id);
        const icon = node.kind === "directory" ? (expanded && !searching ? "▾" : "▸") : "·";
        const badge = node.kind === "directory"
          ? `${(node.metrics && node.metrics.file_count) || 0} files`
          : navFileBadge(node);
        return navButtonHtml({
          id: node.id,
          icon,
          label: node.path === "." ? "repo" : (searching ? node.path : node.label),
          badge,
          recent: isRecentNode(node),
          activeWork: node.active_work,
          indent: Math.min(64, depth * 12),
          tree: true,
          title: node.path
        });
      }).join("")
    : `<div class="empty">No matching files.</div>`;

  document.querySelectorAll("[data-nav-node]").forEach(button => {
    button.addEventListener("click", () => {
      const node = nodeById.get(button.dataset.navNode);
      if (!node || button.disabled) return;
      if (button.dataset.navTree === "true" && node.kind === "directory" && !searchInput.value.trim()) {
        setDirectoryExpansionMode("custom");
        if (selected && selected.id === node.id && expandedDirs.has(node.id) && node.id !== "dir:.") {
          expandedDirs.delete(node.id);
        } else {
          expandedDirs.add(node.id);
        }
      }
      selectNode(node, { center: true });
    });
  });
  document.querySelectorAll("[data-cancel-run]").forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      if (!button.disabled) cancelAgentRun(button.dataset.cancelRun, button);
    });
  });
  document.querySelectorAll("[data-archive-run]").forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      if (!button.disabled) archiveAgentRun(button.dataset.archiveRun, button);
    });
  });
  document.querySelectorAll("[data-run-details]").forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      if (!button.disabled) showRunTranscript(button.dataset.runDetails, button);
    });
  });
  document.querySelectorAll("[data-search-run]").forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      if (!button.disabled) showRunTranscript(button.dataset.searchRun, button);
    });
  });
}
function renderBreadcrumbs() {
  if (!breadcrumbViewEl) return;
  const crumbs = breadcrumbNodes(selected);
  breadcrumbViewEl.innerHTML = crumbs.length
    ? crumbs.map(node => `
        <button class="crumb${selected && selected.id === node.id ? " active" : ""}" data-nav-node="${escapeHtml(node.id)}" type="button">
          ${escapeHtml(node.path === "." ? "repo" : node.label)}
        </button>
      `).join("")
    : `<span class="empty">No selection</span>`;
}
function breadcrumbNodes(node) {
  if (!node) return [nodeById.get("dir:.")].filter(Boolean);
  const path = node.kind === "file" ? node.directory : node.path;
  const pieces = path === "." ? [] : path.split("/");
  const crumbs = [nodeById.get("dir:.")].filter(Boolean);
  let current = [];
  pieces.forEach(part => {
    current.push(part);
    const dirNode = nodeById.get(directoryNodeId(current.join("/")));
    if (dirNode) crumbs.push(dirNode);
  });
  if (node.kind === "file") crumbs.push(node);
  return crumbs;
}
function renderActiveFiles() {
  if (!activeFilesEl) return;
  const activeFiles = ((data.agent && data.agent.active_files) || []).slice(0, 8);
  activeFilesEl.innerHTML = activeFiles.length
    ? activeFiles.map(path => navButtonHtml({
        id: fileNodeId(path),
        icon: "A",
        label: path,
        badge: nodeById.has(fileNodeId(path)) ? "active" : "unindexed",
        activeWork: true,
        missing: !nodeById.has(fileNodeId(path))
      })).join("")
    : `<div class="empty">No active files reported.</div>`;
}
function renderFileClaims() {
  if (!fileClaimsEl) return;
  const claims = ((data.agent && data.agent.active_claims) || []).slice(0, 10);
  fileClaimsEl.innerHTML = claims.length
    ? claims.map(claim => navButtonHtml({
        id: fileNodeId(claim.file_path),
        icon: String(claim.mode || "?").slice(0, 1).toUpperCase(),
        label: claim.file_path,
        badge: `${claim.agent_name || "Agent"} · ${claim.mode || "claim"}`,
        activeWork: true,
        missing: !nodeById.has(fileNodeId(claim.file_path)),
        title: `${claim.agent_name || "Agent"} ${claim.mode || "claim"} claim: ${claim.reason || claim.file_path}`
      })).join("")
    : `<div class="empty">No active file claims.</div>`;
}
function renderAgentRuns() {
  if (!agentRunsEl) return;
  const allRuns = uniqueRuns(
    ((data.agent && data.agent.active_runs) || []).concat(
      (data.agent && data.agent.recent_runs) || []
    )
  ).filter(run => !run.archived_at);
  const runningRuns = allRuns.filter(run => !isTerminalStatus(run.status)).slice(0, 8);
  const pastRuns = allRuns.filter(run => isTerminalStatus(run.status)).slice(0, 8);
  const runningHtml = runningRuns.length
    ? runningRuns.map(run => runRowHtml(run)).join("")
    : `<div class="empty">No queued or active runs.</div>`;
  const pastHtml = pastRuns.length
    ? `
      <details class="past-runs">
        <summary>Past runs (${pastRuns.length})</summary>
        <div class="past-run-list">${pastRuns.map(run => runRowHtml(run)).join("")}</div>
      </details>
    `
    : "";
  agentRunsEl.innerHTML = runningHtml + pastHtml;
}
function renderTaskBoard() {
  if (!taskBoardEl) return;
  const board = data.agent && data.agent.kanban;
  const columns = board && board.columns ? board.columns : {};
  const orchestrator = (data.agent && data.agent.orchestrator) || {};
  const reviewRuns = orchestrator.review_runs || [];
  const reviewBanner = reviewRuns.length
    ? `
      <button class="review-queue-card" data-run-details="${escapeHtml(reviewRuns[0].run_id)}" type="button" title="${escapeHtml(reviewRuns[0].prompt || reviewRuns[0].run_id)}">
        <strong>${escapeHtml(reviewRuns.length)} awaiting review</strong>
        <span>${escapeHtml((reviewRuns[0].agent_name || "Agent") + " · " + (reviewRuns[0].prompt || reviewRuns[0].run_id))}</span>
      </button>
    `
    : "";
  const ordered = ["blocked", "ready", "active", "review", "done"];
  taskBoardEl.innerHTML = reviewBanner + ordered.map(name => {
    const column = columns[name] || { title: name, runs: [] };
    const runs = (column.runs || []).slice(0, 4);
    const title = column.title || name;
    const rows = runs.length
      ? runs.map(run => taskBoardRunHtml(run, name)).join("")
      : `<div class="task-card empty">Empty</div>`;
    return `
      <div class="task-column ${escapeHtml(name)}">
        <div class="task-column-head">
          <span>${escapeHtml(title)}</span>
          <strong>${runs.length}</strong>
        </div>
        ${rows}
      </div>
    `;
  }).join("");
}
function taskBoardRunHtml(run, column) {
  const blockers = (run.blocked_by || []).filter(item => String(item.status || "").toLowerCase() === "active");
  const health = run.run_health || {};
  const label = run.prompt || run.run_id;
  const badge = blockers.length
    ? `${blockers.length} blocker${blockers.length === 1 ? "" : "s"}`
    : `${run.agent_name || "Agent"} · ${health.health || run.status || column}`;
  return `
    <button class="task-card ${escapeHtml(health.health || "")}" data-run-details="${escapeHtml(run.run_id)}" type="button" title="${escapeHtml(label)}">
      <span>${escapeHtml(label)}</span>
      <em>${escapeHtml(badge)}</em>
    </button>
  `;
}
function renderSearchResults() {
  if (!searchResultsEl) return;
  const query = searchInput.value.trim();
  if (!query) {
    searchResultsEl.innerHTML = `<div class="empty">Type above to search files and transcripts.</div>`;
    return;
  }
  if (searchResults.status === "loading" && searchResults.query === query) {
    searchResultsEl.innerHTML = `<div class="empty">Searching...</div>`;
    return;
  }
  if (searchResults.status === "error" && searchResults.query === query) {
    searchResultsEl.innerHTML = `<div class="empty">${escapeHtml(searchResults.error || "Search failed")}</div>`;
    return;
  }
  const fileRows = (searchResults.files || []).slice(0, 6).map(result => {
    const path = result.file_path || "";
    const line = result.start_line ? `:${result.start_line}` : "";
    return navButtonHtml({
      id: fileNodeId(path),
      icon: result.kind === "file_path" ? "F" : "C",
      label: `${path}${line}`,
      badge: result.symbol_name || result.chunk_type || "file",
      connected: true,
      missing: !nodeById.has(fileNodeId(path)),
      title: result.snippet || path
    });
  });
  const transcriptRows = (searchResults.transcripts || []).slice(0, 5).map(result => `
    <button class="nav-row connected" data-search-run="${escapeHtml(result.run_id)}" title="${escapeHtml(result.snippet || result.prompt || result.run_id)}" type="button">
      <span class="nav-icon">T</span>
      <span class="nav-name">${escapeHtml(result.message || result.prompt || result.run_id)}</span>
      <span class="nav-badge">${escapeHtml(result.agent_name || "Agent")} · ${escapeHtml(result.event_type || "run")}</span>
    </button>
  `);
  const rows = fileRows.concat(transcriptRows);
  searchResultsEl.innerHTML = rows.length
    ? rows.join("")
    : `<div class="empty">No server search results.</div>`;
}
function uniqueRuns(runs) {
  const out = [];
  const seen = new Set();
  runs.forEach(run => {
    if (!run || !run.run_id || seen.has(run.run_id)) return;
    seen.add(run.run_id);
    out.push(run);
  });
  return out;
}
function isTerminalStatus(status) {
  return terminalRunStatuses.has(String(status || "").toLowerCase());
}
function activeRunIds() {
  return new Set(((data.agent && data.agent.active_runs) || [])
    .map(run => run && run.run_id)
    .filter(Boolean));
}
function runDisplayStatus(run) {
  const status = String((run && run.status) || "working").toLowerCase();
  if (isTerminalStatus(status) || !run || !run.run_id) return status;
  return activeRunIds().has(run.run_id) ? status : `stale ${status}`;
}
function runRowHtml(run) {
  const status = run.status || "working";
  const displayStatus = runDisplayStatus(run);
  const label = run.prompt || run.run_id;
  const cancelable = !isTerminalStatus(status);
  const active = selectedRunTranscript && selectedRunTranscript.run && selectedRunTranscript.run.run_id === run.run_id ? " active" : "";
  return `
    <div class="run-row${active}" title="${escapeHtml(`${run.agent_name || "Agent"} ${displayStatus}: ${label}`)}">
      <button class="run-select" data-run-details="${escapeHtml(run.run_id)}" type="button" title="Open agent terminal stream">
        <span class="nav-icon">${escapeHtml(displayStatus.slice(0, 1).toUpperCase())}</span>
        <span class="nav-name">${escapeHtml(label)}</span>
        <span class="nav-badge">${escapeHtml(run.agent_name || "Agent")} · ${escapeHtml(displayStatus)}</span>
      </button>
      <button class="run-detail" data-run-details="${escapeHtml(run.run_id)}" type="button" title="View run stream">Stream</button>
      <button class="run-cancel" data-cancel-run="${escapeHtml(run.run_id)}" type="button"${cancelable ? "" : " disabled aria-disabled=\"true\""} title="Cancel run">Cancel</button>
      <button class="run-archive" data-archive-run="${escapeHtml(run.run_id)}" type="button" title="Archive run from the sidebar">Archive</button>
    </div>
  `;
}
function runTargetNodeId(run) {
  const selectedNodes = Array.isArray(run.selected_nodes) ? run.selected_nodes : [];
  for (const id of selectedNodes) {
    if (nodeById.has(id)) return id;
  }
  const activeFiles = Array.isArray(run.active_files) ? run.active_files : [];
  for (const path of activeFiles) {
    const id = fileNodeId(path);
    if (nodeById.has(id)) return id;
  }
  const metadata = run.metadata || {};
  const selectedPaths = Array.isArray(metadata.selected_paths) ? metadata.selected_paths : [];
  for (const path of selectedPaths) {
    const id = fileNodeId(path);
    if (nodeById.has(id)) return id;
  }
  return null;
}
function renderRelatedFiles() {
  if (!relatedFilesEl) return;
  const rows = relatedRowsForSelected();
  relatedFilesEl.innerHTML = rows.length
    ? rows.map(row => navButtonHtml({
        id: row.id,
        icon: row.icon,
        label: row.label,
        badge: row.badge,
        connected: true,
        missing: !nodeById.has(row.id)
      })).join("")
    : `<div class="empty">Select a file or directory to see nearby nodes.</div>`;
}
function relatedRowsForSelected() {
  if (!selected || !selected.metrics) return [];
  if (selected.kind === "directory") {
    const active = selected.metrics.active_files || [];
    const recent = selected.metrics.recent_files || [];
    return uniquePaths(active.concat(recent)).slice(0, 10).map(path => ({
      id: fileNodeId(path),
      icon: active.includes(path) ? "A" : "#",
      label: path,
      badge: active.includes(path) ? "active" : "recent"
    }));
  }
  const incoming = (selected.metrics.incoming_files || []).map(path => ({
    id: fileNodeId(path),
    icon: "in",
    label: path,
    badge: "incoming"
  }));
  const outgoing = (selected.metrics.outgoing_files || []).map(path => ({
    id: fileNodeId(path),
    icon: "out",
    label: path,
    badge: "outgoing"
  }));
  return incoming.concat(outgoing).slice(0, 12);
}
function uniquePaths(paths) {
  const seen = new Set();
  const out = [];
  paths.forEach(path => {
    if (path && !seen.has(path)) {
      seen.add(path);
      out.push(path);
    }
  });
  return out;
}
function navFileBadge(node) {
  if (node.active_work) return "active";
  if (node.metrics && node.metrics.recent_edit_rank) return `#${node.metrics.recent_edit_rank}`;
  return node.care_level || "";
}
function navButtonHtml({ id, icon, label, badge = "", recent = false, activeWork = false, connected = false, missing = false, indent = 0, tree = false, title = null }) {
  const active = selected && selected.id === id ? " active" : "";
  const safeIndent = Math.max(0, Number(indent) || 0);
  const classes = [
    "nav-row",
    tree ? "tree-row" : "",
    recent ? "recent" : "",
    activeWork ? "active-work" : "",
    connected ? "connected" : "",
    missing ? "missing" : "",
    active.trim()
  ].filter(Boolean).join(" ");
  const disabled = missing ? " disabled aria-disabled=\"true\"" : "";
  const treeAttr = tree ? " data-nav-tree=\"true\"" : "";
  const safeTitle = escapeHtml(title || label);
  return `
    <button class="${classes}" data-nav-node="${escapeHtml(id)}"${treeAttr} style="--nav-indent:${safeIndent}px" title="${safeTitle}" type="button"${disabled}>
      <span class="nav-icon">${escapeHtml(icon)}</span>
      <span class="nav-name">${escapeHtml(label)}</span>
      <span class="nav-badge">${escapeHtml(badge)}</span>
    </button>
  `;
}
function updateVisibility() {
  const visibleIds = computeVisibility();
  let visibleEdgeCount = 0;
  document.querySelectorAll(".node").forEach(el => {
    const node = nodeById.get(el.dataset.id);
    el.classList.toggle("active", !!(node && node.active_work));
    el.classList.toggle("recent", !!(node && isRecentNode(node)));
    el.classList.toggle("trail", !!(node && isRecentNode(node)));
    el.classList.toggle("selected", !!(node && selected && selected.id === node.id));
    el.classList.toggle("dim", !node || !node.visible);
  });
  document.querySelectorAll(".edge").forEach(el => {
    const isRelation = el.dataset.kind !== "contains";
    const hiddenKind = isRelation && !showRelations.checked;
    const hidden = hiddenKind || !visibleIds.has(el.dataset.source) || !visibleIds.has(el.dataset.target);
    el.classList.toggle("dim", hidden);
    el.style.display = hiddenKind ? "none" : "";
    if (!hidden) visibleEdgeCount += 1;
  });
  clientMetrics.visible_node_count = visibleIds.size;
  clientMetrics.visible_edge_count = visibleEdgeCount;
}
"""

__all__ = ["GRAPH_SCRIPT_NAVIGATOR"]
