"""Activity JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_ACTIVITY = r"""function loadNotes() {
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
function graphPostHeaders() {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem(graphTokenKey);
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}
function syncGraphTokenFromUrl() {
  try {
    const current = new URL(window.location.href);
    let changed = false;
    ["token", "graph_token", "access_token"].forEach((name) => {
      if (current.searchParams.has(name)) {
        current.searchParams.delete(name);
        changed = true;
      }
    });
    if (changed) {
      window.history.replaceState(
        {},
        document.title,
        `${current.pathname}${current.search}${current.hash}`
      );
    }
  } catch (_err) {
    // URL cleanup should not block static graph use.
  }
}
function graphGetHeaders(extra = {}) {
  const headers = { ...extra };
  const token = localStorage.getItem(graphTokenKey);
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}
function graphNetworkErrorMessage() {
  const oldPortHint = window.location.port === "8768"
    ? " This repo now serves the graph on http://127.0.0.1:8767/repo-graph.html."
    : "";
  return `Graph server is unreachable from this page.${oldPortHint} Refresh the live graph page and try again.`;
}
async function establishGraphBrowserSession(token) {
  const response = await fetch("/api/auth/browser-session", {
    method: "POST",
    credentials: "same-origin",
    headers: { Authorization: `Bearer ${token}` }
  });
  if (!response.ok) throw new Error("Invalid graph server token");
  return response;
}
async function fetchGraphPost(url, payload) {
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: graphPostHeaders(),
      credentials: "same-origin",
      body: JSON.stringify(payload)
    });
  } catch (_err) {
    throw new Error(graphNetworkErrorMessage());
  }
  if (response.status !== 401) return response;
  const token = window.prompt("Graph server token");
  if (!token) return response;
  const trimmedToken = token.trim();
  await establishGraphBrowserSession(trimmedToken);
  localStorage.setItem(graphTokenKey, trimmedToken);
  try {
    return await fetch(url, {
      method: "POST",
      headers: graphPostHeaders(),
      credentials: "same-origin",
      body: JSON.stringify(payload)
    });
  } catch (_err) {
    throw new Error(graphNetworkErrorMessage());
  }
}
async function fetchGraphGet(url, options = {}) {
  const headers = graphGetHeaders(options.headers || {});
  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers,
      credentials: "same-origin",
      cache: options.cache || "no-store"
    });
  } catch (_err) {
    throw new Error(graphNetworkErrorMessage());
  }
  if (response.status !== 401) return response;
  const token = window.prompt("Graph server token");
  if (!token) return response;
  const trimmedToken = token.trim();
  await establishGraphBrowserSession(trimmedToken);
  localStorage.setItem(graphTokenKey, trimmedToken);
  try {
    return await fetch(url, {
      ...options,
      headers: graphGetHeaders(options.headers || {}),
      credentials: "same-origin",
      cache: options.cache || "no-store"
    });
  } catch (_err) {
    throw new Error(graphNetworkErrorMessage());
  }
}
async function postNoteToServer(node, note) {
  if (!canPostToGraphServer()) return;
  try {
    await fetchGraphPost((data.live && data.live.notes_path) || "/api/notes", {
      node_id: node.id,
      path: node.path,
      node_kind: node.kind,
      care_level: node.care_level,
      summary: node.summary,
      note: note.note || ""
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
  return agentTaskPayload(node, noteText(node));
}
function agentTaskPayload(node, message, options = {}) {
  const selectedPaths = node.kind === "file"
    ? [node.path]
    : uniquePaths((node.metrics && ((node.metrics.active_files || []).concat(node.metrics.recent_files || []))) || []);
  const provider = String(options.provider ?? defaultChatProvider()).trim().toLowerCase();
  const executionStrategy = String(options.executionStrategy || options.execution_strategy || "").trim().toLowerCase();
  const payload = {
    kind: "code_index_graph_agent_task",
    root: data.root,
    created_at: new Date().toISOString(),
    agent_name: options.agentName || agentNameForProvider(provider),
    selected_nodes: [node.id],
    selected_paths: selectedPaths,
    message: message || noteText(node),
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
  if (provider) payload.provider = provider;
  if (executionStrategy && executionStrategy !== "single_agent") {
    payload.execution_strategy = executionStrategy;
    if (executionStrategy === "agent_swarm") {
      payload.swarm = {
        enabled: true,
        provider: provider || "kimi"
      };
    }
  }
  return payload;
}
async function postAgentTaskToServer(payload) {
  if (!canPostToGraphServer()) {
    await copyJson(payload);
    return {
      ok: false,
      copied: true,
      error: "graph-server is not active"
    };
  }
  const preflight = await preflightAgentTask(payload);
  const needsConfirmation = preflight && preflight.preflight && preflight.preflight.requires_confirmation;
  if (needsConfirmation && !payload.preflight_confirmed) {
    return {
      ok: false,
      needs_confirmation: true,
      error: "preflight confirmation required",
      preflight
    };
  }
  payload.preflight = preflight.preflight;
  const response = await fetchGraphPost((data.live && data.live.agent_runs_path) || "/api/agent-runs", payload);
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || `HTTP ${response.status}`);
  }
  result.preflight = preflight;
  return result;
}
async function postAgentMessageToRun(runId, payload) {
  if (!runId) return postAgentTaskToServer(payload);
  if (!canPostToGraphServer()) {
    await copyJson(payload);
    return {
      ok: false,
      copied: true,
      error: "graph-server is not active"
    };
  }
  const preflight = await preflightAgentTask(payload);
  const needsConfirmation = preflight && preflight.preflight && preflight.preflight.requires_confirmation;
  if (needsConfirmation && !payload.preflight_confirmed) {
    return {
      ok: false,
      needs_confirmation: true,
      error: "preflight confirmation required",
      preflight
    };
  }
  payload.preflight = preflight.preflight;
  const response = await fetchGraphPost(`/api/agent-runs/${encodeURIComponent(runId)}/messages`, payload);
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || `HTTP ${response.status}`);
  }
  result.preflight = preflight;
  return result;
}
async function preflightAgentTask(payload) {
  const response = await fetchGraphPost((data.live && data.live.agent_preflight_path) || "/api/agent-task-preflight", payload);
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || `HTTP ${response.status}`);
  }
  return result;
}
function preflightWarningText(result) {
  const warnings = (((result || {}).preflight || {}).preflight || {}).warnings || [];
  if (!warnings.length) return "Preflight clear";
  return warnings.map(warning => warning.message || warning.kind || "preflight warning").join(" ");
}
function applyPreflightConfirmation(payload, button) {
  if (button && button.dataset.preflightConfirmed === "true") {
    payload.preflight_confirmed = true;
  }
  return payload;
}
function handlePreflightResult(result, button, status, label = "Send anyway") {
  if (!result || !result.needs_confirmation) return false;
  if (button) {
    button.disabled = false;
    button.dataset.preflightConfirmed = "true";
    button.textContent = label;
  }
  if (status) status.textContent = preflightWarningText(result);
  return true;
}
function resetPreflightButton(button, label) {
  if (!button) return;
  delete button.dataset.preflightConfirmed;
  button.textContent = label;
}
async function cancelAgentRun(runId, button) {
  if (!runId || !canPostToGraphServer()) return;
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Canceling";
  }
  try {
    const response = await fetchGraphPost(`/api/agent-runs/${encodeURIComponent(runId)}/cancel`, {});
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || `HTTP ${response.status}`);
    }
    applyAgentRunResponse(result);
  } catch (err) {
    if (button) {
      button.disabled = false;
      button.textContent = err.message || original || "Cancel";
      setTimeout(() => { button.textContent = original || "Cancel"; }, 1200);
    }
  }
}
async function archiveAgentRun(runId, button) {
  if (!runId || !canPostToGraphServer()) return;
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Archiving";
  }
  try {
    const response = await fetchGraphPost(`/api/agent-runs/${encodeURIComponent(runId)}/archive`, {});
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || `HTTP ${response.status}`);
    }
    applyAgentRunResponse(result);
  } catch (err) {
    if (button) {
      button.disabled = false;
      button.textContent = err.message || original || "Archive";
      setTimeout(() => { button.textContent = original || "Archive"; }, 1200);
    }
  }
}
async function showRunTranscript(runId, button, options = {}) {
  if (!runId || !canPostToGraphServer()) return;
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "...";
  }
  try {
    const response = await fetchGraphGet(`/api/agent-runs/${encodeURIComponent(runId)}`, {
      headers: { "Accept": "application/json" },
      cache: "no-store"
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || `HTTP ${response.status}`);
    }
    selectedRunTranscript = result;
    activeTab = "summary";
    renderNavigator();
    renderInspector();
    if (options.focusComposer) {
      setTimeout(() => {
        const composer = document.getElementById("run-followup-message");
        if (composer) composer.focus();
      }, 0);
    }
  } catch (err) {
    if (button) {
      button.textContent = err.message || original || "Stream";
      setTimeout(() => { button.textContent = original || "Stream"; }, 1200);
    }
  } finally {
    if (button) {
      button.disabled = false;
      if (button.textContent === "...") button.textContent = original || "Stream";
    }
  }
}
async function fetchServerSearch(query) {
  if (!canPostToGraphServer() || !query.trim()) return;
  const params = new URLSearchParams({
    q: query.trim(),
    scope: "all",
    limit: "10"
  });
  searchResults = {
    query: query.trim(),
    status: "loading",
    files: [],
    transcripts: [],
    counts: {}
  };
  renderNavigator();
  try {
    const searchPath = (data.live && data.live.search_path) || "/api/search";
    const response = await fetchGraphGet(`${searchPath}?${params.toString()}`, {
      headers: { "Accept": "application/json" },
      cache: "no-store"
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (searchInput.value.trim() !== query.trim()) return;
    searchResults = {
      query: result.query || query.trim(),
      status: "ready",
      files: result.files || [],
      transcripts: result.transcripts || [],
      counts: result.counts || {}
    };
  } catch (err) {
    searchResults = {
      query: query.trim(),
      status: "error",
      files: [],
      transcripts: [],
      counts: {},
      error: err.message || "Search failed"
    };
  }
  renderNavigator();
}
function scheduleServerSearch() {
  if (searchTimer) clearTimeout(searchTimer);
  const query = searchInput.value.trim();
  if (!query) {
    searchResults = { query: "", status: "idle", files: [], transcripts: [], counts: {} };
    renderNavigator();
    return;
  }
  if (!canPostToGraphServer() || query.length < 2) return;
  searchTimer = setTimeout(() => {
    searchTimer = null;
    fetchServerSearch(query);
  }, 220);
}
function agentEventToEdit(event) {
  if (!event || !event.file_path) return null;
  const payload = event.payload || {};
  return {
    file_path: event.file_path,
    symbol_path: event.symbol_path || null,
    chunk_type: "agent-event",
    chunk_uid: null,
    timestamp: event.timestamp,
    event_source: `agent:${event.agent_name || "Agent"}`,
    change_type: event.event_type || "activity",
    changed_lines: payload.changed_lines || null,
    diff_summary: event.message || "Agent activity event.",
    run_id: event.run_id,
    agent_name: event.agent_name || "Agent"
  };
}
function editIdentity(edit) {
  return [
    edit.file_path || "",
    edit.timestamp || "",
    edit.change_type || "",
    edit.run_id || "",
    edit.diff_summary || ""
  ].join("|");
}
function mergeRecentEdits(primary, secondary) {
  const seen = new Set();
  const merged = [];
  primary.concat(secondary || []).forEach(edit => {
    if (!edit || !edit.file_path) return;
    const key = editIdentity(edit);
    if (seen.has(key)) return;
    seen.add(key);
    merged.push(edit);
  });
  return merged.sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")));
}
function normalizeRecentFile(item, index) {
  return {
    ...item,
    rank: index + 1,
    edit_count: item.edit_count || item.activity_count || 1
  };
}
function mergeRecentFiles(primary, secondary) {
  const seen = new Set();
  const merged = [];
  primary.concat(secondary || []).forEach(item => {
    if (!item || !item.file_path || seen.has(item.file_path)) return;
    seen.add(item.file_path);
    merged.push(item);
  });
  return merged.slice(0, 8).map(normalizeRecentFile);
}
function updateActivityTrail() {
  const recentFiles = ((data.summary && data.summary.recent_files) || []).slice(0, 8);
  data.activity = data.activity || {};
  data.activity.trail = recentFiles.slice(0, 5).map((item, index, list) => {
    if (!list[index + 1]) return null;
    return { from: item.file_path, to: list[index + 1].file_path };
  }).filter(Boolean);
}
function updateNodeActivityFromData() {
  const activeFiles = new Set((data.agent && data.agent.active_files) || []);
  const recentFiles = ((data.summary && data.summary.recent_files) || []);
  const recentRankByPath = new Map(recentFiles.map(item => [item.file_path, item.rank]));
  const editsByPath = new Map();
  ((data.summary && data.summary.recent_edits) || []).forEach(edit => {
    if (!edit.file_path) return;
    if (!editsByPath.has(edit.file_path)) editsByPath.set(edit.file_path, []);
    const bucket = editsByPath.get(edit.file_path);
    if (bucket.length < 12) bucket.push(edit);
  });
  nodes.forEach(node => {
    if (!node.metrics) node.metrics = {};
    if (node.kind === "file") {
      node.active_work = activeFiles.has(node.path);
      if (editsByPath.has(node.path)) node.recent_edits = editsByPath.get(node.path);
      node.metrics.recent_edit_rank = recentRankByPath.get(node.path) || null;
      if (node.recent_edits && node.recent_edits.length) {
        node.metrics.edit_count = Math.max(Number(node.metrics.edit_count || 0), node.recent_edits.length);
      }
      return;
    }
    const activeInDir = [...activeFiles].filter(path => node.path === "." || path.startsWith(`${node.path}/`));
    const recentInDir = recentFiles
      .map(item => item.file_path)
      .filter(path => node.path === "." || path.startsWith(`${node.path}/`));
    node.active_work = activeInDir.length > 0;
    node.metrics.active_files = activeInDir.slice(0, 8);
    node.metrics.recent_files = recentInDir.slice(0, 8);
    node.metrics.recent_edit_rank = recentInDir.length ? 1 : null;
  });
}
function eventIdentity(event) {
  if (event && event.event_pk !== undefined && event.event_pk !== null) {
    return `pk:${event.event_pk}`;
  }
  return [
    (event && event.run_id) || "",
    (event && event.timestamp) || "",
    (event && event.event_type) || "",
    (event && event.file_path) || "",
    (event && event.message) || ""
  ].join("|");
}
function sortTranscriptEvents(events) {
  return [...events].sort((a, b) => {
    const at = Date.parse(a.timestamp || "") || 0;
    const bt = Date.parse(b.timestamp || "") || 0;
    if (at !== bt) return at - bt;
    return Number(a.event_pk || 0) - Number(b.event_pk || 0);
  });
}
function transcriptFiles(run, events, existing = []) {
  const metadata = (run && run.metadata) || {};
  return uniquePaths(
    (existing || [])
      .concat((run && run.active_files) || [])
      .concat(metadata.selected_paths || [])
      .concat((events || []).map(event => event.file_path).filter(Boolean))
  );
}
function recomputeTranscriptSummary(transcript) {
  const previous = transcript.summary || {};
  const events = transcript.events || [];
  const decisions = events.filter(event => event.event_type === "decision");
  const eventTypes = {};
  const filesTouched = [];
  events.forEach(event => {
    const type = event.event_type || "event";
    eventTypes[type] = (eventTypes[type] || 0) + 1;
    if (event.file_path && !filesTouched.includes(event.file_path)) {
      filesTouched.push(event.file_path);
    }
  });
  return {
    ...previous,
    event_count: Math.max(Number(previous.event_count || 0), events.length),
    included_event_count: events.length,
    truncated: Boolean(previous.truncated && Number(previous.event_count || 0) > events.length),
    decision_count: Math.max(Number(previous.decision_count || 0), decisions.length),
    first_event_at: events.length ? events[0].timestamp : (previous.first_event_at || null),
    last_event_at: events.length ? events[events.length - 1].timestamp : (previous.last_event_at || null),
    event_types: eventTypes,
    files_touched: filesTouched
  };
}
function openRunTranscriptFromResponse(result) {
  if (!result || !result.run) return;
  if (result.transcript && result.transcript.run) {
    selectedRunTranscript = result.transcript;
    activeTab = "summary";
    renderNavigator();
    renderInspector();
    return;
  }
  const events = sortTranscriptEvents(result.event ? [result.event] : []);
  selectedRunTranscript = {
    run: result.run,
    events,
    decisions: events.filter(event => event.event_type === "decision"),
    active_files: transcriptFiles(result.run, events),
    suggestions: result.suggestions || null,
    summary: {}
  };
  selectedRunTranscript.summary = recomputeTranscriptSummary(selectedRunTranscript);
  selectedRunTranscript.summaries = selectedRunTranscript.summary;
  activeTab = "summary";
  renderNavigator();
  renderInspector();
}
function updateSelectedTranscriptFromSnapshot(snapshot) {
  if (!selectedRunTranscript || !selectedRunTranscript.run || !selectedRunTranscript.run.run_id) return false;
  const runId = selectedRunTranscript.run.run_id;
  let changed = false;
  const runs = (((snapshot.agent || {}).active_runs) || []).concat(((snapshot.agent || {}).recent_runs) || []);
  const matchingRun = runs.find(run => run && run.run_id === runId);
  if (matchingRun) {
    selectedRunTranscript.run = {
      ...selectedRunTranscript.run,
      ...matchingRun
    };
    changed = true;
  }
  const nextEvents = (((snapshot.activity || {}).agent_events) || [])
    .filter(event => event && event.run_id === runId);
  if (nextEvents.length) {
    const byIdentity = new Map((selectedRunTranscript.events || []).map(event => [eventIdentity(event), event]));
    nextEvents.forEach(event => byIdentity.set(eventIdentity(event), event));
    selectedRunTranscript.events = sortTranscriptEvents([...byIdentity.values()]);
    selectedRunTranscript.decisions = selectedRunTranscript.events.filter(event => event.event_type === "decision");
    selectedRunTranscript.active_files = transcriptFiles(
      selectedRunTranscript.run,
      selectedRunTranscript.events,
      selectedRunTranscript.active_files || []
    );
    selectedRunTranscript.summary = recomputeTranscriptSummary(selectedRunTranscript);
    selectedRunTranscript.summaries = selectedRunTranscript.summary;
    changed = true;
  }
  return changed;
}
function applyAgentRunResponse(result) {
  if (!result || !result.run) return;
  const run = result.run;
  const agent = data.agent || {};
  const terminal = isTerminalStatus(run.status);
  const archived = Boolean(run.archived_at);
  const activeRuns = uniqueRuns([run].concat(agent.active_runs || [])).filter(item => {
    if (item.run_id === run.run_id) return !terminal && !archived;
    return !isTerminalStatus(item.status) && !item.archived_at;
  });
  const recentRuns = uniqueRuns((archived ? [] : [run]).concat(agent.recent_runs || []))
    .filter(item => !item.archived_at)
    .slice(0, 8);
  const activeFiles = uniquePaths(
    activeRuns.flatMap(item => {
      const metadata = item.metadata || {};
      return (item.active_files || []).concat(metadata.selected_paths || []);
    }).concat(((agent.active_claims || []).map(claim => claim.file_path).filter(Boolean)))
  );
  const events = result.event
    ? [result.event].concat((data.activity && data.activity.agent_events) || [])
    : ((data.activity && data.activity.agent_events) || []);
  handleAgentSnapshot({
    agent: {
      ...agent,
      active_runs: activeRuns,
      recent_runs: recentRuns,
      kanban: result.board || agent.kanban,
      orchestrator: (result.board && result.board.orchestrator) || agent.orchestrator,
      active_files: activeFiles,
      active_agents: uniquePaths(activeRuns.map(item => item.agent_name || "Agent")),
      status: activeRuns.length ? "working" : "idle"
    },
    activity: {
      ...((data.activity) || {}),
      agent_events: events
    }
  });
}
function dynamicEdgeFromRelationship(relationship, index) {
  const sourcePath = String((relationship && relationship.source) || "");
  const targetPath = String((relationship && relationship.target) || "");
  if (!sourcePath || !targetPath || sourcePath === targetPath) return null;
  const source = fileNodeId(sourcePath);
  const target = fileNodeId(targetPath);
  const sourceNode = nodeById.get(source);
  const targetNode = nodeById.get(target);
  if (!sourceNode || !targetNode) return null;
  return {
    id: `edge:agent-live:${index}:${source}:${target}`,
    source,
    target,
    kind: "agent_derived",
    weight: Math.max(1, Number(relationship.observations || 1)),
    label: "agent_derived",
    detail: {
      confidence: relationship.confidence,
      observations: relationship.observations,
      rationale: relationship.rationale || "Agents navigated between these files."
    },
    sourceNode,
    targetNode
  };
}
function mergeDynamicEdges(relationships) {
  if (!Array.isArray(relationships)) return false;
  const nextDynamicEdges = relationships
    .map(dynamicEdgeFromRelationship)
    .filter(Boolean);
  const nextSignature = nextDynamicEdges
    .map(edge => `${edge.source}:${edge.target}:${edge.weight}`)
    .sort()
    .join("|");
  const currentSignature = edges
    .filter(edge => edge.kind === "agent_derived")
    .map(edge => `${edge.source}:${edge.target}:${edge.weight}`)
    .sort()
    .join("|");
  if (nextSignature === currentSignature) return false;
  const serialDynamicEdges = nextDynamicEdges.map(edge => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    kind: edge.kind,
    weight: edge.weight,
    label: edge.label,
    detail: edge.detail
  }));
  edges = edges.filter(edge => edge.kind !== "agent_derived").concat(nextDynamicEdges);
  data.edges = ((data.edges || []).filter(edge => edge.kind !== "agent_derived")).concat(serialDynamicEdges);
  data.summary = data.summary || {};
  data.summary.edge_count = data.edges.length;
  data.summary.relation_edge_count = data.edges.filter(edge => edge.kind !== "contains").length;
  graphAdjacencyCache = null;
  neighborhoodCache = null;
  return true;
}
function handleConnectionSnapshot(payload) {
  if (!payload || typeof payload !== "object") return;
  if (mergeDynamicEdges(payload.derived_relationships || [])) {
    scheduleAgentGraphRefresh();
  }
}
function handleAgentSnapshot(snapshot) {
  if (!snapshot || !snapshot.agent) return;
  data.agent = {
    ...(data.agent || {}),
    ...snapshot.agent
  };
  data.activity = {
    ...(data.activity || {}),
    ...((snapshot.activity) || {})
  };
  updateAgentHeader();
  const eventEdits = (((snapshot.activity || {}).agent_events) || [])
    .map(agentEventToEdit)
    .filter(Boolean);
  data.summary = data.summary || {};
  data.summary.recent_edits = mergeRecentEdits(eventEdits, data.summary.recent_edits || []).slice(0, 50);
  data.summary.recent_files = mergeRecentFiles(
    ((snapshot.activity || {}).agent_recent_files) || [],
    data.summary.recent_files || []
  ).slice(0, 5);
  const transcriptChanged = updateSelectedTranscriptFromSnapshot(snapshot);
  updateActivityTrail();
  updateNodeActivityFromData();
  scheduleAgentGraphRefresh();
  renderNavigator();
  const terminalOpen = !!document.getElementById("terminal-stream-body");
  if (selectedRunTranscript && terminalOpen) {
    if (transcriptChanged) scheduleTerminalSync(false);
    return;
  }
  if (!isTypingTarget(document.activeElement)) {
    renderInspector();
  }
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
"""

__all__ = ["GRAPH_SCRIPT_ACTIVITY"]
