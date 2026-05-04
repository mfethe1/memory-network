"""Render Graph JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_RENDER_GRAPH = r"""function agentWorkText(value, limit = 36) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "Working";
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 3))}...` : text;
}
function agentWorkToken(value) {
  return String(value || "working").toLowerCase().replace(/[^a-z0-9_-]+/g, "-") || "working";
}
function agentWorkActionText(work) {
  const status = String((work && work.status) || "").toLowerCase();
  if (status === "completed") return "Done";
  if (status === "failed") return "Failed";
  if (status === "cancelled" || status === "canceled") return "Cancelled";
  if (status === "blocked") return "Blocked";
  const type = String((work && work.event_type) || "").toLowerCase();
  const labels = {
    edit: "Editing",
    read: "Reading",
    test: "Testing",
    tool: "Using tool",
    navigate: "Scanning",
    decision: "Thinking",
    task: "Starting",
    status: "Working",
    claim: "Claimed"
  };
  return labels[type] || "Working";
}
function agentWorkPath(value) {
  return String(value || "").replaceAll("\\", "/").replace(/^\.\//, "").trim();
}
function agentWorkPathTouchesNode(path, node) {
  const itemPath = agentWorkPath(path);
  if (!itemPath || !node) return false;
  const nodePath = agentWorkPath(node.path || ".");
  if (node.kind === "file") return itemPath === nodePath;
  if (nodePath === ".") return true;
  return itemPath === nodePath || itemPath.startsWith(`${nodePath}/`);
}
function agentWorkRunPaths(run) {
  const metadata = (run && run.metadata) || {};
  return uniquePaths(
    ((run && run.active_files) || [])
      .concat(metadata.selected_paths || [])
  );
}
function agentWorkRunTouchesNode(run, node) {
  if (!run || !node) return false;
  const selectedNodes = run.selected_nodes || [];
  if (selectedNodes.includes(node.id)) return true;
  const metadata = (run && run.metadata) || {};
  const paths = uniquePaths(
    ((run && run.active_files) || []).concat(metadata.selected_paths || [])
  );
  return paths.some(path => agentWorkPathTouchesNode(path, node));
}
function agentWorkClaimTouchesNode(claim, node) {
  return claim && agentWorkPathTouchesNode(claim.file_path, node);
}
function latestAgentWorkEvent(runId, node) {
  const events = ((data.activity && data.activity.agent_events) || [])
    .filter(event => {
      if (!event || event.run_id !== runId) return false;
      return !event.file_path || agentWorkPathTouchesNode(event.file_path, node);
    })
    .sort((a, b) => {
      const at = Date.parse(a.timestamp || "") || 0;
      const bt = Date.parse(b.timestamp || "") || 0;
      if (at !== bt) return bt - at;
      return Number(b.event_pk || 0) - Number(a.event_pk || 0);
    });
  return events[0] || null;
}
function activeAgentWorkForNode(node) {
  if (!node || !node.active_work) return null;
  const activeRuns = ((data.agent && data.agent.active_runs) || [])
    .filter(run => agentWorkRunTouchesNode(run, node))
    .map(run => {
      const metadata = run.metadata || {};
      const event = latestAgentWorkEvent(run.run_id, node);
      return {
        kind: "run",
        run_id: run.run_id,
        agent_name: run.agent_name || "Agent",
        provider: metadata.provider || "",
        status: run.status || "working",
        event_type: event ? event.event_type : "status",
        message: (event && event.message) || run.prompt || "Working",
        timestamp: (event && event.timestamp) || run.updated_at || run.started_at || ""
      };
    });
  const runIds = new Set(activeRuns.map(item => item.run_id).filter(Boolean));
  const activeClaims = ((data.agent && data.agent.active_claims) || [])
    .filter(claim => agentWorkClaimTouchesNode(claim, node))
    .filter(claim => !claim.run_id || !runIds.has(claim.run_id))
    .map(claim => ({
      kind: "claim",
      run_id: claim.run_id || "",
      agent_name: claim.agent_name || "Agent",
      provider: ((claim.metadata || {}).provider) || "",
      status: claim.run_status || "working",
      event_type: claim.mode || "claim",
      message: claim.reason || `${claim.mode || "file"} claim`,
      timestamp: claim.updated_at || claim.heartbeat_at || claim.created_at || ""
    }));
  const items = activeRuns.concat(activeClaims).sort((a, b) => {
    const at = Date.parse(a.timestamp || "") || 0;
    const bt = Date.parse(b.timestamp || "") || 0;
    return bt - at;
  });
  if (!items.length) return null;
  return {
    ...items[0],
    count: items.length
  };
}
function renderAgentWorkBubble(group, node, work, r) {
  const action = agentWorkActionText(work);
  const bubble = document.createElementNS("http://www.w3.org/2000/svg", "g");
  bubble.setAttribute("class", `agent-work-bubble is-${agentWorkToken(work.event_type)} status-${agentWorkToken(work.status)}`);
  bubble.setAttribute("role", work.run_id ? "button" : "img");
  bubble.setAttribute("tabindex", work.run_id ? "0" : "-1");
  bubble.dataset.agentName = work.agent_name || "Agent";
  if (work.provider) bubble.dataset.agentProvider = work.provider;
  if (work.run_id) bubble.dataset.runDetails = work.run_id;
  const size = 18;
  const offset = Math.max(r * 0.65, 6);
  const x = offset;
  const y = -offset;
  bubble.setAttribute("transform", `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
  const pulse = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  pulse.setAttribute("class", "agent-work-pulse");
  pulse.setAttribute("r", size / 2 + 1);
  pulse.setAttribute("cx", 0);
  pulse.setAttribute("cy", 0);
  bubble.appendChild(pulse);
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  bg.setAttribute("class", "agent-work-bg");
  bg.setAttribute("r", size / 2 - 1);
  bg.setAttribute("cx", 0);
  bg.setAttribute("cy", 0);
  bubble.appendChild(bg);
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "text");
  icon.setAttribute("text-anchor", "middle");
  icon.setAttribute("dominant-baseline", "central");
  icon.setAttribute("y", "0.5");
  icon.setAttribute("font-size", "9");
  icon.setAttribute("font-weight", "700");
  icon.setAttribute("fill", "var(--ink)");
  icon.textContent = "</>";
  bubble.appendChild(icon);
  const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
  title.textContent = `${action} · ${work.agent_name || "Agent"}${work.count > 1 ? ` +${work.count - 1}` : ""}: ${work.message || work.status || "working"}`;
  bubble.appendChild(title);
  const hit = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  hit.setAttribute("r", size / 2 + 4);
  hit.setAttribute("fill", "transparent");
  bubble.appendChild(hit);
  const openRun = event => {
    event.stopPropagation();
    if (work.run_id) {
      showRunTranscript(work.run_id, null, { focusComposer: true });
      return;
    }
    selectNode(node);
  };
  bubble.addEventListener("click", openRun);
  bubble.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openRun(event);
  });
  group.appendChild(bubble);
}
function renderGraph() {
  const renderStarted = performance.now();
  const visibleIds = computeVisibility();
  edgesLayer.textContent = "";
  nodesLayer.textContent = "";
  renderGroupLabels();
  edges.forEach(edge => {
    if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) return;
    if (edge.kind !== "contains" && !showRelations.checked) return;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const sx = edge.sourceNode.x;
    const sy = edge.sourceNode.y;
    const tx = edge.targetNode.x;
    const ty = edge.targetNode.y;
    const mx = (sx + tx) / 2;
    const my = (sy + ty) / 2 - (edge.kind === "contains" ? 0 : 18);
    path.setAttribute("d", `M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${mx.toFixed(1)} ${my.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)}`);
    path.setAttribute("class", `edge ${edge.kind === "contains" ? "contains" : `relation ${edge.kind}`}`);
    path.setAttribute("stroke", edgeColors[edge.kind] || "#555f69");
    path.setAttribute("stroke-width", edgeWidth(edge));
    if (edge.kind === "agent_derived") {
      path.setAttribute("stroke-dasharray", "5 5");
    }
    path.dataset.source = edge.source;
    path.dataset.target = edge.target;
    path.dataset.kind = edge.kind;
    edgesLayer.appendChild(path);
  });
  renderActivityTrailEdges();
  nodes.forEach(node => {
    if (!visibleIds.has(node.id)) return;
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute(
      "class",
      `node ${node.active_work ? "active" : ""} ${isRecentNode(node) ? "recent trail" : ""} ${selected && selected.id === node.id ? "selected" : ""}`
    );
    group.setAttribute("transform", `translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`);
    group.dataset.id = node.id;
    group.addEventListener("click", event => {
      event.stopPropagation();
      if (node.kind === "file") addToContextBasket(node.path);
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
    const work = activeAgentWorkForNode(node);
    if (work) renderAgentWorkBubble(group, node, work, r);
    nodesLayer.appendChild(group);
  });
  updateVisibility();
  renderNavigator();
  clientMetrics.render_count += 1;
  clientMetrics.last_render_ms = Math.round((performance.now() - renderStarted) * 100) / 100;
}
function renderGroupLabels() {
  if (!layerMode || !["communities", "roles", "flow"].includes(layerMode.value)) return;
  const rect = svg.getBoundingClientRect();
  const width = Math.max(600, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  const counts = groupCounts();
  const anchors = communityAnchors(width, height);
  groupKeys().slice(0, layerMode.value === "flow" ? 8 : 12).forEach(key => {
    const anchor = anchors.get(key);
    if (!anchor) return;
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", "community-label group-label");
    group.setAttribute("transform", `translate(${anchor.x.toFixed(1)} ${anchor.y.toFixed(1)})`);
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    const radius = layerMode.value === "flow"
      ? Math.max(42, Math.min(90, height * 0.18))
      : Math.max(34, Math.min(72, 28 + Math.sqrt(counts.get(key) || 1) * 7));
    circle.setAttribute("r", radius);
    circle.setAttribute("fill", groupColor(key));
    circle.setAttribute("opacity", layerMode.value === "flow" ? "0.055" : "0.08");
    group.appendChild(circle);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", 0);
    text.setAttribute("y", layerMode.value === "flow" ? -Math.min(72, radius + 12) : -42);
    text.setAttribute("text-anchor", "middle");
    text.textContent = `${groupLabel(key)} · ${counts.get(key) || 0}`;
    group.appendChild(text);
    edgesLayer.appendChild(group);
  });
}
"""

__all__ = ["GRAPH_SCRIPT_RENDER_GRAPH"]
