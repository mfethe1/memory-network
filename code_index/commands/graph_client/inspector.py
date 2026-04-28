"""Inspector JavaScript for the graph client."""

from __future__ import annotations


GRAPH_SCRIPT_INSPECTOR = r"""function pill(text) {
  return `<span class="pill">${escapeHtml(text)}</span>`;
}
function renderInspector() {
  if (selectedRunTranscript) {
    renderRunTranscriptInspector(selectedRunTranscript);
    return;
  }
  panelBody.classList.remove("terminal-view");
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
  if (tabChat) tabChat.classList.toggle("active", activeTab === "chat");
  tabEdits.classList.toggle("active", activeTab === "edits");
  tabNotes.classList.toggle("active", activeTab === "notes");
  tabCode.classList.toggle("active", activeTab === "code");
  if (tabDebug) tabDebug.classList.toggle("active", activeTab === "debug");
  if (activeTab === "code") {
    panelBody.innerHTML = renderCode(selected);
  } else if (activeTab === "debug") {
    panelBody.innerHTML = renderDebug(selected);
    bindDebugPanel();
  } else if (activeTab === "chat") {
    panelBody.innerHTML = renderChat(selected);
    bindChatPanel(selected);
  } else if (activeTab === "edits") {
    panelBody.innerHTML = renderEdits(selected);
  } else if (activeTab === "notes") {
    panelBody.innerHTML = renderNotes(selected);
    bindNotesPanel(selected);
  } else {
    panelBody.innerHTML = renderSummary(selected);
  }
}
function renderRunTranscriptInspector(transcript) {
  nodeKind.textContent = "Agent Run";
  updateRunTranscriptHeader(transcript);
  tabSummary.classList.add("active");
  if (tabChat) tabChat.classList.remove("active");
  tabEdits.classList.remove("active");
  tabNotes.classList.remove("active");
  tabCode.classList.remove("active");
  if (tabDebug) tabDebug.classList.remove("active");
  panelBody.classList.add("terminal-view");
  panelBody.innerHTML = renderRunTranscript(transcript);
  bindRunTranscriptPanel(transcript);
}
function updateRunTranscriptHeader(transcript) {
  const run = (transcript && transcript.run) || {};
  const summary = (transcript && transcript.summary) || {};
  nodeTitle.textContent = run.prompt || run.run_id || "Run";
  nodeMeta.innerHTML = [
    run.agent_name || "Agent",
    run.status || "working",
    `${summary.included_event_count || 0}/${summary.event_count || 0} events`,
    `${summary.decision_count || 0} decisions`
  ].filter(Boolean).map(pill).join("");
}
function streamMeta(event, run) {
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  return [
    event.timestamp || "",
    event.agent_name || run.agent_name || "Agent",
    event.event_type || "event",
    payload.stream || "",
    event.file_path || event.symbol_path || ""
  ].filter(Boolean).join(" · ");
}
function renderStreamEvent(event, run) {
  const message = event.message || "";
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  const streamClass = payload.stream ? ` stream-${String(payload.stream).toLowerCase()}` : "";
  const eventKey = typeof eventIdentity === "function" ? eventIdentity(event) : `${event.timestamp || ""}|${event.message || ""}`;
  const body = event.event_type === "tool"
    ? `<pre>${escapeHtml(message)}</pre>`
    : `<div class="stream-message">${escapeHtml(message || "No message recorded.")}</div>`;
  return `
    <div class="stream-line${streamClass}" data-event-key="${escapeHtml(eventKey)}">
      <div class="stream-meta">${escapeHtml(streamMeta(event, run))}</div>
      ${body}
    </div>
  `;
}
function transcriptStreamEvents(transcript) {
  return ((transcript && transcript.events) || [])
    .filter(event => streamEventTypes.has(String(event.event_type || "").toLowerCase()));
}
function renderTerminalRows(transcript) {
  const run = (transcript && transcript.run) || {};
  const events = transcriptStreamEvents(transcript);
  if (!events.length) {
    return `<p class="empty">No terminal output recorded for this run yet.</p>`;
  }
  return events.map(event => renderStreamEvent(event, run)).join("") + `<div class="terminal-cursor" aria-hidden="true"></div>`;
}
function terminalSignature(transcript) {
  return transcriptStreamEvents(transcript).map(eventIdentity).join("|");
}
function appendTerminalRows(body, transcript) {
  const run = (transcript && transcript.run) || {};
  const events = transcriptStreamEvents(transcript);
  if (!events.length) {
    body.innerHTML = `<p class="empty">No terminal output recorded for this run yet.</p>`;
    return;
  }
  if (!body.querySelector(".terminal-cursor")) {
    body.innerHTML = `<div class="terminal-cursor" aria-hidden="true"></div>`;
  }
  const existing = new Set(
    [...body.querySelectorAll(".stream-line")]
      .map(line => line.dataset.eventKey)
      .filter(Boolean)
  );
  let cursor = body.querySelector(".terminal-cursor");
  events.forEach(event => {
    const key = eventIdentity(event);
    if (existing.has(key)) return;
    cursor.insertAdjacentHTML("beforebegin", renderStreamEvent(event, run));
    existing.add(key);
  });
}
function scrollTerminalToBottom() {
  const body = document.getElementById("terminal-stream-body");
  if (!body) return;
  body.scrollTop = body.scrollHeight;
}
function scheduleTerminalSync(forceScroll = false) {
  terminalForceScroll = terminalForceScroll || forceScroll;
  if (terminalRenderFrame) return;
  terminalRenderFrame = requestAnimationFrame(() => {
    terminalRenderFrame = null;
    syncTerminalPanel(terminalForceScroll);
    terminalForceScroll = false;
  });
}
function syncTerminalPanel(forceScroll = false) {
  const body = document.getElementById("terminal-stream-body");
  if (!body || !selectedRunTranscript) return false;
  const signature = terminalSignature(selectedRunTranscript);
  const previousSignature = terminalLastSignature;
  if (signature !== terminalLastSignature) {
    appendTerminalRows(body, selectedRunTranscript);
    terminalLastSignature = signature;
  }
  updateRunTranscriptHeader(selectedRunTranscript);
  if (forceScroll || signature !== previousSignature) {
    scrollTerminalToBottom();
  }
  return true;
}
function bindSubmitOnEnter(messageBox, sendButton) {
  if (!messageBox || !sendButton) return;
  messageBox.addEventListener("keydown", event => {
    if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) {
      return;
    }
    event.preventDefault();
    if (!sendButton.disabled) sendButton.click();
  });
}
function renderRunTranscript(transcript) {
  const run = transcript.run || {};
  const events = transcript.events || [];
  const decisions = transcript.decisions || [];
  const files = transcript.active_files || [];
  const suggestionItems = ((transcript.suggestions && transcript.suggestions.suggestions) || []);
  const summary = transcript.summary || {};
  const decisionRows = decisions.length
    ? decisions.slice(0, 8).map(event => `
        <li>${escapeHtml((event.payload && event.payload.decision) || event.message || "decision")}</li>
      `).join("")
    : `<li>No decisions recorded.</li>`;
  const fileRows = files.length
    ? files.slice(0, 12).map(path => `<li>${escapeHtml(path)}</li>`).join("")
    : `<li>No active files reported.</li>`;
  const suggestionRows = suggestionItems.length
    ? suggestionItems.slice(0, 8).map(item => {
        const command = item.command ? ` <code>${escapeHtml(item.command)}</code>` : "";
        return `<li>${escapeHtml(item.message || item.kind || "suggestion")}${command}</li>`;
      }).join("")
    : `<li>No post-run suggestions yet.</li>`;
  const runFacts = [
    `status ${run.status || "working"}`,
    `${summary.included_event_count || 0}/${summary.event_count || 0} events`,
    files.length ? `${files.length} file(s)` : "no files",
    decisions.length ? `${decisions.length} decision(s)` : null
  ].filter(Boolean).join(" · ");
  const eventRows = events.length
    ? events.map(event => {
        const target = event.file_path || event.symbol_path || "";
        const targetText = target ? ` ${target}` : "";
        return `
          <div class="edit-item">
            <strong>${escapeHtml(event.event_type || "event")}${escapeHtml(targetText)}</strong>
            <span>${escapeHtml(event.timestamp || "")} · ${escapeHtml(event.agent_name || run.agent_name || "Agent")}</span>
            <div>${escapeHtml(event.message || "")}</div>
          </div>
        `;
      }).join("")
    : `<p class="empty">No transcript events recorded.</p>`;
  return `
    <div class="terminal-shell">
      <div class="terminal-bar">
        <span>${escapeHtml(defaultProviderForRun(run).toUpperCase())}</span>
        <span>${escapeHtml(runFacts)}</span>
      </div>
      <details class="terminal-context">
        <summary>${escapeHtml(run.run_id || "run")} · ${escapeHtml(run.started_at || "")}</summary>
        <div class="terminal-context-grid">
          <div><strong>Active Files</strong><ul class="compact">${fileRows}</ul></div>
          <div><strong>Suggestions</strong><ul class="compact">${suggestionRows}</ul></div>
          <div><strong>Decisions</strong><ul class="compact">${decisionRows}</ul></div>
        </div>
      </details>
      <div class="stream-list terminal-body" id="terminal-stream-body" data-run-id="${escapeHtml(run.run_id || "")}">${renderTerminalRows(transcript)}</div>
      <div class="terminal-composer">
        <div class="chat-controls terminal-target">
          <label>
            <span>Target</span>
            <select id="run-followup-provider">
              <option value="codex">Codex CLI</option>
              <option value="claude">Claude CLI</option>
              <option value="configured">Configured adapter</option>
            </select>
          </label>
        </div>
        <textarea class="note-box chat-box terminal-input" id="run-followup-message" placeholder="$ Send a follow-up with this run's files and graph context."></textarea>
        <div class="actions">
          <button class="small-button primary-action" id="send-run-followup" type="button"${canPostToGraphServer() ? "" : " disabled aria-disabled=\"true\""}>Send follow-up</button>
          <span class="inline-status" id="run-followup-status">${canPostToGraphServer() ? "Ready" : "Graph server required"}</span>
        </div>
      </div>
    </div>
  `;
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
function debugNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}
function sanitizeDebugValue(value) {
  const blockedKeys = new Set([
    "token",
    "graph_token",
    "access_token",
    "authorization",
    "fence_token",
    "lease_token",
    "bearer_token",
    "session_cookie"
  ]);
  if (Array.isArray(value)) return value.map(item => sanitizeDebugValue(item));
  if (value && typeof value === "object") {
    const clean = {};
    Object.entries(value).forEach(([key, item]) => {
      if (blockedKeys.has(String(key).toLowerCase())) return;
      clean[key] = sanitizeDebugValue(item);
    });
    return clean;
  }
  return value;
}
function opsFromPerfTick(tick) {
  const counters = (tick && tick.counters) || {};
  const retrieval = counters.retrieval_budget && typeof counters.retrieval_budget === "object"
    ? counters.retrieval_budget
    : {};
  return {
    preflight: { rejections: counters.preflight_rejections || {} },
    auth: { failures: counters.auth_failures || {} },
    claims: {
      active_count: 0,
      conflict_count: debugNumber(counters.claim_conflicts),
      active: []
    },
    sse: { dropped_events: debugNumber(counters.sse_dropped_events) },
    runs: {
      stale_after_seconds: null,
      stale_count: debugNumber(counters.stale_runs),
      stale: []
    },
    search: { latency_ms: counters.search_latency_ms || {} },
    retrieval_budget: {
      broker_configured: !!retrieval.broker_configured,
      requests: debugNumber(retrieval.requests),
      budget_rejections: debugNumber(retrieval.budget_rejections),
      placeholder: true,
      note: "Perf tick carries counters only; fetch server debug for run and claim details."
    }
  };
}
function debugViewSnapshot() {
  if (debugSnapshot) return debugSnapshot;
  if (!debugPerfTick) return null;
  return {
    kind: "code_index_graph_debug_perf_tick",
    generated_at: debugPerfTick.generated_at,
    perf: debugPerfTick,
    ops: opsFromPerfTick(debugPerfTick)
  };
}
function debugObjectEntries(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value).filter(([key]) => key !== "");
}
function renderOpsList(items, emptyText) {
  if (!items.length) return `<p class="empty">${escapeHtml(emptyText)}</p>`;
  return `<ul class="ops-list">${items.map(item => `
    <li>
      <span>${escapeHtml(item.label)}</span>
      <strong>${escapeHtml(item.value)}</strong>
    </li>
  `).join("")}</ul>`;
}
function renderOpsBucket(bucket, emptyText) {
  const rows = debugObjectEntries(bucket)
    .sort((a, b) => debugNumber(b[1]) - debugNumber(a[1]))
    .map(([label, value]) => ({ label, value }));
  return renderOpsList(rows, emptyText);
}
function renderOpsCard(title, active, status, body, extraClass = "") {
  const className = ["ops-card", active ? "active" : "quiet", extraClass].filter(Boolean).join(" ");
  return `
    <div class="${escapeHtml(className)}">
      <div class="ops-card-head">
        <h4>${escapeHtml(title)}</h4>
        <span>${escapeHtml(status)}</span>
      </div>
      ${body}
    </div>
  `;
}
function renderClaimOps(ops) {
  const claims = (ops.claims && ops.claims.active) || [];
  const conflictCount = debugNumber(ops.claims && ops.claims.conflict_count);
  const activeCount = debugNumber((ops.claims && ops.claims.active_count) || claims.length);
  const rows = claims.slice(0, 8).map(claim => ({
    label: claim.file_path || claim.claim_id || "claim",
    value: [claim.agent_name || "Agent", claim.mode || "claim", claim.run_status || claim.status || ""]
      .filter(Boolean)
      .join(" · ")
  }));
  const body = `
    ${renderOpsList([
      { label: "Conflicts", value: conflictCount },
      { label: "Active claims", value: activeCount }
    ], "No claim activity recorded.")}
    ${rows.length ? renderOpsList(rows, "No active claims.") : `<p class="empty">No active claims.</p>`}
  `;
  return renderOpsCard(
    "Claim Conflicts",
    conflictCount > 0 || activeCount > 0,
    conflictCount > 0 ? `${conflictCount} conflict${conflictCount === 1 ? "" : "s"}` : "No conflicts",
    body,
    conflictCount > 0 ? "warn" : ""
  );
}
function renderStaleRunOps(ops) {
  const runs = (ops.runs && ops.runs.stale) || [];
  const staleCount = debugNumber((ops.runs && ops.runs.stale_count) || runs.length);
  const rows = runs.slice(0, 8).map(run => ({
    label: run.run_id || "run",
    value: [run.agent_name || "Agent", run.status || "working", run.updated_at || ""]
      .filter(Boolean)
      .join(" · ")
  }));
  const staleAfter = ops.runs && ops.runs.stale_after_seconds
    ? `after ${ops.runs.stale_after_seconds}s`
    : "counter only";
  const body = rows.length
    ? renderOpsList(rows, "No stale runs.")
    : `<p class="empty">No stale runs detected.</p>`;
  return renderOpsCard(
    "Stale Runs",
    staleCount > 0,
    staleCount > 0 ? `${staleCount} stale · ${staleAfter}` : "Fresh",
    body,
    staleCount > 0 ? "warn" : ""
  );
}
function renderSearchLatencyOps(ops) {
  const latency = (ops.search && ops.search.latency_ms) || {};
  const count = debugNumber(latency.count);
  const rows = [
    { label: "Samples", value: count },
    { label: "Last", value: latency.last == null ? "n/a" : `${latency.last} ms` },
    { label: "Average", value: latency.avg == null ? "n/a" : `${latency.avg} ms` },
    { label: "Max", value: latency.max == null ? "n/a" : `${latency.max} ms` }
  ];
  const scopeRows = debugObjectEntries(latency.by_scope || {}).map(([scope, scoped]) => ({
    label: `scope ${scope}`,
    value: scoped && typeof scoped === "object"
      ? `${debugNumber(scoped.count)} · avg ${scoped.avg == null ? "n/a" : `${scoped.avg} ms`}`
      : scoped
  }));
  return renderOpsCard(
    "Search Latency",
    count > 0,
    count > 0 ? `${count} sample${count === 1 ? "" : "s"}` : "No searches",
    count > 0
      ? renderOpsList(rows.concat(scopeRows), "No search latency samples.")
      : `<p class="empty">No server searches recorded yet.</p>`
  );
}
function renderRetrievalBudgetOps(ops) {
  const budget = (ops && ops.retrieval_budget) || {};
  const requests = debugNumber(budget.requests);
  const rejections = debugNumber(budget.budget_rejections);
  const configured = !!budget.broker_configured;
  const body = `
    ${renderOpsList([
      { label: "Broker", value: configured ? "configured" : "not configured" },
      { label: "Requests", value: requests },
      { label: "Budget rejections", value: rejections }
    ], "No retrieval budget counters.")}
    <p class="ops-note">${escapeHtml(budget.note || (budget.placeholder ? "Retrieval budget broker readiness placeholder." : ""))}</p>
  `;
  return renderOpsCard(
    "Retrieval Budget",
    configured || requests > 0 || rejections > 0,
    configured ? "Ready" : "Not wired",
    body,
    rejections > 0 ? "warn" : ""
  );
}
function renderOpsPanel(snapshot) {
  if (!snapshot) {
    return `
      <div class="ops-empty">
        <strong>No server ops snapshot loaded</strong>
        <span>Fetch server debug to render auth failures, preflight rejections, claim conflicts, SSE drops, stale runs, search latency, and retrieval budget readiness.</span>
      </div>
    `;
  }
  const ops = snapshot.ops || {};
  const authFailures = (ops.auth && ops.auth.failures) || {};
  const preflightRejections = (ops.preflight && ops.preflight.rejections) || {};
  const sseDrops = debugNumber(ops.sse && ops.sse.dropped_events);
  return `
    <div class="ops-grid">
      ${renderOpsCard(
        "Auth Failures",
        debugObjectEntries(authFailures).length > 0,
        debugObjectEntries(authFailures).length ? "Failures recorded" : "Clean",
        renderOpsBucket(authFailures, "No auth failures recorded."),
        debugObjectEntries(authFailures).length ? "warn" : ""
      )}
      ${renderOpsCard(
        "Preflight Rejections",
        debugObjectEntries(preflightRejections).length > 0,
        debugObjectEntries(preflightRejections).length ? "Rejected requests" : "Clear",
        renderOpsBucket(preflightRejections, "No preflight rejections recorded."),
        debugObjectEntries(preflightRejections).length ? "warn" : ""
      )}
      ${renderClaimOps(ops)}
      ${renderOpsCard(
        "SSE Drops",
        sseDrops > 0,
        sseDrops > 0 ? `${sseDrops} drop${sseDrops === 1 ? "" : "s"}` : "Connected",
        sseDrops > 0
          ? renderOpsList([{ label: "Dropped events", value: sseDrops }], "No SSE drops recorded.")
          : `<p class="empty">No dropped SSE connections recorded.</p>`,
        sseDrops > 0 ? "warn" : ""
      )}
      ${renderStaleRunOps(ops)}
      ${renderSearchLatencyOps(ops)}
      ${renderRetrievalBudgetOps(ops)}
    </div>
  `;
}
function handlePerfTick(tick) {
  debugPerfTick = sanitizeDebugValue(tick || {});
  if (activeTab === "debug" && !selectedRunTranscript) {
    renderInspector();
  }
}
function renderDebug(node) {
  const summary = data.summary || {};
  const agent = data.agent || {};
  const activity = data.activity || {};
  const snapshot = debugViewSnapshot();
  const selectedFacts = node ? [
    `selected ${node.id}`,
    `care ${node.care_level || "n/a"}`,
    `incoming ${(node.metrics && node.metrics.incoming_relations) || 0}`,
    `outgoing ${(node.metrics && node.metrics.outgoing_relations) || 0}`
  ].join(" · ") : "No selected node";
  const local = {
    client: clientMetrics,
    graph: {
      generated_at: data.generated_at,
      payload_chars: clientMetrics.payload_chars,
      node_count: summary.node_count,
      edge_count: summary.edge_count,
      relation_edge_count: summary.relation_edge_count,
      visible_node_count: clientMetrics.visible_node_count,
      visible_edge_count: clientMetrics.visible_edge_count
    },
    live: {
      can_post: canPostToGraphServer(),
      event_source_connected: liveConnected,
      live_refresh_checked: liveRefresh.checked
    },
    agent: {
      status: agent.status,
      active_files: agent.active_files || [],
      active_claims: (agent.active_claims || []).length,
      active_runs: (agent.active_runs || []).length,
      recent_runs: (agent.recent_runs || []).length,
      recent_events: (activity.agent_events || []).length
    },
    selected: selectedFacts
  };
  const statusText = debugFetchError
    ? debugFetchError
    : (debugSnapshot
      ? `Fetched ${debugSnapshot.generated_at || ""}`
      : (debugPerfTick ? `Live perf tick ${debugPerfTick.generated_at || ""}` : (canPostToGraphServer() ? "Server snapshot available" : "Graph server required")));
  const rawSnapshot = snapshot
    ? { server: snapshot, client: clientMetrics }
    : { local };
  return `
    <p class="summary-text">Debug state for graph build, rendering, live updates, and agent activity.</p>
    <div class="section">
      <h3>Local Runtime</h3>
      <dl class="kv">
        <dt>Payload chars</dt><dd>${escapeHtml(local.graph.payload_chars)}</dd>
        <dt>Nodes</dt><dd>${escapeHtml(`${local.graph.visible_node_count}/${local.graph.node_count || 0}`)}</dd>
        <dt>Edges</dt><dd>${escapeHtml(`${local.graph.visible_edge_count}/${local.graph.edge_count || 0}`)}</dd>
        <dt>Hydrate</dt><dd>${escapeHtml(`${clientMetrics.last_hydrate_ms} ms · ${clientMetrics.hydrate_count}x`)}</dd>
        <dt>Render</dt><dd>${escapeHtml(`${clientMetrics.last_render_ms} ms · ${clientMetrics.render_count}x`)}</dd>
        <dt>Live</dt><dd>${escapeHtml(local.live.event_source_connected ? "connected" : (liveRefresh.checked ? "connecting" : "off"))}</dd>
        <dt>Perf tick</dt><dd>${escapeHtml(debugPerfTick ? (debugPerfTick.generated_at || "received") : "none")}</dd>
      </dl>
    </div>
    <div class="section">
      <h3>Selected Context</h3>
      <p class="summary-text">${escapeHtml(selectedFacts)}</p>
    </div>
    <div class="actions">
      <button class="small-button" id="refresh-debug" type="button"${canPostToGraphServer() ? "" : " disabled aria-disabled=\"true\""}>Fetch server debug</button>
      <span class="inline-status" id="debug-status">${escapeHtml(statusText)}</span>
    </div>
    <div class="section">
      <h3>Ops Snapshot</h3>
      <div id="debug-ops">${renderOpsPanel(snapshot)}</div>
    </div>
    <div class="section">
      <h3>Snapshot</h3>
      <pre id="debug-json">${escapeHtml(JSON.stringify(rawSnapshot, null, 2))}</pre>
    </div>
  `;
}
function bindDebugPanel() {
  const button = document.getElementById("refresh-debug");
  if (!button) return;
  button.addEventListener("click", fetchDebugSnapshot);
}
async function fetchDebugSnapshot() {
  const output = document.getElementById("debug-json");
  const status = document.getElementById("debug-status");
  if (!output || !status || !canPostToGraphServer()) return;
  const started = performance.now();
  status.textContent = "Fetching";
  try {
    const response = await fetchGraphGet("/api/debug", {
      headers: { "Accept": "application/json" },
      cache: "no-store"
    });
    const snapshot = sanitizeDebugValue(await response.json());
    if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
    clientMetrics.last_debug_fetch_ms = Math.round((performance.now() - started) * 100) / 100;
    debugSnapshot = snapshot;
    debugFetchError = "";
    output.textContent = JSON.stringify({ server: snapshot, client: clientMetrics }, null, 2);
    status.textContent = `Fetched in ${clientMetrics.last_debug_fetch_ms} ms`;
    if (activeTab === "debug" && !selectedRunTranscript) renderInspector();
  } catch (err) {
    debugFetchError = err.message || "Fetch failed";
    status.textContent = debugFetchError;
  }
}
function renderChat(node) {
  const canSubmit = canPostToGraphServer();
  const disabled = canSubmit ? "" : " disabled aria-disabled=\"true\"";
  const selectedPaths = node.kind === "file"
    ? [node.path]
    : uniquePaths((node.metrics && ((node.metrics.active_files || []).concat(node.metrics.recent_files || []))) || []);
  const recentEvents = ((data.activity && data.activity.agent_events) || []).slice(0, 8);
  const eventRows = recentEvents.length
    ? recentEvents.map(event => {
        const target = event.file_path ? ` · ${event.file_path}` : "";
        return `
          <div class="edit-item compact-event">
            <strong>${escapeHtml(event.agent_name || "Agent")} · ${escapeHtml(event.event_type || "event")}${escapeHtml(target)}</strong>
            <span>${escapeHtml(event.timestamp || "")}</span>
            <div>${escapeHtml(event.message || "")}</div>
          </div>
        `;
      }).join("")
    : `<p class="empty">No agent messages recorded yet.</p>`;
  return `
    <div class="agent-chat">
      <div class="chat-controls">
        <label>
          <span>Target</span>
          <select id="agent-provider">
            <option value="codex">Codex CLI</option>
            <option value="claude">Claude CLI</option>
            <option value="configured">Configured adapter</option>
          </select>
        </label>
      </div>
      <textarea class="note-box chat-box" id="agent-chat-message" placeholder="Send a task or question about this selected node to the coding agent."></textarea>
      <div class="actions">
        <button class="small-button primary-action" id="send-agent-message" type="button"${disabled}>Send to agent</button>
        <button class="small-button" id="copy-agent-message-json" type="button">Copy JSON</button>
        <span class="inline-status" id="agent-chat-status">${canSubmit ? "Ready" : "Graph server required"}</span>
      </div>
    </div>
    <div class="section">
      <h3>Selected Context</h3>
      <dl class="kv">
        <dt>Node</dt><dd>${escapeHtml(node.path)}</dd>
        <dt>Care</dt><dd>${escapeHtml(node.care_level || "")}</dd>
        <dt>Files</dt><dd>${escapeHtml(selectedPaths.length ? selectedPaths.join(", ") : "No file targets")}</dd>
      </dl>
      <p class="summary-text">${escapeHtml(node.summary)}</p>
    </div>
    <div class="section">
      <h3>Agent Timeline</h3>
      <div class="edit-list">${eventRows}</div>
    </div>
  `;
}
function agentNameForProvider(provider) {
  if (provider === "codex") return "Codex";
  if (provider === "claude") return "Claude";
  return (data.agent && data.agent.name) || "Agent";
}
function providerFromChatControl(selectEl) {
  const value = selectEl ? String(selectEl.value || "codex") : "codex";
  return value === "configured" ? "" : value;
}
function defaultProviderForRun(run) {
  const metadata = (run && run.metadata) || {};
  const provider = String(metadata.provider || "").toLowerCase();
  if (provider === "claude" || String((run && run.agent_name) || "").toLowerCase().includes("claude")) return "claude";
  return "codex";
}
function agentTaskPayloadFromRun(transcript, message, options = {}) {
  const run = transcript.run || {};
  const metadata = run.metadata || {};
  const selectedPaths = uniquePaths(
    (transcript.active_files || [])
      .concat(run.active_files || [])
      .concat(metadata.selected_paths || [])
      .concat((transcript.summary && transcript.summary.files_touched) || [])
  );
  const selectedNodes = uniquePaths((run.selected_nodes || []).concat(
    selectedPaths.map(path => fileNodeId(path)).filter(id => nodeById.has(id))
  ));
  const anchorNode = selectedPaths.map(path => nodeById.get(fileNodeId(path))).find(Boolean) || selected || nodeById.get("dir:.");
  const payload = agentTaskPayload(anchorNode, message, options);
  payload.kind = "code_index_graph_agent_followup_task";
  payload.parent_run_id = run.run_id || null;
  payload.selected_nodes = selectedNodes.length ? selectedNodes : payload.selected_nodes;
  payload.selected_paths = selectedPaths.length ? selectedPaths : payload.selected_paths;
  payload.run_context = {
    run_id: run.run_id || null,
    agent_name: run.agent_name || "Agent",
    status: run.status || "working",
    recent_events: (transcript.events || []).slice(-12)
  };
  return payload;
}
function bindRunTranscriptPanel(transcript) {
  const providerSelect = document.getElementById("run-followup-provider");
  const messageBox = document.getElementById("run-followup-message");
  const sendButton = document.getElementById("send-run-followup");
  const status = document.getElementById("run-followup-status");
  if (providerSelect) providerSelect.value = defaultProviderForRun((transcript && transcript.run) || {});
  terminalLastSignature = "";
  scheduleTerminalSync(true);
  if (!sendButton || !messageBox || !status) return;
  bindSubmitOnEnter(messageBox, sendButton);
  sendButton.addEventListener("click", async () => {
    const message = messageBox.value.trim();
    if (!message) {
      status.textContent = "Message required";
      return;
    }
    const provider = providerFromChatControl(providerSelect);
    sendButton.disabled = true;
    status.textContent = "Sending";
    try {
      const payload = applyPreflightConfirmation(agentTaskPayloadFromRun(transcript, message, {
        provider,
        agentName: agentNameForProvider(provider)
      }), sendButton);
      const result = await postAgentTaskToServer(payload);
      if (handlePreflightResult(result, sendButton, status, "Send anyway")) return;
      if (result.ok) {
        status.textContent = result.dispatch && result.dispatch.configured
          ? `Started ${result.run.run_id.slice(0, 8)}`
          : `Queued ${result.run.run_id.slice(0, 8)}`;
        applyAgentRunResponse(result);
        messageBox.value = "";
        resetPreflightButton(sendButton, "Send follow-up");
        openRunTranscriptFromResponse(result);
      }
    } catch (err) {
      status.textContent = err.message || "Send failed";
    } finally {
      sendButton.disabled = !canPostToGraphServer();
    }
  });
}
function bindChatPanel(node) {
  const providerSelect = document.getElementById("agent-provider");
  const messageBox = document.getElementById("agent-chat-message");
  const sendButton = document.getElementById("send-agent-message");
  const copyButton = document.getElementById("copy-agent-message-json");
  const status = document.getElementById("agent-chat-status");
  if (copyButton && messageBox) {
    copyButton.addEventListener("click", async () => {
      const provider = providerFromChatControl(providerSelect);
      await copyJson(agentTaskPayload(node, messageBox.value.trim(), {
        provider,
        agentName: agentNameForProvider(provider)
      }));
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy JSON"; }, 900);
    });
  }
  if (sendButton && messageBox && status) {
    bindSubmitOnEnter(messageBox, sendButton);
    sendButton.addEventListener("click", async () => {
      const message = messageBox.value.trim();
      if (!message) {
        status.textContent = "Message required";
        return;
      }
      const provider = providerFromChatControl(providerSelect);
      sendButton.disabled = true;
      status.textContent = "Sending";
      try {
        const payload = applyPreflightConfirmation(agentTaskPayload(node, message, {
          provider,
          agentName: agentNameForProvider(provider)
        }), sendButton);
        const result = await postAgentTaskToServer(payload);
        if (handlePreflightResult(result, sendButton, status, "Send anyway")) return;
        if (result.ok) {
          status.textContent = result.dispatch && result.dispatch.configured
            ? `Started ${result.run.run_id.slice(0, 8)}`
            : `Queued ${result.run.run_id.slice(0, 8)}`;
          applyAgentRunResponse(result);
          messageBox.value = "";
          resetPreflightButton(sendButton, "Send to agent");
          openRunTranscriptFromResponse(result);
        } else if (result.copied) {
          status.textContent = "Copied task JSON";
        }
      } catch (err) {
        status.textContent = err.message || "Send failed";
      } finally {
        sendButton.disabled = !canPostToGraphServer();
      }
    });
  }
}
"""

__all__ = ["GRAPH_SCRIPT_INSPECTOR"]
