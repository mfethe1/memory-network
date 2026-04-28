"""Layout JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_LAYOUT = r"""function fileRadius(node) {
  if (node.kind === "directory") return Math.max(8, Math.min(18, 8 + Math.sqrt(node.metrics.file_count || 1)));
  return Math.max(5, Math.min(20, 6 + Math.sqrt(Number(node.importance.score) || 0)));
}
function nodeColor(node) {
  if (layerMode && ["communities", "roles", "flow"].includes(layerMode.value)) {
    return groupColor(node);
  }
  if (node.kind === "directory") return colors.directory;
  return colors[node.care_level] || colors.medium;
}
function stableHash(value) {
  let hash = 0;
  const text = String(value || "");
  for (let idx = 0; idx < text.length; idx++) {
    hash = ((hash << 5) - hash + text.charCodeAt(idx)) | 0;
  }
  return Math.abs(hash);
}
function communityKey(node) {
  if (!node) return "repo";
  if (node.path === ".") return "repo";
  const source = node.kind === "directory" ? node.path : (node.directory || node.path || "");
  const top = String(source || "").split("/").filter(Boolean)[0];
  return top || node.role || "repo";
}
function roleGroupKey(node) {
  if (!node) return "other";
  if (node.path === ".") return "repo";
  if (node.kind === "directory") return "directories";
  return node.role || node.file_type || "source";
}
function flowGroupKey(node) {
  if (!node) return "support";
  if (node.path === ".") return "structure";
  if (node.kind === "directory") return "structure";
  const role = String(node.role || "");
  const path = String(node.path || "");
  if (role === "command" || path.includes("/commands/") || path.endsWith("cli.py")) return "entrypoints";
  if (["config", "locking", "identity", "mcp"].includes(role)) return "foundation";
  if (["pipeline", "parser", "storage"].includes(role) || path.includes("/parsers/")) return "core";
  if (role === "test" || path.startsWith("tests/")) return "verification";
  if (["docs", "config"].includes(node.file_type) || [".md", ".toml", ".json", ".yaml", ".yml"].some(ext => path.endsWith(ext))) return "docs-config";
  if (node.care_level === "critical") return "foundation";
  return "support";
}
function groupKey(node) {
  const mode = layerMode ? layerMode.value : "communities";
  if (mode === "roles") return roleGroupKey(node);
  if (mode === "flow") return flowGroupKey(node);
  return communityKey(node);
}
function communityLabel(key) {
  return key === "repo" ? "repo" : key;
}
function groupLabel(key) {
  const labels = {
    repo: "repo",
    directories: "directories",
    entrypoints: "entrypoints",
    foundation: "foundation",
    core: "core logic",
    verification: "tests",
    "docs-config": "docs/config",
    support: "support"
  };
  return labels[key] || communityLabel(key);
}
function communityColor(nodeOrKey) {
  const key = typeof nodeOrKey === "string" ? nodeOrKey : communityKey(nodeOrKey);
  return communityColors[stableHash(key) % communityColors.length];
}
function groupColor(nodeOrKey) {
  const key = typeof nodeOrKey === "string" ? nodeOrKey : groupKey(nodeOrKey);
  return communityColors[stableHash(key) % communityColors.length];
}
function groupCounts() {
  const counts = new Map();
  const mode = layerMode ? layerMode.value : "communities";
  nodes.forEach(node => {
    if (mode !== "flow" && node.kind !== "file") return;
    const key = groupKey(node);
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  return counts;
}
function communityCounts() {
  return groupCounts();
}
function flowKeyOrder() {
  return ["structure", "entrypoints", "foundation", "core", "support", "verification", "docs-config"];
}
function groupKeys() {
  const counts = groupCounts();
  if (layerMode && layerMode.value === "flow") {
    const present = new Set(nodes.map(flowGroupKey));
    return flowKeyOrder().filter(key => present.has(key));
  }
  const keys = [...counts.keys()].sort((a, b) => {
    const delta = (counts.get(b) || 0) - (counts.get(a) || 0);
    return delta || a.localeCompare(b);
  });
  return keys.length ? keys : ["repo"];
}
function communityKeys() {
  return groupKeys();
}
function communityAnchors(width, height) {
  const keys = groupKeys();
  const anchors = new Map();
  if (layerMode && layerMode.value === "roles") {
    const columns = Math.max(2, Math.ceil(Math.sqrt(keys.length || 1)));
    const rows = Math.max(1, Math.ceil((keys.length || 1) / columns));
    keys.forEach((key, index) => {
      const column = index % columns;
      const row = Math.floor(index / columns);
      anchors.set(key, {
        x: width * ((column + 1) / (columns + 1)),
        y: height * ((row + 1) / (rows + 1))
      });
    });
    anchors.set("repo", { x: width * 0.5, y: height * 0.5 });
    return anchors;
  }
  if (layerMode && layerMode.value === "flow") {
    const order = flowKeyOrder().filter(key => keys.includes(key));
    order.forEach((key, index) => {
      anchors.set(key, {
        x: width * ((index + 1) / (order.length + 1)),
        y: height * 0.48
      });
    });
    anchors.set("repo", anchors.get("structure") || { x: width * 0.12, y: height * 0.5 });
    return anchors;
  }
  const centerX = width * 0.52;
  const centerY = height * 0.52;
  const rx = Math.max(180, width * 0.28);
  const ry = Math.max(120, height * 0.24);
  keys.forEach((key, index) => {
    const angle = (index / Math.max(1, keys.length)) * Math.PI * 2 - Math.PI / 2;
    anchors.set(key, {
      x: centerX + Math.cos(angle) * rx,
      y: centerY + Math.sin(angle) * ry
    });
  });
  anchors.set("repo", { x: width * 0.5, y: height * 0.5 });
  return anchors;
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
function directoryNodeId(path) {
  return `dir:${path || "."}`;
}
function fileNodeId(path) {
  return `file:${path}`;
}
function directoryDepth(path) {
  if (!path || path === ".") return 0;
  return path.split("/").filter(Boolean).length;
}
function ancestorDirectoryIdsForNode(node) {
  if (!node) return [directoryNodeId(".")];
  const directory = node.kind === "file" ? node.directory : node.directory;
  const pieces = !directory || directory === "." ? [] : directory.split("/");
  const ids = [directoryNodeId(".")];
  let current = [];
  pieces.forEach(part => {
    current.push(part);
    ids.push(directoryNodeId(current.join("/")));
  });
  return ids;
}
function ensureParentDirectoriesExpanded(node) {
  ancestorDirectoryIdsForNode(node).forEach(id => expandedDirs.add(id));
}
function pruneExpandedDirs() {
  expandedDirs = new Set([...expandedDirs].filter(id => id === "dir:." || nodeById.has(id)));
  expandedDirs.add("dir:.");
}
function childTreeNodes(directoryPath) {
  return nodes
    .filter(node => {
      if (node.id === "dir:.") return false;
      if (node.kind === "directory") return node.directory === directoryPath;
      return node.kind === "file" && node.directory === directoryPath;
    })
    .sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
      return a.label.localeCompare(b.label) || a.path.localeCompare(b.path);
    });
}
function treeRows() {
  const q = searchInput.value.trim().toLowerCase();
  if (q) {
    return nodes
      .filter(node => (node.kind === "directory" || node.kind === "file") && searchableText(node).includes(q))
      .sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
        return a.path.localeCompare(b.path);
      })
      .slice(0, 120)
      .map(node => ({
        node,
        depth: Math.min(5, node.kind === "file" ? directoryDepth(node.directory) + 1 : directoryDepth(node.path)),
        searching: true
      }));
  }
  const root = nodeById.get("dir:.");
  const rows = [];
  function visit(node, depth) {
    rows.push({ node, depth, searching: false });
    if (node.kind !== "directory" || !expandedDirs.has(node.id)) return;
    childTreeNodes(node.path).forEach(child => visit(child, depth + 1));
  }
  if (root) visit(root, 0);
  return rows;
}
function neighborIds(node) {
  return neighborhoodContext(node, neighborhoodDepth).ids;
}
function graphAdjacency() {
  if (graphAdjacencyCache) return graphAdjacencyCache;
  const adjacency = new Map();
  function add(from, to) {
    if (!from || !to || from === to) return;
    if (!adjacency.has(from)) adjacency.set(from, new Set());
    adjacency.get(from).add(to);
  }
  edges.forEach(edge => {
    if (edge.kind === "contains") {
      add(edge.target, edge.source);
      const source = nodeById.get(edge.source);
      if (source && source.kind === "directory") add(edge.source, edge.target);
      return;
    }
    add(edge.source, edge.target);
    add(edge.target, edge.source);
  });
  nodes.forEach(node => {
    if (node.kind === "file") {
      parentDirectories(node.path).forEach(id => add(node.id, id));
      const incoming = (node.metrics && node.metrics.incoming_files) || [];
      const outgoing = (node.metrics && node.metrics.outgoing_files) || [];
      incoming.concat(outgoing).forEach(path => add(node.id, fileNodeId(path)));
    } else {
      parentDirectories(node.path).forEach(id => add(node.id, id));
    }
  });
  graphAdjacencyCache = adjacency;
  return adjacency;
}
function neighborhoodContext(node, depth = neighborhoodDepth) {
  if (!node) return { ids: new Set(), distances: new Map() };
  const safeDepth = Math.max(1, Math.min(3, Number(depth) || 1));
  const key = `${node.id}:${safeDepth}:${edges.length}:${nodes.length}`;
  if (neighborhoodCache && neighborhoodCache.key === key) return neighborhoodCache;
  const adjacency = graphAdjacency();
  const ids = new Set([node.id]);
  const distances = new Map([[node.id, 0]]);
  const queue = [{ id: node.id, distance: 0 }];
  while (queue.length && ids.size < 350) {
    const current = queue.shift();
    if (!current || current.distance >= safeDepth) continue;
    const nextIds = adjacency.get(current.id) || new Set();
    nextIds.forEach(nextId => {
      if (!nodeById.has(nextId) || ids.has(nextId) || ids.size >= 350) return;
      ids.add(nextId);
      distances.set(nextId, current.distance + 1);
      queue.push({ id: nextId, distance: current.distance + 1 });
    });
  }
  const result = { key, ids, distances };
  neighborhoodCache = result;
  return result;
}
function selectedNeighborhoodDistance(node) {
  if (!selected || !node) return null;
  const context = neighborhoodContext(selected, neighborhoodDepth);
  return context.distances.has(node.id) ? context.distances.get(node.id) : null;
}
function isSelectedContextNode(node) {
  if (!selected) return false;
  return node.id === selected.id || neighborIds(selected).has(node.id);
}
function passesLayer(node) {
  const mode = layerMode.value;
  if (mode === "all") return true;
  if (node.active_work) return true;
  if (mode === "overview") {
    return node.kind === "directory" || node.care_level === "critical" || (node.importance.rank && node.importance.rank <= 24);
  }
  if (mode === "structure") {
    if (node.path === ".") return true;
    if (node.kind === "directory") {
      return directoryDepth(node.path) <= 2 || node.active_work || (selected && ancestorDirectoryIdsForNode(selected).includes(node.id));
    }
    if (node.active_work || isRecentNode(node) || isSelectedContextNode(node)) return true;
    if (node.care_level === "critical" || node.care_level === "high") return true;
    return Boolean(node.importance.rank && node.importance.rank <= 55);
  }
  if (mode === "communities") {
    if (node.path === ".") return true;
    if (node.kind === "directory") {
      return directoryDepth(node.path) <= 1 || node.active_work || (selected && ancestorDirectoryIdsForNode(selected).includes(node.id));
    }
    if (node.active_work || isRecentNode(node)) return true;
    if (node.care_level === "critical" || node.care_level === "high") return true;
    if (node.importance.rank && node.importance.rank <= 40) return true;
    if (selected && neighborIds(selected).has(node.id)) return true;
    return false;
  }
  if (mode === "roles" || mode === "flow") {
    if (node.path === ".") return true;
    if (node.kind === "directory") return directoryDepth(node.path) <= 1 || node.active_work || isSelectedContextNode(node);
    if (node.active_work || isRecentNode(node) || isSelectedContextNode(node)) return true;
    if (node.care_level === "critical" || node.care_level === "high") return true;
    return Boolean(node.importance.rank && node.importance.rank <= 65);
  }
  if (mode === "layered") {
    if (!selected) return node.kind === "directory" || node.care_level === "critical" || (node.importance.rank && node.importance.rank <= 30);
    return neighborIds(selected).has(node.id);
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
function setViewportTransform() {
  viewport.setAttribute("transform", `translate(${transform.x} ${transform.y}) scale(${transform.k})`);
  scheduleViewStateSave();
}
function computeVisibility() {
  const visibleIds = new Set();
  nodes.forEach(node => {
    node.visible = passesFilter(node);
    if (node.visible) visibleIds.add(node.id);
  });
  return visibleIds;
}
function initPositions() {
  const rect = svg.getBoundingClientRect();
  const width = Math.max(600, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  const anchors = communityAnchors(width, height);
  nodes.forEach((node, index) => {
    const depth = node.path === "." ? 0 : node.path.split("/").length;
    const angle = (index * 2.399963229728653) % (Math.PI * 2);
    const ring = node.kind === "directory" ? 56 + depth * 72 : 190 + (index % 7) * 22;
    const anchor = anchors.get(groupKey(node)) || anchors.get("repo") || { x: width / 2, y: height / 2 };
    node.x = anchor.x + Math.cos(angle) * ring * 0.45;
    node.y = anchor.y + Math.sin(angle) * ring * 0.36;
  });
}
function sortedStructureNodes() {
  return [...nodes].sort((a, b) => {
    const aDepth = a.kind === "file" ? directoryDepth(a.directory) + 1 : directoryDepth(a.path);
    const bDepth = b.kind === "file" ? directoryDepth(b.directory) + 1 : directoryDepth(b.path);
    return aDepth - bDepth || a.path.localeCompare(b.path);
  });
}
function applyStructureLayout() {
  const rect = svg.getBoundingClientRect();
  const rowGap = 34;
  const colGap = 178;
  const top = 72;
  const left = 78;
  sortedStructureNodes().forEach((node, index) => {
    const depth = node.kind === "file" ? directoryDepth(node.directory) + 1 : directoryDepth(node.path);
    const laneOffset = node.kind === "file" ? 34 : 0;
    node.x = left + depth * colGap + laneOffset;
    node.y = top + index * rowGap;
    node.vx = 0;
    node.vy = 0;
  });
  const height = Math.max(420, rect.height || 600);
  const visible = nodes.filter(node => node.visible !== false);
  if (visible.length && visible.length < nodes.length) {
    const minY = Math.min(...visible.map(node => node.y));
    const maxY = Math.max(...visible.map(node => node.y));
    const shift = height * 0.5 - (minY + maxY) * 0.5;
    visible.forEach(node => { node.y += shift; });
  }
}
function applyFlowLayout() {
  const rect = svg.getBoundingClientRect();
  const width = Math.max(700, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  const anchors = communityAnchors(width, height);
  const buckets = new Map();
  nodes.forEach(node => {
    const key = flowGroupKey(node);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(node);
  });
  buckets.forEach((bucket, key) => {
    bucket.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
      const rankA = Number((a.importance && a.importance.rank) || 9999);
      const rankB = Number((b.importance && b.importance.rank) || 9999);
      return rankA - rankB || a.path.localeCompare(b.path);
    });
    const anchor = anchors.get(key) || { x: width * 0.5, y: height * 0.5 };
    const visibleCount = bucket.filter(node => node.visible !== false).length || bucket.length || 1;
    const gap = Math.max(26, Math.min(46, (height * 0.72) / Math.max(1, visibleCount)));
    let visibleIndex = 0;
    bucket.forEach((node, index) => {
      const row = node.visible === false ? index : visibleIndex++;
      const offset = (row - (visibleCount - 1) / 2) * gap;
      const jitter = ((stableHash(node.id) % 19) - 9) * 0.8;
      node.x = anchor.x + jitter;
      node.y = anchor.y + offset;
      node.vx = 0;
      node.vy = 0;
    });
  });
}
function applyLayeredLayout() {
  if (!selected) {
    applyFlowLayout();
    return;
  }
  const rect = svg.getBoundingClientRect();
  const width = Math.max(700, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  const context = neighborhoodContext(selected, neighborhoodDepth);
  const maxDistance = Math.max(1, ...context.distances.values());
  const left = Math.max(82, width * 0.12);
  const right = Math.max(left + 120, width * 0.9);
  const buckets = new Map();
  nodes.forEach(node => {
    if (node.visible === false) return;
    const distance = context.distances.get(node.id);
    if (distance === undefined) return;
    if (!buckets.has(distance)) buckets.set(distance, []);
    buckets.get(distance).push(node);
  });
  for (let distance = 0; distance <= maxDistance; distance++) {
    const bucket = buckets.get(distance) || [];
    bucket.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
      const careRank = { critical: 0, high: 1, medium: 2, low: 3 };
      const careDelta = (careRank[a.care_level] ?? 4) - (careRank[b.care_level] ?? 4);
      if (careDelta) return careDelta;
      const rankA = Number((a.importance && a.importance.rank) || 9999);
      const rankB = Number((b.importance && b.importance.rank) || 9999);
      return rankA - rankB || a.path.localeCompare(b.path);
    });
    const x = maxDistance === 0 ? width * 0.5 : left + ((right - left) * distance) / maxDistance;
    const gap = Math.max(30, Math.min(58, (height * 0.76) / Math.max(1, bucket.length)));
    bucket.forEach((node, index) => {
      const offset = (index - (bucket.length - 1) / 2) * gap;
      node.x = x + ((stableHash(node.id) % 15) - 7) * 0.7;
      node.y = height * 0.5 + offset;
      node.vx = 0;
      node.vy = 0;
    });
  }
}
function seedMissingPositions() {
  const rect = svg.getBoundingClientRect();
  const width = Math.max(600, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  const positioned = nodes.filter(node => node.x || node.y);
  if (!positioned.length) {
    initPositions();
    return;
  }
  const anchor = selected || positioned[0];
  const anchors = communityAnchors(width, height);
  nodes.forEach((node, index) => {
    if (node.x || node.y) return;
    const angle = (index * 2.399963229728653) % (Math.PI * 2);
    const radius = node.kind === "directory" ? 90 : 140;
    const communityAnchor = anchors.get(groupKey(node));
    const base = communityAnchor || anchor || { x: width / 2, y: height / 2 };
    node.x = base.x + Math.cos(angle) * radius;
    node.y = base.y + Math.sin(angle) * radius * 0.75;
  });
}
function tickSimulation(iterations = 1) {
  if (layerMode && layerMode.value === "structure") {
    computeVisibility();
    applyStructureLayout();
    return;
  }
  if (layerMode && layerMode.value === "flow") {
    computeVisibility();
    applyFlowLayout();
    return;
  }
  if (layerMode && layerMode.value === "layered") {
    computeVisibility();
    applyLayeredLayout();
    return;
  }
  const rect = svg.getBoundingClientRect();
  const width = Math.max(600, rect.width || 900);
  const height = Math.max(420, rect.height || 600);
  const useGroups = layerMode && ["communities", "roles"].includes(layerMode.value);
  const anchors = useGroups ? communityAnchors(width, height) : null;
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
      const communityAnchor = anchors ? anchors.get(groupKey(node)) : null;
      const anchorX = communityAnchor ? communityAnchor.x : (node.kind === "directory" ? width * 0.45 : width * 0.52);
      const anchorY = communityAnchor ? communityAnchor.y : height * 0.5;
      const strength = communityAnchor ? 0.0024 : 0.0008;
      node.vx += (anchorX - node.x) * strength;
      node.vy += (anchorY - node.y) * strength;
      node.vx *= 0.82;
      node.vy *= 0.82;
      node.x += node.vx;
      node.y += node.vy;
    });
  }
}
function renderActivityTrailEdges() {
  document.querySelectorAll(".edge.activity").forEach(edge => edge.remove());
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
}
"""

__all__ = ["GRAPH_SCRIPT_LAYOUT"]
