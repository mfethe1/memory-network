"""Standalone HTML renderer for `code_index graph`."""

from __future__ import annotations

import json
from typing import Any


def _json_for_html(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def render_html(payload: dict[str, Any]) -> str:
    graph_json = _json_for_html(payload)
    return HTML_TEMPLATE.replace("__GRAPH_JSON__", graph_json)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>code_index graph</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d1117;
      --ink: #e6edf3;
      --muted: #8b949e;
      --panel: #161b22;
      --panel-2: #0f141b;
      --field: #0b1016;
      --line: #30363d;
      --line-strong: #46515d;
      --critical: #ff6b6b;
      --high: #f2a65a;
      --medium: #5dd4c6;
      --low: #9aa7b3;
      --focus: #58a6ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    .app {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      height: 100vh;
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) minmax(220px, 360px) minmax(260px, 420px) auto;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: rgba(13, 17, 23, 0.96);
    }
    .brand {
      min-width: 0;
    }
    .brand h1 {
      margin: 0;
      font-size: 16px;
      line-height: 1.2;
      font-weight: 700;
    }
    .brand p {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .agent-status {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: var(--panel);
    }
    .agent-status strong {
      display: block;
      font-size: 12px;
      line-height: 1.25;
      color: var(--ink);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .agent-status span {
      display: block;
      margin-top: 3px;
      font-size: 11px;
      line-height: 1.25;
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .search {
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      font-size: 13px;
      background: var(--field);
      color: var(--ink);
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .controls select,
    .controls button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: inherit;
      font-size: 12px;
      padding: 0 10px;
      cursor: pointer;
    }
    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .workspace {
      min-height: 0;
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr) 7px minmax(320px, 430px);
    }
    .navigator {
      min-width: 0;
      min-height: 0;
      border-right: 1px solid var(--line);
      background: var(--panel);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .navigator-head {
      padding: 12px 12px 10px;
      border-bottom: 1px solid var(--line);
    }
    .navigator-head h2 {
      margin: 0;
      font-size: 13px;
      line-height: 1.2;
    }
    .navigator-head p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }
    .navigator-body {
      min-height: 0;
      overflow: auto;
      padding: 10px;
    }
    .nav-section {
      margin-bottom: 16px;
    }
    .nav-section h3 {
      margin: 0 0 8px;
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .nav-list {
      display: grid;
      gap: 4px;
    }
    .nav-row {
      width: 100%;
      min-height: 27px;
      border: 0;
      border-radius: 5px;
      background: transparent;
      color: var(--muted);
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr) auto;
      align-items: center;
      gap: 5px;
      padding: 4px 6px;
      font: inherit;
      font-size: 12px;
      text-align: left;
      cursor: pointer;
    }
    .nav-row:hover,
    .nav-row.active {
      background: #1f2833;
      color: var(--ink);
    }
    .nav-row.recent {
      color: #ffd166;
    }
    .nav-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .nav-badge {
      color: var(--muted);
      font-size: 10px;
    }
    .graph-wrap {
      position: relative;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      background:
        linear-gradient(90deg, rgba(139,148,158,0.08) 1px, transparent 1px),
        linear-gradient(0deg, rgba(139,148,158,0.08) 1px, transparent 1px);
      background-size: 42px 42px;
    }
    .resizer {
      cursor: col-resize;
      background: var(--panel-2);
      border-left: 1px solid var(--line);
      border-right: 1px solid var(--line);
    }
    .resizer:hover,
    body.resizing .resizer {
      background: var(--focus);
    }
    svg {
      width: 100%;
      height: 100%;
      display: block;
    }
    .edge {
      stroke: #9aa3ad;
      stroke-opacity: 0.32;
      fill: none;
      pointer-events: none;
    }
    .edge.relation {
      stroke: #555f69;
      stroke-opacity: 0.48;
    }
    .edge.activity {
      stroke: #ffd166;
      stroke-opacity: 0.75;
      stroke-dasharray: 5 5;
    }
    .node {
      cursor: pointer;
    }
    .node text {
      font-size: 10px;
      fill: var(--ink);
      pointer-events: none;
      paint-order: stroke;
      stroke: rgba(13,17,23,0.9);
      stroke-width: 3px;
      stroke-linejoin: round;
    }
    .node circle,
    .node rect {
      stroke-width: 1.5px;
      stroke: rgba(240,246,252,0.82);
      filter: drop-shadow(0 1px 2px rgba(0,0,0,0.18));
    }
    .node.selected circle,
    .node.selected rect {
      stroke: #f0f6fc;
      stroke-width: 3px;
    }
    .node.recent circle,
    .node.recent rect {
      stroke: #ffd166;
      stroke-width: 4px;
    }
    .node.trail circle,
    .node.trail rect {
      filter: drop-shadow(0 0 8px rgba(255, 209, 102, 0.45));
    }
    .node.active circle,
    .node.active rect {
      stroke: var(--focus);
      stroke-width: 3px;
    }
    .node.dim,
    .edge.dim {
      opacity: 0.12;
    }
    .legend {
      position: absolute;
      left: 14px;
      bottom: 14px;
      display: grid;
      gap: 7px;
      padding: 10px 12px;
      border: 1px solid rgba(217,222,216,0.92);
      border-radius: 8px;
      background: rgba(22,27,34,0.92);
      backdrop-filter: blur(10px);
      font-size: 12px;
      color: var(--muted);
    }
    .legend-row {
      display: flex;
      align-items: center;
      gap: 7px;
      white-space: nowrap;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      display: inline-block;
    }
    .inspector {
      min-width: 0;
      min-height: 0;
      background: var(--panel);
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
    }
    .inspector-head {
      padding: 16px 16px 12px;
      border-bottom: 1px solid var(--line);
    }
    .eyebrow {
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .inspector h2 {
      margin: 0;
      font-size: 18px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      color: var(--muted);
      font-size: 11px;
      background: var(--panel-2);
    }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 10px 16px;
      border-bottom: 1px solid var(--line);
    }
    .tab {
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--muted);
      font: inherit;
      font-size: 12px;
      padding: 0 10px;
      cursor: pointer;
    }
    .tab.active {
      color: var(--ink);
      border-color: #aeb7b0;
      background: #1f2833;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .small-button {
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: inherit;
      font-size: 12px;
      padding: 6px 10px;
      cursor: pointer;
    }
    .panel-body {
      min-height: 0;
      overflow: auto;
      padding: 14px 16px 18px;
    }
    .summary-text {
      margin: 0 0 14px;
      color: #c9d1d9;
      font-size: 13px;
      line-height: 1.5;
    }
    .section {
      margin-top: 16px;
    }
    .section h3 {
      margin: 0 0 8px;
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .kv {
      display: grid;
      grid-template-columns: 132px minmax(0, 1fr);
      gap: 8px 10px;
      font-size: 12px;
      line-height: 1.4;
    }
    .kv dt {
      color: var(--muted);
    }
    .kv dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    ul.compact {
      margin: 0;
      padding-left: 18px;
      font-size: 12px;
      line-height: 1.45;
    }
    textarea.note-box {
      width: 100%;
      min-height: 150px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      font: 13px/1.45 inherit;
      color: var(--ink);
      background: var(--field);
    }
    .edit-list {
      display: grid;
      gap: 8px;
    }
    .edit-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: var(--panel-2);
      font-size: 12px;
      line-height: 1.4;
    }
    .edit-item strong {
      display: block;
      margin-bottom: 4px;
      color: var(--ink);
    }
    .edit-item span {
      color: var(--muted);
    }
    pre {
      margin: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #070b10;
      color: #e7ecec;
      overflow: auto;
      font: 12px/1.45 "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      white-space: pre;
    }
    .empty {
      color: var(--muted);
      font-size: 13px;
    }
    @media (max-width: 900px) {
      .topbar {
        grid-template-columns: 1fr;
      }
      .controls {
        justify-content: flex-start;
      }
      .workspace {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(220px, 32vh) minmax(360px, 40vh) minmax(360px, 28vh);
      }
      .navigator {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .resizer {
        display: none;
      }
      .inspector {
        border-left: 0;
        border-top: 1px solid var(--line);
      }
      .legend {
        display: none;
      }
    }
  </style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="brand">
      <h1>code_index graph</h1>
      <p id="repo-subtitle"></p>
    </div>
    <div class="agent-status">
      <strong id="agent-name">Agent</strong>
      <span id="agent-files">No active file</span>
    </div>
    <input class="search" id="search" placeholder="Search files, directories, symbols, imports">
    <div class="controls">
      <select id="layer-mode" aria-label="Graph layer">
        <option value="overview">Overview</option>
        <option value="directory">Directory layer</option>
        <option value="focus">Focused node</option>
        <option value="all">All nodes</option>
      </select>
      <select id="care-filter" aria-label="Care filter">
        <option value="all">All care levels</option>
        <option value="critical">Critical</option>
        <option value="high">High</option>
        <option value="medium">Medium</option>
        <option value="low">Low</option>
      </select>
      <label class="toggle"><input id="show-dirs" type="checkbox" checked> directories</label>
      <label class="toggle"><input id="show-relations" type="checkbox" checked> relations</label>
      <label class="toggle"><input id="live-refresh" type="checkbox"> live</label>
      <button id="refresh-graph" type="button">Refresh</button>
      <button id="reset-view" type="button">Reset</button>
    </div>
  </header>
  <main class="workspace">
    <aside class="navigator">
      <div class="navigator-head">
        <h2>Navigator</h2>
        <p id="navigator-summary">Tree and recent activity</p>
      </div>
      <div class="navigator-body">
        <section class="nav-section">
          <h3>Last Edited</h3>
          <div class="nav-list" id="recent-files"></div>
        </section>
        <section class="nav-section">
          <h3>Code Tree</h3>
          <div class="nav-list" id="tree-view"></div>
        </section>
      </div>
    </aside>
    <section class="graph-wrap" id="graph-wrap">
      <svg id="graph" role="img" aria-label="Repository graph">
        <g id="viewport">
          <g id="edges"></g>
          <g id="nodes"></g>
        </g>
      </svg>
      <div class="legend">
        <div class="legend-row"><span class="dot" style="background:var(--critical)"></span>Critical care</div>
        <div class="legend-row"><span class="dot" style="background:var(--high)"></span>High care</div>
        <div class="legend-row"><span class="dot" style="background:var(--medium)"></span>Medium care</div>
        <div class="legend-row"><span class="dot" style="background:var(--low)"></span>Low care</div>
        <div class="legend-row"><span class="dot" style="background:var(--focus)"></span>Active work</div>
      </div>
    </section>
    <div class="resizer" id="panel-resizer" title="Resize side panel"></div>
    <aside class="inspector">
      <div class="inspector-head">
        <p class="eyebrow" id="node-kind">Repository</p>
        <h2 id="node-title">Select a node</h2>
        <div class="meta" id="node-meta"></div>
      </div>
      <div class="tabs">
        <button class="tab active" id="tab-summary" type="button">Summary</button>
        <button class="tab" id="tab-edits" type="button">Edits</button>
        <button class="tab" id="tab-notes" type="button">Notes</button>
        <button class="tab" id="tab-code" type="button">Code</button>
      </div>
      <div class="panel-body" id="panel-body"></div>
    </aside>
  </main>
</div>
<script id="graph-data" type="application/json">__GRAPH_JSON__</script>
<script>
let data = JSON.parse(document.getElementById("graph-data").textContent);
const svg = document.getElementById("graph");
const viewport = document.getElementById("viewport");
const edgesLayer = document.getElementById("edges");
const nodesLayer = document.getElementById("nodes");
const workspace = document.querySelector(".workspace");
const panelResizer = document.getElementById("panel-resizer");
const navigatorSummary = document.getElementById("navigator-summary");
const recentFilesEl = document.getElementById("recent-files");
const treeViewEl = document.getElementById("tree-view");
const searchInput = document.getElementById("search");
const layerMode = document.getElementById("layer-mode");
const careFilter = document.getElementById("care-filter");
const showDirs = document.getElementById("show-dirs");
const showRelations = document.getElementById("show-relations");
const liveRefresh = document.getElementById("live-refresh");
const refreshGraph = document.getElementById("refresh-graph");
const resetView = document.getElementById("reset-view");
const nodeKind = document.getElementById("node-kind");
const nodeTitle = document.getElementById("node-title");
const nodeMeta = document.getElementById("node-meta");
const panelBody = document.getElementById("panel-body");
const tabSummary = document.getElementById("tab-summary");
const tabEdits = document.getElementById("tab-edits");
const tabNotes = document.getElementById("tab-notes");
const tabCode = document.getElementById("tab-code");
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
  overrides: "#8c5d68"
};
let nodes = [];
let nodeById = new Map();
let edges = [];
let selected = null;
let activeTab = "summary";
let transform = { x: 0, y: 0, k: 1 };
const notesKey = `code_index_graph_notes:${data.root}`;
const panelWidthKey = `code_index_graph_panel_width:${data.root}`;
let notes = loadNotes();
let liveTimer = null;
let refreshing = false;

function hydrateData(nextData, options = {}) {
  const priorSelectedId = selected && selected.id;
  data = nextData;
  mergeServerNotes();
  repoSubtitle.textContent =
    `${data.summary.file_count} files, ${data.summary.relation_edge_count} relation edges, generated ${data.generated_at}`;
  const agent = data.agent || {};
  const activeFiles = agent.active_files || data.focus_paths || [];
  const activeAgents = (agent.active_agents && agent.active_agents.length)
    ? agent.active_agents.join(", ")
    : (agent.name || "Agent");
  const activeRuns = agent.active_runs || [];
  agentName.textContent = `${activeAgents} · ${agent.status || (activeFiles.length ? "working" : "idle")}`;
  agentFiles.textContent = activeFiles.length
    ? activeFiles.join(", ")
    : (activeRuns.length ? `${activeRuns.length} active run(s)` : "No active file");
  nodes = data.nodes.map((node, index) => ({
    ...node,
    index,
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
    visible: true
  }));
  nodeById = new Map(nodes.map(n => [n.id, n]));
  edges = data.edges
    .map(edge => ({ ...edge, sourceNode: nodeById.get(edge.source), targetNode: nodeById.get(edge.target) }))
    .filter(edge => edge.sourceNode && edge.targetNode);
  selected =
    (options.preserveSelection && priorSelectedId && nodeById.get(priorSelectedId)) ||
    nodes.find(n => n.active_work) ||
    nodes.find(n => n.kind === "directory" && n.path === ".") ||
    nodes[0] ||
    null;
  renderNavigator();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function fileRadius(node) {
  if (node.kind === "directory") return Math.max(8, Math.min(18, 8 + Math.sqrt(node.metrics.file_count || 1)));
  return Math.max(5, Math.min(20, 6 + Math.sqrt(Number(node.importance.score) || 0)));
}
function nodeColor(node) {
  if (node.kind === "directory") return colors.directory;
  return colors[node.care_level] || colors.medium;
}
function recentRank(node) {
  return node && node.metrics ? node.metrics.recent_edit_rank : null;
}
function isRecentNode(node) {
  return Number(recentRank(node) || 0) > 0 && Number(recentRank(node) || 0) <= 5;
}
function edgeWidth(edge) {
  return Math.max(0.7, Math.min(5, 0.7 + Math.log2((edge.weight || 1) + 1)));
}
function searchableText(node) {
  const symbols = (node.symbols || []).map(s => s.canonical_name).join(" ");
  const imports = (node.imports || []).join(" ");
  return `${node.path} ${node.label} ${node.role} ${node.language} ${symbols} ${imports}`.toLowerCase();
}
function parentDirectories(path) {
  if (!path || path === ".") return new Set(["dir:."]);
  const pieces = path.split("/");
  const ids = new Set(["dir:."]);
  let current = [];
  const limit = path.includes(".") ? pieces.slice(0, -1) : pieces;
  limit.forEach(part => {
    current.push(part);
    ids.add(`dir:${current.join("/")}`);
  });
  return ids;
}
function isInsideDirectory(node, directory) {
  if (directory === ".") return true;
  if (node.kind === "directory") {
    return node.path === directory || node.path.startsWith(`${directory}/`);
  }
  return node.directory === directory || node.path.startsWith(`${directory}/`);
}
function isImmediateChildDirectory(node, directory) {
  return node.kind === "directory" && node.directory === directory;
}
function neighborIds(node) {
  const ids = new Set([node.id]);
  const incoming = (node.metrics && node.metrics.incoming_files) || [];
  const outgoing = (node.metrics && node.metrics.outgoing_files) || [];
  incoming.concat(outgoing).forEach(path => ids.add(`file:${path}`));
  parentDirectories(node.path).forEach(id => ids.add(id));
  return ids;
}
function passesLayer(node) {
  const mode = layerMode.value;
  if (mode === "all") return true;
  if (node.active_work) return true;
  if (mode === "overview") {
    return node.kind === "directory" || node.care_level === "critical" || (node.importance.rank && node.importance.rank <= 24);
  }
  if (mode === "directory") {
    const dir = selected && selected.kind === "directory" ? selected.path : (selected ? selected.directory : ".");
    return node.path === dir || node.directory === dir || isImmediateChildDirectory(node, dir) || parentDirectories(dir).has(node.id);
  }
  if (mode === "focus" && selected) {
    return neighborIds(selected).has(node.id);
  }
  return true;
}
function passesFilter(node) {
  const care = careFilter.value;
  if (!showDirs.checked && node.kind === "directory") return false;
  if (!passesLayer(node)) return false;
  if (care !== "all" && node.kind === "file" && node.care_level !== care) return false;
  const q = searchInput.value.trim().toLowerCase();
  if (q && !searchableText(node).includes(q)) return false;
  return true;
}
function initPositions() {
  const rect = svg.getBoundingClientRect();
  const width = Math.max(600, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  nodes.forEach((node, index) => {
    const depth = node.path === "." ? 0 : node.path.split("/").length;
    const angle = (index * 2.399963229728653) % (Math.PI * 2);
    const ring = node.kind === "directory" ? 56 + depth * 72 : 190 + (index % 7) * 22;
    node.x = width / 2 + Math.cos(angle) * ring;
    node.y = height / 2 + Math.sin(angle) * ring * 0.7;
  });
}
function tickSimulation(iterations = 1) {
  const rect = svg.getBoundingClientRect();
  const width = Math.max(600, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  for (let step = 0; step < iterations; step++) {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = b.x - a.x || 0.01;
        const dy = b.y - a.y || 0.01;
        const dist2 = dx * dx + dy * dy;
        const minDist = fileRadius(a) + fileRadius(b) + 18;
        const force = Math.min(1.4, 980 / Math.max(dist2, 70));
        a.vx -= dx * force * 0.015;
        a.vy -= dy * force * 0.015;
        b.vx += dx * force * 0.015;
        b.vy += dy * force * 0.015;
        const dist = Math.sqrt(dist2);
        if (dist < minDist) {
          const push = (minDist - dist) * 0.015;
          a.vx -= (dx / dist) * push;
          a.vy -= (dy / dist) * push;
          b.vx += (dx / dist) * push;
          b.vy += (dy / dist) * push;
        }
      }
    }
    edges.forEach(edge => {
      if (edge.kind !== "contains" && !showRelations.checked) return;
      const a = edge.sourceNode;
      const b = edge.targetNode;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const ideal = edge.kind === "contains" ? 78 : 132;
      const strength = edge.kind === "contains" ? 0.018 : 0.01;
      const delta = (distance - ideal) * strength;
      const nx = dx / distance;
      const ny = dy / distance;
      a.vx += nx * delta;
      a.vy += ny * delta;
      b.vx -= nx * delta;
      b.vy -= ny * delta;
    });
    nodes.forEach(node => {
      const anchorX = node.kind === "directory" ? width * 0.45 : width * 0.52;
      const anchorY = height * 0.5;
      node.vx += (anchorX - node.x) * 0.0008;
      node.vy += (anchorY - node.y) * 0.0008;
      node.vx *= 0.82;
      node.vy *= 0.82;
      node.x += node.vx;
      node.y += node.vy;
    });
  }
}
function renderGraph() {
  edgesLayer.textContent = "";
  nodesLayer.textContent = "";
  edges.forEach(edge => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const sx = edge.sourceNode.x;
    const sy = edge.sourceNode.y;
    const tx = edge.targetNode.x;
    const ty = edge.targetNode.y;
    const mx = (sx + tx) / 2;
    const my = (sy + ty) / 2 - (edge.kind === "contains" ? 0 : 18);
    path.setAttribute("d", `M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${mx.toFixed(1)} ${my.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)}`);
    path.setAttribute("class", `edge ${edge.kind === "contains" ? "contains" : "relation"}`);
    path.setAttribute("stroke", edgeColors[edge.kind] || "#555f69");
    path.setAttribute("stroke-width", edgeWidth(edge));
    path.dataset.source = edge.source;
    path.dataset.target = edge.target;
    path.dataset.kind = edge.kind;
    edgesLayer.appendChild(path);
  });
  const trail = (data.activity && data.activity.trail) || [];
  trail.forEach((step, index) => {
    const from = nodeById.get(`file:${step.from}`);
    const to = nodeById.get(`file:${step.to}`);
    if (!from || !to) return;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2 + 24 + index * 3;
    path.setAttribute("d", `M ${from.x.toFixed(1)} ${from.y.toFixed(1)} Q ${mx.toFixed(1)} ${my.toFixed(1)} ${to.x.toFixed(1)} ${to.y.toFixed(1)}`);
    path.setAttribute("class", "edge activity");
    path.setAttribute("stroke-width", Math.max(1.5, 4 - index * 0.35));
    path.dataset.source = `file:${step.from}`;
    path.dataset.target = `file:${step.to}`;
    path.dataset.kind = "activity";
    edgesLayer.appendChild(path);
  });
  nodes.forEach(node => {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute(
      "class",
      `node ${node.active_work ? "active" : ""} ${isRecentNode(node) ? "recent trail" : ""} ${selected && selected.id === node.id ? "selected" : ""}`
    );
    group.setAttribute("transform", `translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`);
    group.dataset.id = node.id;
    group.addEventListener("click", event => {
      event.stopPropagation();
      selectNode(node);
    });
    const r = fileRadius(node);
    if (node.kind === "directory") {
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", -r);
      rect.setAttribute("y", -r);
      rect.setAttribute("width", r * 2);
      rect.setAttribute("height", r * 2);
      rect.setAttribute("rx", 4);
      rect.setAttribute("fill", nodeColor(node));
      group.appendChild(rect);
    } else {
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("r", r);
      circle.setAttribute("fill", nodeColor(node));
      group.appendChild(circle);
    }
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", r + 5);
    label.setAttribute("y", 3);
    label.textContent = node.label;
    group.appendChild(label);
    nodesLayer.appendChild(group);
  });
  updateVisibility();
  renderNavigator();
}
function renderNavigator() {
  if (!recentFilesEl || !treeViewEl) return;
  const recentFiles = (data.summary && data.summary.recent_files) || [];
  navigatorSummary.textContent = `${data.summary.file_count} files · ${recentFiles.length} recent`;
  recentFilesEl.innerHTML = recentFiles.length
    ? recentFiles.map(item => navButtonHtml({
        id: `file:${item.file_path}`,
        icon: String(item.rank),
        label: item.file_path,
        badge: `${item.edit_count} edits`,
        recent: true
      })).join("")
    : `<div class="empty">No recent edits indexed.</div>`;

  const visibleTreeNodes = nodes
    .filter(node => node.kind === "directory" || node.kind === "file")
    .sort((a, b) => {
      if (a.path === ".") return -1;
      if (b.path === ".") return 1;
      return a.path.localeCompare(b.path);
    });
  treeViewEl.innerHTML = visibleTreeNodes.map(node => {
    const depth = node.path === "." ? 0 : node.path.split("/").length - (node.kind === "file" ? 1 : 0);
    const indent = Math.min(44, depth * 12);
    const icon = node.kind === "directory" ? "▸" : "·";
    const badge = node.kind === "file" && node.metrics && node.metrics.recent_edit_rank ? `#${node.metrics.recent_edit_rank}` : "";
    return navButtonHtml({
      id: node.id,
      icon,
      label: node.path === "." ? "repo" : node.label,
      badge,
      recent: isRecentNode(node),
      indent
    });
  }).join("");

  document.querySelectorAll("[data-nav-node]").forEach(button => {
    button.addEventListener("click", () => {
      const node = nodeById.get(button.dataset.navNode);
      if (node) selectNode(node);
    });
  });
}
function navButtonHtml({ id, icon, label, badge = "", recent = false, indent = 0 }) {
  const active = selected && selected.id === id ? " active" : "";
  return `
    <button class="nav-row${recent ? " recent" : ""}${active}" data-nav-node="${escapeHtml(id)}" type="button">
      <span style="padding-left:${indent}px">${escapeHtml(icon)}</span>
      <span class="nav-name">${escapeHtml(label)}</span>
      <span class="nav-badge">${escapeHtml(badge)}</span>
    </button>
  `;
}
function updateVisibility() {
  const visibleIds = new Set();
  nodes.forEach(node => {
    node.visible = passesFilter(node);
    if (node.visible) visibleIds.add(node.id);
  });
  document.querySelectorAll(".node").forEach(el => {
    const node = nodeById.get(el.dataset.id);
    el.classList.toggle("dim", !node || !node.visible);
  });
  document.querySelectorAll(".edge").forEach(el => {
    const isRelation = el.dataset.kind !== "contains";
    const hiddenKind = isRelation && !showRelations.checked;
    const hidden = hiddenKind || !visibleIds.has(el.dataset.source) || !visibleIds.has(el.dataset.target);
    el.classList.toggle("dim", hidden);
    el.style.display = hiddenKind ? "none" : "";
  });
}
function pill(text) {
  return `<span class="pill">${escapeHtml(text)}</span>`;
}
function renderInspector() {
  if (!selected) return;
  nodeKind.textContent = selected.kind === "directory" ? "Directory" : selected.role_label;
  nodeTitle.textContent = selected.path;
  const meta = [
    selected.care_level,
    selected.language,
    `score ${selected.importance.score}`,
    selected.active_work ? "active work" : null
  ].filter(Boolean);
  nodeMeta.innerHTML = meta.map(pill).join("");
  tabSummary.classList.toggle("active", activeTab === "summary");
  tabEdits.classList.toggle("active", activeTab === "edits");
  tabNotes.classList.toggle("active", activeTab === "notes");
  tabCode.classList.toggle("active", activeTab === "code");
  if (activeTab === "code") {
    panelBody.innerHTML = renderCode(selected);
  } else if (activeTab === "edits") {
    panelBody.innerHTML = renderEdits(selected);
  } else if (activeTab === "notes") {
    panelBody.innerHTML = renderNotes(selected);
    bindNotesPanel(selected);
  } else {
    panelBody.innerHTML = renderSummary(selected);
  }
}
function renderSummary(node) {
  const metrics = node.metrics || {};
  const reasons = (node.importance.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join("");
  const symbols = (node.symbols || []).slice(0, 12).map(s =>
    `<li>${escapeHtml(s.kind)}: ${escapeHtml(s.canonical_name)}${s.line ? ` at line ${escapeHtml(s.line)}` : ""}</li>`
  ).join("");
  const imports = (node.imports || []).slice(0, 12).map(i => `<li>${escapeHtml(i)}</li>`).join("");
  const incoming = (metrics.incoming_files || []).slice(0, 10).map(i => `<li>${escapeHtml(i)}</li>`).join("");
  const outgoing = (metrics.outgoing_files || []).slice(0, 10).map(i => `<li>${escapeHtml(i)}</li>`).join("");
  return `
    <p class="summary-text">${escapeHtml(node.summary)}</p>
    <div class="section">
      <h3>Care Guidance</h3>
      <p class="summary-text">${escapeHtml(node.freedom)}</p>
      <ul class="compact">${reasons}</ul>
    </div>
    <div class="section">
      <h3>Metrics</h3>
      <dl class="kv">
        <dt>Rank</dt><dd>${escapeHtml(node.importance.rank || "n/a")}</dd>
        <dt>Lines</dt><dd>${escapeHtml(metrics.line_count || "n/a")}</dd>
        <dt>Symbols</dt><dd>${escapeHtml(metrics.symbol_count || 0)}</dd>
        <dt>Chunks</dt><dd>${escapeHtml(metrics.chunk_count || 0)}</dd>
        <dt>Inbound</dt><dd>${escapeHtml(metrics.incoming_relations || 0)}</dd>
        <dt>Outbound</dt><dd>${escapeHtml(metrics.outgoing_relations || 0)}</dd>
        <dt>Edits</dt><dd>${escapeHtml(metrics.edit_count || 0)}</dd>
        <dt>Tests</dt><dd>${escapeHtml(metrics.test_count || 0)}</dd>
      </dl>
    </div>
    ${symbols ? `<div class="section"><h3>Symbols</h3><ul class="compact">${symbols}</ul></div>` : ""}
    ${imports ? `<div class="section"><h3>Imports</h3><ul class="compact">${imports}</ul></div>` : ""}
    ${incoming ? `<div class="section"><h3>Incoming Files</h3><ul class="compact">${incoming}</ul></div>` : ""}
    ${outgoing ? `<div class="section"><h3>Outgoing Files</h3><ul class="compact">${outgoing}</ul></div>` : ""}
  `;
}
function editsForNode(node) {
  if (node.kind === "file") return node.recent_edits || [];
  const edits = (data.summary.recent_edits || []);
  if (node.path === ".") return edits.slice(0, 30);
  return edits.filter(edit => edit.file_path === node.path || edit.file_path.startsWith(`${node.path}/`)).slice(0, 30);
}
function renderEdits(node) {
  const edits = editsForNode(node);
  if (!edits.length) {
    return `<p class="empty">No recorded edits for this layer yet.</p>`;
  }
  const items = edits.map(edit => `
    <div class="edit-item">
      <strong>${escapeHtml(edit.change_type || "edit")} ${escapeHtml(edit.symbol_path || edit.file_path)}</strong>
      <span>${escapeHtml(edit.timestamp || "")} · ${escapeHtml(edit.event_source || "unknown")} · ${escapeHtml(edit.chunk_type || "chunk")}</span>
      <div>${escapeHtml(edit.diff_summary || "No diff summary recorded.")}</div>
    </div>
  `).join("");
  return `<div class="edit-list">${items}</div>`;
}
function renderCode(node) {
  if (node.kind !== "file") {
    return `<p class="empty">${escapeHtml(node.code.reason || "No code for this node.")}</p>`;
  }
  if (!node.code || !node.code.included) {
    return `<p class="empty">${escapeHtml((node.code && node.code.reason) || "Code was not embedded.")}</p>`;
  }
  return `<pre><code>${escapeHtml(node.code.content)}</code></pre>`;
}
function loadNotes() {
  try {
    return JSON.parse(localStorage.getItem(notesKey) || "{}");
  } catch (_err) {
    return {};
  }
}
function saveNotes() {
  localStorage.setItem(notesKey, JSON.stringify(notes));
}
function mergeServerNotes() {
  const byNode = (data.notes && data.notes.by_node) || {};
  let changed = false;
  Object.entries(byNode).forEach(([nodeId, note]) => {
    const local = notes[nodeId];
    const localTime = Date.parse((local && local.updated_at) || 0) || 0;
    const serverTime = Date.parse(note.updated_at || 0) || 0;
    if (!local || serverTime >= localTime) {
      notes[nodeId] = {
        note: note.note || "",
        path: note.path,
        kind: note.node_kind || note.kind,
        care_level: note.care_level,
        summary: note.summary,
        updated_at: note.updated_at
      };
      changed = true;
    }
  });
  if (changed) saveNotes();
}
function canPostToGraphServer() {
  const httpPage = window.location.protocol === "http:" || window.location.protocol === "https:";
  return httpPage && !!(data.live && data.live.server);
}
async function postNoteToServer(node, note) {
  if (!canPostToGraphServer()) return;
  try {
    await fetch((data.live && data.live.notes_path) || "/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        node_id: node.id,
        path: node.path,
        node_kind: node.kind,
        care_level: node.care_level,
        summary: node.summary,
        note: note.note || ""
      })
    });
  } catch (_err) {
    // Static servers cannot accept note writes; localStorage remains the fallback.
  }
}
function noteText(node) {
  return (notes[node.id] && notes[node.id].note) || "";
}
function notePayload() {
  return {
    kind: "code_index_graph_notes",
    root: data.root,
    graph_generated_at: data.generated_at,
    exported_at: new Date().toISOString(),
    notes: Object.entries(notes).map(([node_id, note]) => {
      const node = nodeById.get(node_id);
      return {
        node_id,
        path: node ? node.path : note.path,
        node_kind: node ? node.kind : note.kind,
        care_level: node ? node.care_level : note.care_level,
        note: note.note,
        updated_at: note.updated_at,
        summary: node ? node.summary : note.summary
      };
    })
  };
}
function taskPayload(node) {
  return {
    kind: "code_index_graph_agent_task",
    root: data.root,
    created_at: new Date().toISOString(),
    selected_nodes: [node.id],
    message: noteText(node),
    node: {
      id: node.id,
      path: node.path,
      kind: node.kind,
      care_level: node.care_level,
      role: node.role,
      summary: node.summary,
      incoming_files: (node.metrics && node.metrics.incoming_files) || [],
      outgoing_files: (node.metrics && node.metrics.outgoing_files) || [],
      recent_edits: editsForNode(node).slice(0, 12)
    }
  };
}
function exportJson(payload, filename) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function copyJson(payload) {
  const text = JSON.stringify(payload, null, 2);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const area = document.createElement("textarea");
  area.value = text;
  document.body.appendChild(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  return ok;
}
function renderNotes(node) {
  return `
    <p class="summary-text">Notes save locally in this browser and, when served by graph-server, sync into .code_index/graph-notes.json for agent review.</p>
    <textarea class="note-box" id="node-note" placeholder="Leave instructions, questions, review notes, or constraints for this node.">${escapeHtml(noteText(node))}</textarea>
    <div class="actions">
      <button class="small-button" id="save-note" type="button">Save note</button>
      <button class="small-button" id="copy-task-json" type="button">Copy task JSON</button>
      <button class="small-button" id="export-notes-json" type="button">Export notes JSON</button>
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
      await copyJson(taskPayload(node));
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy task JSON"; }, 900);
    });
  }
  if (exportButton) {
    exportButton.addEventListener("click", () => {
      exportJson(notePayload(), "code-index-graph-notes.json");
    });
  }
}
function selectNode(node) {
  selected = node;
  if (node.kind === "directory") {
    layerMode.value = "directory";
  } else if (node.kind === "file" && layerMode.value !== "all") {
    layerMode.value = "focus";
  }
  renderGraph();
  renderInspector();
}
function resetTransform() {
  transform = { x: 0, y: 0, k: 1 };
  viewport.setAttribute("transform", "translate(0 0) scale(1)");
}
function setPanelWidth(widthPx) {
  if (!workspace || window.matchMedia("(max-width: 900px)").matches) return;
  const width = Math.max(320, Math.min(760, Number(widthPx) || 430));
  workspace.style.gridTemplateColumns = `280px minmax(0, 1fr) 7px ${width}px`;
  localStorage.setItem(panelWidthKey, String(width));
}
function restorePanelWidth() {
  const saved = localStorage.getItem(panelWidthKey);
  if (saved) setPanelWidth(Number(saved));
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
    const columns = getComputedStyle(workspace).gridTemplateColumns.split(" ");
    startWidth = parseFloat(columns[3]) || 430;
    startX = event.clientX;
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}
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
async function refreshGraphData() {
  if (refreshing) return;
  refreshing = true;
  const original = refreshGraph.textContent;
  refreshGraph.textContent = "Refreshing";
  try {
    const response = await fetch(sidecarUrl(), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const nextData = await response.json();
    hydrateData(nextData, { preserveSelection: true });
    initPositions();
    tickSimulation(220);
    renderGraph();
    renderInspector();
    refreshGraph.textContent = "Updated";
    setTimeout(() => { refreshGraph.textContent = original; }, 1000);
  } catch (_err) {
    // file:// pages often cannot fetch sibling files. Reloading still picks
    // up the latest HTML if a watcher or agent regenerated it.
    if (!liveRefresh.checked) window.location.reload();
    refreshGraph.textContent = original;
  } finally {
    refreshing = false;
  }
}
function setLiveRefresh(enabled) {
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
  if (enabled) {
    liveTimer = setInterval(() => {
      if (document.visibilityState === "visible") refreshGraphData();
    }, 3000);
    refreshGraphData();
  }
}
function bindEventStream() {
  if (!canPostToGraphServer() || !window.EventSource) return;
  try {
    const source = new EventSource((data.live && data.live.events_path) || "/events");
    source.addEventListener("graph", () => {
      if (document.visibilityState === "visible") refreshGraphData();
    });
  } catch (_err) {
    // The static HTML still works through manual refresh / polling.
  }
}
svg.addEventListener("wheel", event => {
  event.preventDefault();
  const factor = event.deltaY < 0 ? 1.08 : 0.92;
  transform.k = Math.max(0.25, Math.min(3.5, transform.k * factor));
  transform.x -= event.deltaX;
  transform.y -= event.deltaY * 0.2;
  viewport.setAttribute("transform", `translate(${transform.x} ${transform.y}) scale(${transform.k})`);
}, { passive: false });
searchInput.addEventListener("input", updateVisibility);
layerMode.addEventListener("change", updateVisibility);
careFilter.addEventListener("change", updateVisibility);
showDirs.addEventListener("change", updateVisibility);
showRelations.addEventListener("change", updateVisibility);
refreshGraph.addEventListener("click", refreshGraphData);
liveRefresh.addEventListener("change", () => setLiveRefresh(liveRefresh.checked));
resetView.addEventListener("click", () => {
  searchInput.value = "";
  layerMode.value = "overview";
  careFilter.value = "all";
  showDirs.checked = true;
  showRelations.checked = true;
  resetTransform();
  updateVisibility();
});
tabSummary.addEventListener("click", () => { activeTab = "summary"; renderInspector(); });
tabEdits.addEventListener("click", () => { activeTab = "edits"; renderInspector(); });
tabNotes.addEventListener("click", () => { activeTab = "notes"; renderInspector(); });
tabCode.addEventListener("click", () => { activeTab = "code"; renderInspector(); });

hydrateData(data);
bindEventStream();
restorePanelWidth();
bindPanelResizer();
initPositions();
tickSimulation(220);
renderGraph();
renderInspector();
</script>
</body>
</html>
"""
