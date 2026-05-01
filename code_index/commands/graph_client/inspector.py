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
  panelBody.classList.remove("terminal-view", "chat-view");
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
    panelBody.classList.add("chat-view");
    panelBody.innerHTML = renderChat(selected);
    bindChatPanel(selected);
  } else if (activeTab === "edits") {
    panelBody.innerHTML = renderEdits(selected);
  } else if (activeTab === "notes") {
    panelBody.innerHTML = renderNotes(selected);
    bindNotesPanel(selected);
  } else {
    panelBody.innerHTML = renderSummary(selected);
    bindSummaryPanel();
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
  panelBody.classList.remove("chat-view");
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
    return terminalEmptyHtml(transcript);
  }
  return events.map(event => renderStreamEvent(event, run)).join("") + terminalCursorHtml(transcript);
}
function terminalSignature(transcript) {
  return transcriptStreamEvents(transcript).map(eventIdentity).join("|");
}
function runIsTerminal(run) {
  return isTerminalStatus((run && run.status) || "");
}
function runStatusClass(run) {
  const status = String((run && run.status) || "working").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  return status || "working";
}
function runActivityLabel(event, run) {
  const status = String((run && run.status) || "").toLowerCase();
  if (status === "completed") return "Done";
  if (status === "failed") return "Failed";
  if (status === "cancelled" || status === "canceled") return "Cancelled";
  if (status === "blocked") return "Blocked";
  if (status === "review" || status === "needs_review" || status === "needs-review") return "Review";
  const type = String((event && event.event_type) || "").toLowerCase();
  const labels = {
    edit: "Editing",
    read: "Reading",
    test: "Testing",
    tool: "Using tool",
    navigate: "Scanning",
    decision: "Thinking",
    task: "Starting",
    status: "Working"
  };
  return labels[type] || "Working";
}
function runActivitySummary(transcript) {
  const run = (transcript && transcript.run) || {};
  const events = (transcript && transcript.events) || [];
  const lastEvent = events.length ? events[events.length - 1] : null;
  const statusClass = runStatusClass(run);
  return {
    label: runActivityLabel(lastEvent, run),
    message: (lastEvent && lastEvent.message) || run.prompt || "",
    className: `${runIsTerminal(run) ? "is-terminal" : "is-running"} is-${statusClass}`
  };
}
function terminalCursorHtml(transcript) {
  const run = (transcript && transcript.run) || {};
  return runIsTerminal(run) ? "" : `<div class="terminal-cursor" aria-hidden="true"></div>`;
}
function terminalEmptyHtml(transcript) {
  return `<p class="empty">No terminal output recorded for this run yet.</p>${terminalCursorHtml(transcript)}`;
}
function runMessageHistoryKey(runId) {
  return `code_index_graph_run_history:${data.root}:${runId || "unknown"}`;
}
function loadRunMessageHistory(runId) {
  try {
    const raw = localStorage.getItem(runMessageHistoryKey(runId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.slice(-3) : [];
  } catch (_err) {
    return [];
  }
}
function saveRunMessageHistory(runId, message) {
  try {
    const history = loadRunMessageHistory(runId);
    history.push({ text: message, timestamp: new Date().toISOString() });
    const trimmed = history.slice(-3);
    localStorage.setItem(runMessageHistoryKey(runId), JSON.stringify(trimmed));
  } catch (_err) {
    // Storage may be full or private browsing.
  }
}
function renderRunMessageHistory(runId) {
  const history = loadRunMessageHistory(runId);
  if (!history.length) return "";
  const items = history.map(item => `
    <div class="thread-history-item">
      <span class="thread-history-time">${escapeHtml(new Date(item.timestamp).toLocaleTimeString())}</span>
      <span class="thread-history-text">${escapeHtml(item.text)}</span>
    </div>
  `).join("");
  return `
    <div class="thread-history" id="run-thread-history">
      <strong>Recent messages</strong>
      ${items}
    </div>
  `;
}
function appendTerminalRows(body, transcript) {
  const run = (transcript && transcript.run) || {};
  const events = transcriptStreamEvents(transcript);
  if (!events.length) {
    body.innerHTML = terminalEmptyHtml(transcript);
    return;
  }
  if (body.querySelector(".empty")) {
    body.innerHTML = "";
  }
  let cursor = body.querySelector(".terminal-cursor");
  if (!cursor && !runIsTerminal(run)) {
    body.insertAdjacentHTML("beforeend", terminalCursorHtml(transcript));
    cursor = body.querySelector(".terminal-cursor");
  }
  const existing = new Set(
    [...body.querySelectorAll(".stream-line")]
      .map(line => line.dataset.eventKey)
      .filter(Boolean)
  );
  events.forEach(event => {
    const key = eventIdentity(event);
    if (existing.has(key)) return;
    if (cursor) {
      cursor.insertAdjacentHTML("beforebegin", renderStreamEvent(event, run));
    } else {
      body.insertAdjacentHTML("beforeend", renderStreamEvent(event, run));
    }
    existing.add(key);
  });
  if (runIsTerminal(run) && cursor) cursor.remove();
}
function scrollTerminalToBottom() {
  const body = document.getElementById("terminal-stream-body");
  if (!body) return;
  body.scrollTop = body.scrollHeight;
}
function terminalIsNearBottom(body) {
  if (!body) return true;
  return body.scrollHeight - body.scrollTop - body.clientHeight < 80;
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
  const shouldStayPinned = forceScroll || terminalIsNearBottom(body);
  const signature = terminalSignature(selectedRunTranscript);
  const previousSignature = terminalLastSignature;
  if (signature !== terminalLastSignature) {
    appendTerminalRows(body, selectedRunTranscript);
    terminalLastSignature = signature;
  }
  syncTerminalRunIndicator(selectedRunTranscript);
  updateRunTranscriptHeader(selectedRunTranscript);
  if (shouldStayPinned && (forceScroll || signature !== previousSignature)) {
    scrollTerminalToBottom();
  }
  return true;
}
function syncTerminalRunIndicator(transcript) {
  const activity = runActivitySummary(transcript);
  const bar = document.querySelector(".terminal-bar");
  if (bar) bar.className = `terminal-bar ${activity.className}`;
  const indicator = document.querySelector(".terminal-run-indicator");
  if (!indicator) return;
  indicator.className = `terminal-run-indicator ${activity.className}`;
  const label = indicator.querySelector("strong");
  const message = indicator.querySelector("em");
  if (label) label.textContent = activity.label;
  if (message) message.textContent = activity.message;
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
  const activity = runActivitySummary(transcript);
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
      <div class="terminal-bar ${activity.className}">
        <span class="terminal-run-indicator ${activity.className}">
          <span class="terminal-status-dot" aria-hidden="true"></span>
          <strong>${escapeHtml(activity.label)}</strong>
          <em>${escapeHtml(activity.message)}</em>
        </span>
        <span class="terminal-run-facts">${escapeHtml(defaultProviderForRun(run).toUpperCase())} · ${escapeHtml(runFacts)}</span>
      </div>
      <details class="terminal-context" open>
        <summary>${escapeHtml(run.run_id || "run")} · ${escapeHtml(run.started_at || "")}</summary>
        <div class="terminal-context-grid">
          <div><strong>Active Files</strong><ul class="compact">${fileRows}</ul></div>
          <div><strong>Suggestions</strong><ul class="compact">${suggestionRows}</ul></div>
          <div><strong>Decisions</strong><ul class="compact">${decisionRows}</ul></div>
        </div>
      </details>
      <div class="stream-list terminal-body" id="terminal-stream-body" data-run-id="${escapeHtml(run.run_id || "")}">${renderTerminalRows(transcript)}</div>
      <div class="terminal-composer">
        ${renderRunMessageHistory(run.run_id)}
        <div class="chat-controls terminal-target">
          <label>
            <span>Target</span>
            <select id="run-followup-provider">
              ${providerOptionHtml(defaultProviderForRun(run))}
            </select>
          </label>
          <label>
            <span>Strategy</span>
            <select id="run-followup-execution-strategy">
              <option value="single_agent">Single agent</option>
              <option value="agent_swarm">Agent Swarm</option>
            </select>
          </label>
        </div>
        <textarea class="note-box chat-box terminal-input" id="run-followup-message" placeholder="$ Send another message to this run."></textarea>
        <div class="actions">
          <button class="small-button primary-action" id="send-run-followup" type="button"${canPostToGraphServer() ? "" : " disabled aria-disabled=\"true\""}>Send message</button>
          <span class="inline-status" id="run-followup-status">${canPostToGraphServer() ? "Ready" : "Graph server required"}</span>
          <span class="inline-status runtime-status" id="run-runtime-status">${escapeHtml(renderAgentRuntimeStatus())}</span>
        </div>
        <span class="keyboard-hint">Press Enter to send · Shift+Enter for newline</span>
      </div>
    </div>
  `;
}
function compactNumber(value, fallback = "0") {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return number.toLocaleString();
}
function summaryStatusText(node) {
  const metrics = node.metrics || {};
  const parts = [];
  if (node.kind === "file") {
    parts.push(`${node.role_label || "File"} in ${node.language || "unknown language"}`);
  } else {
    parts.push("Directory layer");
  }
  if (metrics.line_count) parts.push(`${compactNumber(metrics.line_count)} lines`);
  if (metrics.symbol_count) parts.push(`${compactNumber(metrics.symbol_count)} symbols`);
  if ((node.imports || []).length) parts.push(`${compactNumber((node.imports || []).length)} imports`);
  return parts.join(" · ");
}
function summaryInsightRows(node) {
  const metrics = node.metrics || {};
  const rows = [];
  if (node.active_work) rows.push("An agent is currently reporting active work here.");
  if (metrics.recent_edit_rank) rows.push(`Recently edited file ranked #${metrics.recent_edit_rank} in activity.`);
  if (metrics.incoming_relations || metrics.outgoing_relations) {
    rows.push(`${compactNumber(metrics.incoming_relations || 0)} inbound and ${compactNumber(metrics.outgoing_relations || 0)} outbound cross-file relation(s).`);
  }
  if ((metrics.incoming_files || []).length) {
    rows.push(`${compactNumber((metrics.incoming_files || []).length)} file(s) depend on this node.`);
  }
  if ((metrics.outgoing_files || []).length) {
    rows.push(`This node reaches ${compactNumber((metrics.outgoing_files || []).length)} other file(s).`);
  }
  if (metrics.test_count) rows.push(`${compactNumber(metrics.test_count)} affected test edge(s) are indexed.`);
  if (metrics.diagnostic_count) rows.push(`${compactNumber(metrics.diagnostic_count)} diagnostic(s) are attached.`);
  if (!rows.length) rows.push("No active work, relation pressure, diagnostics, or test edges are currently attached.");
  return rows.map(row => `<li>${escapeHtml(row)}</li>`).join("");
}
function summaryStat(label, value, detail = "") {
  return `
    <div class="summary-stat">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${detail ? `<small>${escapeHtml(detail)}</small>` : ""}
    </div>
  `;
}
function summaryFileReference(path, direction) {
  const id = fileNodeId(path);
  const indexed = nodeById.has(id);
  const classes = ["summary-file-ref", indexed ? "" : "missing"].filter(Boolean).join(" ");
  const attrs = indexed ? ` data-summary-node="${escapeHtml(id)}"` : " disabled aria-disabled=\"true\"";
  return `
    <button class="${classes}" type="button"${attrs} title="${escapeHtml(path)}">
      <span>${escapeHtml(direction)}</span>
      <strong>${escapeHtml(path)}</strong>
    </button>
  `;
}
function renderRelationshipSection(title, description, paths, direction, totalRelations) {
  const unique = uniquePaths(paths || []);
  if (!unique.length) return "";
  const rows = unique.slice(0, 14).map(path => summaryFileReference(path, direction)).join("");
  const overflow = unique.length > 14 ? `<p class="summary-note">Showing 14 of ${escapeHtml(compactNumber(unique.length))} files.</p>` : "";
  const relationText = totalRelations
    ? `${compactNumber(totalRelations)} relation(s) across ${compactNumber(unique.length)} file(s).`
    : `${compactNumber(unique.length)} linked file(s).`;
  return `
    <div class="section summary-relationships">
      <div class="summary-section-head">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <p>${escapeHtml(description)}</p>
        </div>
        <span>${escapeHtml(relationText)}</span>
      </div>
      <div class="summary-ref-list">${rows}</div>
      ${overflow}
    </div>
  `;
}
function renderSummary(node) {
  const metrics = node.metrics || {};
  const index = node.index || {};
  const reasons = (node.importance.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join("");
  const symbols = (node.symbols || []).slice(0, 12).map(s => `
    <li>
      <strong>${escapeHtml(s.canonical_name)}</strong>
      <span>${escapeHtml(s.kind)}${s.line ? ` · line ${escapeHtml(s.line)}` : ""}</span>
    </li>`
  ).join("");
  const imports = (node.imports || []).slice(0, 12).map(i => `<li>${escapeHtml(i)}</li>`).join("");
  const status = summaryStatusText(node);
  const quickRead = node.kind === "file"
    ? `${node.summary} ${node.freedom}`
    : node.summary;
  return `
    <div class="summary-overview">
      <div class="summary-copy">
        <h3>Quick Read</h3>
        <p>${escapeHtml(quickRead)}</p>
        <span>${escapeHtml(status)}</span>
      </div>
      <div class="summary-stat-grid">
        ${summaryStat("Care", node.care_level || "n/a", node.importance.rank ? `rank ${node.importance.rank}` : "")}
        ${summaryStat("Size", metrics.line_count ? compactNumber(metrics.line_count) : "n/a", "lines")}
        ${summaryStat("Symbols", compactNumber(metrics.symbol_count || 0), `${compactNumber(metrics.chunk_count || 0)} chunks`)}
        ${summaryStat("Relations", `${compactNumber(metrics.incoming_relations || 0)} in / ${compactNumber(metrics.outgoing_relations || 0)} out`)}
      </div>
    </div>
    <div class="section summary-scan">
      <div>
        <h3>What To Notice</h3>
        <ul class="compact summary-insights">${summaryInsightRows(node)}</ul>
      </div>
      <div>
        <h3>Index State</h3>
        <dl class="kv compact-kv">
          <dt>Parse</dt><dd>${escapeHtml(index.parse_status || "unknown")}</dd>
          <dt>Source</dt><dd>${escapeHtml(index.semantic_source || "n/a")}</dd>
          <dt>Confidence</dt><dd>${escapeHtml(index.parser_confidence || "n/a")}</dd>
          <dt>Last Edit</dt><dd>${escapeHtml(metrics.last_edited_at || "not recorded")}</dd>
          <dt>Edits</dt><dd>${escapeHtml(compactNumber(metrics.edit_count || 0))}</dd>
          <dt>Tests</dt><dd>${escapeHtml(compactNumber(metrics.test_count || 0))}</dd>
        </dl>
      </div>
    </div>
    <div class="section">
      <h3>Care Guidance</h3>
      <p class="summary-text">${escapeHtml(node.freedom)}</p>
      <ul class="compact">${reasons}</ul>
    </div>
    ${symbols ? `<div class="section"><h3>Top Symbols</h3><ul class="compact summary-symbols">${symbols}</ul></div>` : ""}
    ${imports ? `<div class="section"><h3>Imports</h3><ul class="compact">${imports}</ul></div>` : ""}
    ${renderRelationshipSection(
      "Incoming Files",
      "Files with indexed relations pointing into this node.",
      metrics.incoming_files || [],
      "in",
      metrics.incoming_relations || 0
    )}
    ${renderRelationshipSection(
      "Outgoing Files",
      "Files this node points to through indexed relations.",
      metrics.outgoing_files || [],
      "out",
      metrics.outgoing_relations || 0
    )}
  `;
}
function bindSummaryPanel() {
  document.querySelectorAll("[data-summary-node]").forEach(button => {
    button.addEventListener("click", () => {
      const target = nodeById.get(button.dataset.summaryNode);
      if (target) selectNode(target, { center: true });
    });
  });
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
function shortRunId(runId) {
  return String(runId || "").slice(0, 8) || "run";
}
function runFiles(run) {
  const metadata = (run && run.metadata) || {};
  return uniquePaths(
    ((run && run.active_files) || [])
      .concat(metadata.selected_paths || [])
      .concat(
        Array.isArray(run && run.selected_nodes)
          ? run.selected_nodes
              .filter(id => String(id || "").startsWith("file:"))
              .map(id => String(id).slice(5))
          : []
      )
  );
}
function selectedFilesForNode(node) {
  if (!node) return [];
  if (node.kind === "file") return [node.path];
  return uniquePaths((node.metrics && ((node.metrics.active_files || []).concat(node.metrics.recent_files || []))) || []);
}
function cleanAgentMessage(message) {
  const raw = String(message || "").trim();
  if (!raw) return "";
  if (raw.startsWith("CODE_INDEX_EVENT ")) {
    try {
      const payload = JSON.parse(raw.slice("CODE_INDEX_EVENT ".length));
      return [
        payload.message || payload.event || payload.type || "agent event",
        payload.path ? `file: ${payload.path}` : "",
        payload.payload && payload.payload.phase ? `phase: ${payload.payload.phase}` : ""
      ].filter(Boolean).join("\n");
    } catch (_err) {
      return raw;
    }
  }
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw);
      const item = parsed.item || {};
      if (item.type === "agent_message" && item.text) return String(item.text).trim();
      if (item.type === "todo_list" && Array.isArray(item.items)) {
        return item.items.map(todo => `${todo.completed ? "[x]" : "[ ]"} ${todo.text || ""}`).join("\n");
      }
      if (item.type === "command_execution") {
        const command = item.command ? `$ ${item.command}` : "";
        const output = item.aggregated_output ? String(item.aggregated_output).trim() : "";
        const status = item.status ? `status: ${item.status}` : "";
        return [command, output, status].filter(Boolean).join("\n");
      }
      if (parsed.type) return parsed.type;
    } catch (_err) {
      return raw;
    }
  }
  return raw;
}
function truncateText(text, limit = 900) {
  const value = String(text || "");
  if (value.length <= limit) return value;
  return `${value.slice(0, limit - 1)}...`;
}
function eventPhase(event) {
  const type = String((event && event.event_type) || "").toLowerCase();
  if (type === "edit") return "editing";
  if (type === "test") return "testing";
  if (type === "read" || type === "navigate") return "reading";
  if (type === "decision") return "planning";
  if (type === "suggestion") return "review";
  if (type === "status") {
    const message = String((event && event.message) || "").toLowerCase();
    if (message.includes("complete")) return "done";
    if (message.includes("cancel")) return "blocked";
  }
  return type || "activity";
}
function eventToneClass(event) {
  const phase = eventPhase(event);
  if (phase === "editing") return "is-editing";
  if (phase === "testing") return "is-testing";
  if (phase === "blocked") return "is-blocked";
  if (phase === "review") return "is-review";
  return "";
}
function fileChip(path, options = {}) {
  const id = fileNodeId(path);
  const indexed = nodeById.has(id);
  const classes = ["chat-file-chip", options.active ? "active" : "", indexed ? "" : "missing"].filter(Boolean).join(" ");
  const attrs = indexed ? ` data-chat-node="${escapeHtml(id)}"` : " disabled aria-disabled=\"true\"";
  const title = options.title || path;
  return `<button class="${classes}" type="button"${attrs} title="${escapeHtml(title)}">${escapeHtml(path)}</button>`;
}
function collectSuggestionRows(events, limit = 6) {
  const rows = [];
  (events || []).forEach(event => {
    if (!event) return;
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    const payloadSuggestions = Array.isArray(payload.suggestions) ? payload.suggestions : [];
    if (String(event.event_type || "").toLowerCase() === "suggestion" && !payloadSuggestions.length) {
      rows.push({
        message: event.message || "Agent suggestion",
        command: "",
        run_id: event.run_id || ""
      });
    }
    payloadSuggestions.forEach(item => {
      rows.push({
        message: item.message || item.kind || event.message || "Agent suggestion",
        command: item.command || "",
        run_id: event.run_id || ""
      });
    });
  });
  return rows.slice(0, limit);
}
function timelineEventsForFocus(focus) {
  const events = (focus && focus.scopedEvents && focus.scopedEvents.length)
    ? focus.scopedEvents
    : ((focus && focus.events) || []);
  return [...events].sort((a, b) => {
    const at = Date.parse(a.timestamp || "") || 0;
    const bt = Date.parse(b.timestamp || "") || 0;
    if (at !== bt) return at - bt;
    return Number(a.event_pk || 0) - Number(b.event_pk || 0);
  });
}
function agentFocusForNode(node) {
  const agent = data.agent || {};
  const activity = data.activity || {};
  const activeRuns = uniqueRuns(agent.active_runs || []).filter(run => !isTerminalStatus(run.status));
  const recentRuns = uniqueRuns(agent.recent_runs || []);
  const allRuns = uniqueRuns(activeRuns.concat(recentRuns));
  const selectedPaths = selectedFilesForNode(node);
  const relatedRun = allRuns.find(run => runFiles(run).some(path => selectedPaths.includes(path)));
  const currentRun = relatedRun || activeRuns[0] || recentRuns[0] || null;
  const events = activity.agent_events || [];
  const scopedEvents = currentRun ? events.filter(event => event.run_id === currentRun.run_id) : events;
  const latestEvent = scopedEvents[0] || events[0] || null;
  const claims = agent.active_claims || [];
  const activeFiles = uniquePaths(
    (agent.active_files || [])
      .concat(currentRun ? runFiles(currentRun) : [])
      .concat(claims.map(claim => claim.file_path).filter(Boolean))
  );
  const files = activeFiles.length ? activeFiles : selectedPaths;
  const scopedSuggestions = collectSuggestionRows(scopedEvents);
  const seenSuggestions = new Set(scopedSuggestions.map(item => `${item.message}|${item.command}`));
  const fallbackSuggestions = collectSuggestionRows(events)
    .filter(item => !seenSuggestions.has(`${item.message}|${item.command}`));
  return {
    activeRuns,
    currentRun,
    latestEvent,
    phase: latestEvent ? eventPhase(latestEvent) : (activeRuns.length ? "working" : "idle"),
    files,
    claims,
    suggestions: scopedSuggestions.concat(fallbackSuggestions).slice(0, 6),
    scopedEvents,
    events
  };
}
function renderFocusList(title, rows, emptyText) {
  return `
    <div class="focus-block">
      <h4>${escapeHtml(title)}</h4>
      ${rows.length ? `<div class="focus-list">${rows.join("")}</div>` : `<p class="empty">${escapeHtml(emptyText)}</p>`}
    </div>
  `;
}
function renderTimelineEvent(event) {
  const target = event.file_path || event.symbol_path || "";
  const phase = eventPhase(event);
  const message = truncateText(cleanAgentMessage(event.message), 6000);
  const meta = [
    event.timestamp || "",
    event.agent_name || "Agent",
    event.event_type || "event",
    target
  ].filter(Boolean).join(" · ");
  return `
    <div class="edit-item compact-event agent-event-card ${escapeHtml(eventToneClass(event))}">
      <div class="event-card-head">
        <strong>${escapeHtml(phase)}</strong>
        <span>${escapeHtml(shortRunId(event.run_id))}</span>
      </div>
      <span>${escapeHtml(meta)}</span>
      <div class="agent-message-text">${escapeHtml(message || "No message recorded.")}</div>
    </div>
  `;
}
function renderChat(node) {
  const canSubmit = canPostToGraphServer();
  const disabled = canSubmit ? "" : " disabled aria-disabled=\"true\"";
  const selectedPaths = selectedFilesForNode(node);
  const focus = agentFocusForNode(node);
  const currentRun = focus.currentRun;
  const timelineEvents = timelineEventsForFocus(focus);
  const activeFileRows = focus.files.slice(0, 10).map(path => fileChip(path, {
    active: ((data.agent && data.agent.active_files) || []).includes(path),
    title: "Open file in graph"
  }));
  const claimRows = focus.claims.slice(0, 8).map(claim => `
    <div class="focus-row">
      <span>${escapeHtml(claim.file_path || "claim")}</span>
      <strong>${escapeHtml([claim.agent_name || "Agent", claim.mode || "claim", claim.run_status || claim.status || ""].filter(Boolean).join(" · "))}</strong>
    </div>
  `);
  const suggestionRows = focus.suggestions.map(item => {
    const command = item.command ? `<code>${escapeHtml(item.command)}</code>` : "";
    return `
      <div class="focus-row suggestion-row">
        <span>${escapeHtml(item.message || "Suggestion")}</span>
        ${command}
      </div>
    `;
  });
  const eventRows = timelineEvents.length
    ? timelineEvents.map(renderTimelineEvent).join("")
    : `<p class="empty">No agent messages recorded yet.</p>`;
  const runLabel = currentRun
    ? `${currentRun.agent_name || "Agent"} · ${currentRun.status || "working"} · ${shortRunId(currentRun.run_id)}`
    : "No active run";
  const latestText = focus.latestEvent
    ? truncateText(cleanAgentMessage(focus.latestEvent.message), 260)
    : "Live activity will appear here when an agent emits events.";
  return `
    <div class="chat-workspace">
      <section class="agent-focus">
        <div class="agent-focus-head">
          <div>
            <h3>Agent Focus</h3>
            <p>${escapeHtml(runLabel)}</p>
          </div>
          <span class="phase-pill">${escapeHtml(focus.phase)}</span>
        </div>
        <p class="focus-current">${escapeHtml(latestText)}</p>
        <div class="focus-grid">
          ${renderFocusList("Active Files", activeFileRows, "No active files reported.")}
          ${renderFocusList("File Claims", claimRows, "No active file claims.")}
          ${renderFocusList("Suggestions", suggestionRows, "No suggestions yet.")}
        </div>
      </section>
      <div class="agent-chat">
        <div class="chat-controls">
          <label>
            <span>Target</span>
            <select id="agent-provider">
              ${providerOptionHtml(defaultChatProvider({ run: currentRun }))}
            </select>
          </label>
          <label>
            <span>Strategy</span>
            <select id="agent-execution-strategy">
              <option value="single_agent">Single agent</option>
              <option value="agent_swarm">Agent Swarm</option>
            </select>
          </label>
        </div>
        <textarea class="note-box chat-box" id="agent-chat-message" placeholder="Send a focused task or follow-up about the selected node."></textarea>
        <div class="actions">
          <button class="small-button primary-action" id="send-agent-message" type="button"${disabled}>Send to agent</button>
          <button class="small-button" id="copy-agent-message-json" type="button">Copy JSON</button>
          <span class="inline-status" id="agent-chat-status">${canSubmit ? "Ready" : "Graph server required"}</span>
          <span class="inline-status runtime-status" id="agent-runtime-status">${escapeHtml(renderAgentRuntimeStatus())}</span>
        </div>
      </div>
      <div class="section selected-context-section">
        <h3>Selected Context</h3>
        <dl class="kv">
          <dt>Node</dt><dd>${escapeHtml(node.path)}</dd>
          <dt>Care</dt><dd>${escapeHtml(node.care_level || "")}</dd>
          <dt>Files</dt><dd>${escapeHtml(selectedPaths.length ? selectedPaths.join(", ") : "No file targets")}</dd>
        </dl>
        <p class="summary-text">${escapeHtml(node.summary)}</p>
      </div>
      <div class="section chat-timeline-section">
        <div class="timeline-head">
          <h3>Agent Timeline</h3>
          <span>${escapeHtml(timelineEvents.length ? `${timelineEvents.length} events · oldest first` : "No events")}</span>
        </div>
        <div class="timeline-scroll" tabindex="0" role="region" aria-label="Agent timeline">
          <div class="edit-list chat-event-list">${eventRows}</div>
        </div>
      </div>
    </div>
  `;
}
function agentProvidersSignatureFromLive(live) {
  const providers = Array.isArray(live && live.agent_providers) ? live.agent_providers : [];
  const runtime = (live && live.agent_runtime) || {};
  const dispatch = runtime.dispatch || {};
  const providerParts = providers.map(provider => [
    provider.id || "",
    provider.display_name || "",
    provider.command_preset || "",
    (provider.capabilities || []).join(",")
  ].join(":")).join("|");
  const dispatchParts = [
    dispatch.webhook_configured ? "webhook" : "",
    dispatch.local_command_configured ? "local" : "",
    dispatch.provider || "",
    dispatch.custom_command_configured ? "custom" : "",
    (dispatch.provider_presets || []).join(",")
  ].join(":");
  return `${providerParts}::${dispatchParts}`;
}
function agentProviderRegistry() {
  const live = (data && data.live) || (typeof graph !== "undefined" && graph && graph.live) || {};
  const providers = Array.isArray(live.agent_providers) ? live.agent_providers : [];
  if (providers.length) return providers;
  return [
    { id: "configured", display_name: "Configured adapter" }
  ];
}
function providerExists(providerId) {
  const target = String(providerId || "").toLowerCase();
  if (!target) return false;
  return agentProviderRegistry().some(provider =>
    String(provider.id || "").toLowerCase() === target
  );
}
function providerHintForRun(run) {
  const metadata = (run && run.metadata) || {};
  const provider = String(metadata.provider || "").toLowerCase();
  if (provider) return provider;
  const agent = String((run && run.agent_name) || "").toLowerCase();
  if (agent.includes("kimi")) return "kimi";
  if (agent.includes("claude")) return "claude";
  if (agent.includes("codex")) return "codex";
  if (agent.includes("opencode")) return "opencode";
  return "";
}
function configuredProviderHint() {
  const dispatch = (agentRuntimeConfig().dispatch) || {};
  const provider = String(dispatch.provider || "").toLowerCase();
  if (provider) return provider;
  if (dispatch.custom_command_configured || dispatch.local_command_configured || dispatch.webhook_configured) {
    return "configured";
  }
  return "";
}
function activeProviderHint() {
  const agent = (data && data.agent) || {};
  const runs = ((agent.active_runs || []).concat(agent.recent_runs || []))
    .filter(run => run && !isTerminalStatus(run.status));
  for (const run of runs) {
    const provider = providerHintForRun(run);
    if (provider) return provider;
  }
  return "";
}
function defaultChatProvider(options = {}) {
  const runHint = options.run ? providerHintForRun(options.run) : "";
  if (runHint) return runHint;
  if (options.includeFocus !== false && selected) {
    const focus = agentFocusForNode(selected);
    const focusHint = providerHintForRun(focus.currentRun);
    if (focusHint) return focusHint;
  }
  const activeHint = activeProviderHint();
  if (activeHint) return activeHint;
  const configuredHint = configuredProviderHint();
  if (configuredHint) return configuredHint;
  if (providerExists("codex")) return "codex";
  const preset = agentProviderRegistry().find(provider => provider && provider.command_preset && provider.id);
  return preset ? String(preset.id || "configured").toLowerCase() : "configured";
}
function providerOptionHtml(selected) {
  const selectedValue = String(selected || defaultChatProvider());
  const providers = agentProviderRegistry().slice();
  if (selectedValue && !providers.some(provider => String(provider.id || "") === selectedValue)) {
    const label = selectedValue === "opencode"
      ? "OpenCode"
      : selectedValue.replace(/(^|[-_\s])([a-z])/g, (_match, prefix, char) => `${prefix}${char.toUpperCase()}`);
    providers.push({ id: selectedValue, display_name: label });
  }
  if (!providers.some(provider => String(provider.id || "") === "configured")) {
    providers.push({ id: "configured", display_name: "Configured adapter" });
  }
  return providers.map(provider => {
    const id = String(provider.id || "");
    if (!id) return "";
    const label = provider.display_name || id;
    const selectedAttr = id === selectedValue ? " selected" : "";
    return `<option value="${escapeHtml(id)}"${selectedAttr}>${escapeHtml(label)}</option>`;
  }).join("");
}
function agentRuntimeConfig() {
  const live = (data && data.live) || {};
  return (live.agent_runtime && typeof live.agent_runtime === "object") ? live.agent_runtime : {};
}
function renderAgentRuntimeStatus() {
  if (!canPostToGraphServer()) return "Static graph";
  const runtime = agentRuntimeConfig();
  const dispatch = (runtime && runtime.dispatch) || {};
  if (dispatch.webhook_configured) return "Webhook dispatch ready";
  if (dispatch.custom_command_configured) return "Custom command ready";
  if (dispatch.local_command_configured) {
    const provider = dispatch.provider ? agentNameForProvider(dispatch.provider) : "Local command";
    return `${provider} dispatch ready`;
  }
  const presets = Array.isArray(dispatch.provider_presets) ? dispatch.provider_presets : [];
  if (presets.length) return "Provider presets ready";
  return "Queue only";
}
function applyAgentProvidersPayload(payload) {
  if (!payload || typeof payload !== "object") return false;
  data.live = data.live || {};
  const before = agentProvidersSignatureFromLive(data.live);
  if (Array.isArray(payload.providers)) {
    data.live.agent_providers = payload.providers;
  }
  if (payload.runtime && typeof payload.runtime === "object") {
    data.live.agent_runtime = payload.runtime;
  }
  const after = agentProvidersSignatureFromLive(data.live);
  agentProviderConfigSignature = after;
  return before !== after;
}
function syncProviderSelectOptions(selectEl, selected = null) {
  if (!selectEl) return;
  const previous = selected == null ? selectEl.value : selected;
  selectEl.innerHTML = providerOptionHtml(previous);
  if ([...selectEl.options].some(option => option.value === previous)) {
    selectEl.value = previous;
  }
}
function syncAgentProviderControls() {
  syncProviderSelectOptions(document.getElementById("agent-provider"));
  syncProviderSelectOptions(document.getElementById("run-followup-provider"));
  const statusText = renderAgentRuntimeStatus();
  document.querySelectorAll(".runtime-status").forEach(item => {
    item.textContent = statusText;
  });
}
async function refreshAgentProviders(options = {}) {
  if (!canPostToGraphServer()) return false;
  const force = !!options.force;
  const now = Date.now();
  if (!force && now - agentProvidersLastFetchMs < 3000) return false;
  if (agentProvidersRefreshPromise) return agentProvidersRefreshPromise;
  agentProvidersLastFetchMs = now;
  const path = (data.live && data.live.agent_providers_path) || "/api/agent-providers";
  agentProvidersRefreshPromise = (async () => {
    try {
      const response = await fetchGraphGet(path, {
        headers: { "Accept": "application/json" },
        cache: "no-store"
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      const changed = applyAgentProvidersPayload(payload);
      if (changed) syncAgentProviderControls();
      return changed;
    } catch (_err) {
      return false;
    } finally {
      agentProvidersRefreshPromise = null;
    }
  })();
  return agentProvidersRefreshPromise;
}
function agentNameForProvider(provider) {
  const providerId = String(provider || "").toLowerCase();
  const registryProvider = agentProviderRegistry().find(item =>
    String(item.id || "").toLowerCase() === providerId
  );
  if (registryProvider && registryProvider.display_name) {
    return String(registryProvider.display_name);
  }
  if (providerId === "opencode") return "OpenCode";
  if (providerId) {
    return providerId.replace(/(^|[-_\s])([a-z])/g, (_match, prefix, char) => `${prefix}${char.toUpperCase()}`);
  }
  return (data.agent && data.agent.name) || "Agent";
}
function providerFromChatControl(selectEl) {
  const value = selectEl ? String(selectEl.value || defaultChatProvider()) : defaultChatProvider();
  return value === "configured" ? "" : value;
}
function executionStrategyFromControl(selectEl) {
  return selectEl ? String(selectEl.value || "single_agent") : "single_agent";
}
function syncProviderForExecutionStrategy(providerSelect, strategySelect) {
  if (!providerSelect || !strategySelect) return;
  const hasKimi = agentProviderRegistry().some(provider => String(provider.id || "") === "kimi");
  if (hasKimi && strategySelect.value === "agent_swarm" && providerSelect.value === "codex") {
    providerSelect.value = "kimi";
  }
}
function defaultProviderForRun(run) {
  return providerHintForRun(run) || defaultChatProvider({ includeFocus: false });
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
  const strategySelect = document.getElementById("run-followup-execution-strategy");
  const messageBox = document.getElementById("run-followup-message");
  const sendButton = document.getElementById("send-run-followup");
  const status = document.getElementById("run-followup-status");
  if (providerSelect) providerSelect.value = defaultProviderForRun((transcript && transcript.run) || {});
  refreshAgentProviders();
  if (strategySelect) {
    strategySelect.addEventListener("change", () => {
      syncProviderForExecutionStrategy(providerSelect, strategySelect);
    });
  }
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
    const executionStrategy = executionStrategyFromControl(strategySelect);
    const run = (transcript && transcript.run) || {};
    const runProvider = defaultProviderForRun(run);
    const taskAgentName = provider === runProvider
      ? (run.agent_name || agentNameForProvider(provider))
      : agentNameForProvider(provider);
    sendButton.disabled = true;
    status.textContent = "Sending";
    try {
      const payload = applyPreflightConfirmation(agentTaskPayloadFromRun(transcript, message, {
        provider,
        agentName: taskAgentName,
        executionStrategy
      }), sendButton);
      const runId = ((transcript && transcript.run) || {}).run_id || "";
      const result = await postAgentMessageToRun(runId, payload);
      if (handlePreflightResult(result, sendButton, status, "Send anyway")) return;
      if (result.ok) {
        saveRunMessageHistory(runId, message);
        status.textContent = result.same_run
          ? `Sent ${result.run.run_id.slice(0, 8)}`
          : (result.dispatch && result.dispatch.configured
            ? `Started ${result.run.run_id.slice(0, 8)}`
            : `Queued ${result.run.run_id.slice(0, 8)}`);
        applyAgentRunResponse(result);
        messageBox.value = "";
        resetPreflightButton(sendButton, "Send message");
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
  const strategySelect = document.getElementById("agent-execution-strategy");
  const messageBox = document.getElementById("agent-chat-message");
  const sendButton = document.getElementById("send-agent-message");
  const copyButton = document.getElementById("copy-agent-message-json");
  const status = document.getElementById("agent-chat-status");
  refreshAgentProviders();
  document.querySelectorAll("[data-chat-node]").forEach(button => {
    button.addEventListener("click", () => {
      const target = nodeById.get(button.dataset.chatNode);
      if (target) selectNode(target, { center: true });
    });
  });
  if (copyButton && messageBox) {
    if (strategySelect) {
      strategySelect.addEventListener("change", () => {
        syncProviderForExecutionStrategy(providerSelect, strategySelect);
      });
    }
    copyButton.addEventListener("click", async () => {
      const provider = providerFromChatControl(providerSelect);
      const executionStrategy = executionStrategyFromControl(strategySelect);
      await copyJson(agentTaskPayload(node, messageBox.value.trim(), {
        provider,
        agentName: agentNameForProvider(provider),
        executionStrategy
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
      const executionStrategy = executionStrategyFromControl(strategySelect);
      sendButton.disabled = true;
      status.textContent = "Sending";
      try {
        const payload = applyPreflightConfirmation(agentTaskPayload(node, message, {
          provider,
          agentName: agentNameForProvider(provider),
          executionStrategy
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
