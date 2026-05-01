"""State JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_STATE = r"""
let data = JSON.parse(document.getElementById("graph-data").textContent);
const svg = document.getElementById("graph");
const viewport = document.getElementById("viewport");
const edgesLayer = document.getElementById("edges");
const nodesLayer = document.getElementById("nodes");
const workspace = document.querySelector(".workspace");
const navResizer = document.getElementById("nav-resizer");
const panelResizer = document.getElementById("panel-resizer");
const navigatorSummary = document.getElementById("navigator-summary");
const breadcrumbViewEl = document.getElementById("breadcrumb-view");
const activeFilesEl = document.getElementById("active-files");
const fileClaimsEl = document.getElementById("file-claims");
const agentRunsEl = document.getElementById("agent-runs");
const taskBoardEl = document.getElementById("task-board");
const searchResultsEl = document.getElementById("search-results");
const relatedFilesEl = document.getElementById("related-files");
const recentFilesEl = document.getElementById("recent-files");
const treeViewEl = document.getElementById("tree-view");
const navParent = document.getElementById("nav-parent");
const navCenter = document.getElementById("nav-center");
const navExpandAll = document.getElementById("nav-expand-all");
const navCollapseAll = document.getElementById("nav-collapse-all");
const searchInput = document.getElementById("search");
const layerMode = document.getElementById("layer-mode");
const careFilter = document.getElementById("care-filter");
const showDirs = document.getElementById("show-dirs");
const showRelations = document.getElementById("show-relations");
const liveRefresh = document.getElementById("live-refresh");
const refreshGraph = document.getElementById("refresh-graph");
const resetView = document.getElementById("reset-view");
const zoomOutButton = document.getElementById("zoom-out");
const zoomInButton = document.getElementById("zoom-in");
const fitViewButton = document.getElementById("fit-view");
const focusViewButton = document.getElementById("focus-view");
const expandNeighborhoodButton = document.getElementById("expand-neighborhood");
const collapseNeighborhoodButton = document.getElementById("collapse-neighborhood");
const neighborhoodStatus = document.getElementById("neighborhood-status");
const nodeKind = document.getElementById("node-kind");
const nodeTitle = document.getElementById("node-title");
const nodeMeta = document.getElementById("node-meta");
const panelExpandButton = document.getElementById("panel-expand");
const panelBody = document.getElementById("panel-body");
const tabSummary = document.getElementById("tab-summary");
const tabChat = document.getElementById("tab-chat");
const tabEdits = document.getElementById("tab-edits");
const tabNotes = document.getElementById("tab-notes");
const tabCode = document.getElementById("tab-code");
const tabDebug = document.getElementById("tab-debug");
const repoSubtitle = document.getElementById("repo-subtitle");
const agentName = document.getElementById("agent-name");
const agentFiles = document.getElementById("agent-files");

const colors = {
  critical: "#c84040",
  high: "#c87826",
  medium: "#2f7b72",
  low: "#697580",
  directory: "#3e5872"
};
const edgeColors = {
  contains: "#aab2bb",
  imports: "#7a699c",
  calls: "#536f85",
  inherits: "#a56b45",
  implements: "#5d7c68",
  overrides: "#8c5d68",
  agent_derived: "#b49ad8"
};
const communityColors = [
  "#5dd4c6",
  "#f2a65a",
  "#8fb7ff",
  "#e38ec5",
  "#9fd07f",
  "#d0b36f",
  "#79a7a8",
  "#b49ad8"
];
const terminalRunStatuses = new Set(["completed", "failed", "cancelled", "canceled"]);
const streamEventTypes = new Set(["task", "status", "tool", "read", "edit", "test", "navigate", "decision"]);
let nodes = [];
let nodeById = new Map();
let edges = [];
let selected = null;
let selectedRunTranscript = null;
let activeTab = "summary";
let transform = { x: 0, y: 0, k: 1 };
const notesKey = `code_index_graph_notes:${data.root}`;
const navWidthKey = `code_index_graph_nav_width:${data.root}`;
const panelWidthKey = `code_index_graph_panel_width:${data.root}`;
const viewStateKey = `code_index_graph_view:${data.root}`;
const graphTokenKey = `code_index_graph_token:${data.root}`;
const DIRECTORY_EXPANSION_DEFAULT_VERSION = 2;
let notes = loadNotes();
let eventSource = null;
let liveConnected = false;
let refreshing = false;
let viewState = loadViewState();
let expandedDirs = new Set(Array.isArray(viewState.expandedDirs) ? viewState.expandedDirs : ["dir:."]);
let neighborhoodDepth = Math.max(1, Math.min(3, Number(viewState.neighborhoodDepth || 1)));
let graphAdjacencyCache = null;
let neighborhoodCache = null;
let searchResults = {
  query: "",
  status: "idle",
  files: [],
  transcripts: [],
  counts: {}
};
let searchTimer = null;
let panState = null;
let lastGraphSignature = "";
let viewSaveTimer = null;
let terminalRenderFrame = null;
let terminalForceScroll = false;
let terminalLastSignature = "";
let debugSnapshot = null;
let debugPerfTick = null;
let debugFetchError = "";
let agentProvidersRefreshPromise = null;
let agentProvidersLastFetchMs = 0;
let agentProviderConfigSignature = "";
let agentGraphRenderFrame = null;
let clientMetrics = {
  hydrate_count: 0,
  render_count: 0,
  last_hydrate_ms: 0,
  last_render_ms: 0,
  last_debug_fetch_ms: 0,
  payload_chars: document.getElementById("graph-data").textContent.length,
  node_count: 0,
  edge_count: 0,
  visible_node_count: 0,
  visible_edge_count: 0
};

function updateAgentHeader() {
  const agent = data.agent || {};
  const orchestrator = agent.orchestrator || {};
  const activeFiles = agent.active_files || data.focus_paths || [];
  const activeClaims = agent.active_claims || [];
  const reviewRuns = orchestrator.review_runs || [];
  const activeAgents = (agent.active_agents && agent.active_agents.length)
    ? agent.active_agents.join(", ")
    : (agent.name || "Agent");
  const activeRuns = (agent.active_runs || []).concat(agent.recent_runs || []);
  const status = agent.status || (activeFiles.length ? "working" : "idle");
  const liveStatus = liveRefresh.checked
    ? (liveConnected ? "live" : "connecting")
    : "manual";
  const reviewText = reviewRuns.length ? ` · ${reviewRuns.length} review` : "";
  agentName.textContent = `${activeAgents} · ${status}${reviewText} · ${liveStatus}`;
  agentFiles.textContent = activeFiles.length
    ? activeFiles.join(", ")
    : (reviewRuns.length ? `${reviewRuns.length} run(s) waiting for review` : (activeClaims.length ? `${activeClaims.length} active file claim(s)` : (activeRuns.length ? `${activeRuns.length} tracked run(s)` : "No active file")));
}

function hydrateData(nextData, options = {}) {
  const hydrateStarted = performance.now();
  const priorSelectedId = (selected && selected.id) || (!options.preserveSelection && viewState.selectedId);
  const priorPositions = new Map(nodes.map(node => [node.id, {
    x: node.x,
    y: node.y,
    vx: node.vx,
    vy: node.vy,
    visible: node.visible
  }]));
  data = nextData;
  lastGraphSignature = graphDataSignature(data);
  agentProviderConfigSignature = agentProvidersSignatureFromLive((data && data.live) || {});
  mergeServerNotes();
  repoSubtitle.textContent =
    `${data.summary.file_count} files, ${data.summary.relation_edge_count} relation edges, generated ${data.generated_at}`;
  updateAgentHeader();
  nodes = data.nodes.map((node, index) => {
    const prior = priorPositions.get(node.id);
    return {
      ...node,
      index,
      x: prior ? prior.x : 0,
      y: prior ? prior.y : 0,
      vx: prior ? prior.vx : 0,
      vy: prior ? prior.vy : 0,
      visible: prior ? prior.visible : true
    };
  });
  nodeById = new Map(nodes.map(n => [n.id, n]));
  const appliedDirectoryExpansionDefault = applyDirectoryExpansionDefaultIfNeeded();
  edges = data.edges
    .map(edge => ({ ...edge, sourceNode: nodeById.get(edge.source), targetNode: nodeById.get(edge.target) }))
    .filter(edge => edge.sourceNode && edge.targetNode);
  graphAdjacencyCache = null;
  neighborhoodCache = null;
  selected =
    (options.preserveSelection && priorSelectedId && nodeById.get(priorSelectedId)) ||
    nodes.find(n => n.active_work) ||
    nodes.find(n => n.kind === "directory" && n.path === ".") ||
    nodes[0] ||
    null;
  pruneExpandedDirs();
  ensureParentDirectoriesExpanded(selected);
  renderNavigator();
  if (appliedDirectoryExpansionDefault) scheduleViewStateSave();
  clientMetrics.hydrate_count += 1;
  clientMetrics.last_hydrate_ms = Math.round((performance.now() - hydrateStarted) * 100) / 100;
  clientMetrics.payload_chars = JSON.stringify(data).length;
  clientMetrics.node_count = nodes.length;
  clientMetrics.edge_count = edges.length;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function graphDataSignature(payload) {
  const summary = payload.summary || {};
  const activity = payload.activity || {};
  const live = payload.live || {};
  const recentEvent = (activity.agent_events || [])[0] || {};
  const recentEdit = (summary.recent_edits || [])[0] || {};
  const activeRuns = (((payload.agent || {}).active_runs || []).concat((payload.agent || {}).recent_runs || [])).map(run => `${run.run_id}:${run.status}:${run.updated_at}`).join("|");
  const activeClaims = (((payload.agent || {}).active_claims || []).map(claim =>
    `${claim.claim_id || ""}:${claim.run_id || ""}:${claim.file_path || ""}:${claim.status || ""}:${claim.updated_at || ""}`
  )).join("|");
  const dynamicEdges = ((payload.edges || [])
    .filter(edge => edge && edge.kind === "agent_derived")
    .map(edge => `${edge.source || ""}:${edge.target || ""}:${edge.weight || ""}`)
    .sort()
  ).join("|");
  const notes = Object.values((payload.notes && payload.notes.by_node) || {})
    .map(note => `${note.node_id || note.path}:${note.updated_at || ""}`)
    .sort()
    .join("|");
  return JSON.stringify({
    nodes: summary.node_count,
    edges: summary.edge_count,
    files: summary.file_count,
    recentEvent: recentEvent.event_pk,
    recentEdit: `${recentEdit.file_path || ""}:${recentEdit.timestamp || ""}:${recentEdit.change_type || ""}`,
    activeRuns,
    activeClaims,
    dynamicEdges,
    agentProviders: agentProvidersSignatureFromLive(live),
    notes
  });
}
function loadViewState() {
  try {
    const state = JSON.parse(localStorage.getItem(viewStateKey) || "{}");
    return state && typeof state === "object" ? state : {};
  } catch (_err) {
    return {};
  }
}
function applyViewStateControls() {
  if (!viewState || !Object.keys(viewState).length) return;
  if (viewState.layerMode && [...layerMode.options].some(option => option.value === viewState.layerMode)) {
    layerMode.value = viewState.layerMode;
  }
  if (viewState.careFilter && [...careFilter.options].some(option => option.value === viewState.careFilter)) {
    careFilter.value = viewState.careFilter;
  }
  if (typeof viewState.showDirs === "boolean") showDirs.checked = viewState.showDirs;
  if (typeof viewState.showRelations === "boolean") showRelations.checked = viewState.showRelations;
  if (typeof viewState.liveRefresh === "boolean") liveRefresh.checked = viewState.liveRefresh;
  if (typeof viewState.search === "string") searchInput.value = viewState.search;
}
function applyDirectoryExpansionDefaultIfNeeded() {
  const savedVersion = Number(viewState.directoryExpansionDefaultVersion || 0);
  const savedMode = viewState.directoryExpansionMode === "custom" ? "custom" : "all";
  if (savedVersion >= DIRECTORY_EXPANSION_DEFAULT_VERSION && savedMode === "custom") {
    return false;
  }
  const nextExpandedDirs = new Set(defaultExpandedDirectoryIds());
  const changed = (
    savedVersion < DIRECTORY_EXPANSION_DEFAULT_VERSION ||
    savedMode !== "all" ||
    !sameIdSet(expandedDirs, nextExpandedDirs)
  );
  if (!changed) return false;
  expandedDirs = nextExpandedDirs;
  viewState = {
    ...viewState,
    directoryExpansionDefaultVersion: DIRECTORY_EXPANSION_DEFAULT_VERSION,
    directoryExpansionMode: "all",
    expandedDirs: [...expandedDirs]
  };
  return true;
}
function sameIdSet(left, right) {
  if (left.size !== right.size) return false;
  for (const id of left) {
    if (!right.has(id)) return false;
  }
  return true;
}
function setDirectoryExpansionMode(mode) {
  viewState = {
    ...viewState,
    directoryExpansionDefaultVersion: DIRECTORY_EXPANSION_DEFAULT_VERSION,
    directoryExpansionMode: mode === "custom" ? "custom" : "all"
  };
}
function viewStatePayload() {
  return {
    directoryExpansionDefaultVersion: DIRECTORY_EXPANSION_DEFAULT_VERSION,
    directoryExpansionMode: viewState.directoryExpansionMode === "custom" ? "custom" : "all",
    selectedId: selected ? selected.id : null,
    transform,
    expandedDirs: [...expandedDirs],
    neighborhoodDepth,
    layerMode: layerMode.value,
    careFilter: careFilter.value,
    showDirs: showDirs.checked,
    showRelations: showRelations.checked,
    liveRefresh: liveRefresh.checked,
    search: searchInput.value
  };
}
function saveViewStateNow() {
  try {
    localStorage.setItem(viewStateKey, JSON.stringify(viewStatePayload()));
  } catch (_err) {
    // Private browsing or full storage should not break graph use.
  }
}
function scheduleViewStateSave() {
  if (viewSaveTimer) clearTimeout(viewSaveTimer);
  viewSaveTimer = setTimeout(() => {
    viewSaveTimer = null;
    saveViewStateNow();
  }, 150);
}
"""

__all__ = ["GRAPH_SCRIPT_STATE"]
