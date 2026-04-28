"""Render Graph JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_RENDER_GRAPH = r"""function renderGraph() {
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
    path.setAttribute("class", `edge ${edge.kind === "contains" ? "contains" : "relation"}`);
    path.setAttribute("stroke", edgeColors[edge.kind] || "#555f69");
    path.setAttribute("stroke-width", edgeWidth(edge));
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
