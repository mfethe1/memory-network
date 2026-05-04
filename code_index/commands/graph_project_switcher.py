"""Shared folder/project switcher fragment for graph HTML views."""

from __future__ import annotations


PROJECT_SWITCHER_CSS = r"""
    .project-switcher {
      margin-top: 8px;
    }
    .project-switcher-trigger {
      max-width: 100%;
      min-height: 34px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--field, var(--panel));
      color: var(--text, var(--ink));
      padding: 0 10px;
      font: inherit;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .project-switcher-trigger span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .project-switcher-backdrop {
      position: fixed;
      inset: 0;
      z-index: 80;
      display: grid;
      place-items: start center;
      overflow: auto;
      background: rgba(0, 0, 0, 0.56);
      padding: max(16px, env(safe-area-inset-top)) 14px 16px;
    }
    .project-switcher-backdrop[hidden] {
      display: none !important;
    }
    .project-switcher-panel {
      width: min(640px, 100%);
      max-height: calc(100vh - 32px);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text, var(--ink));
      box-shadow: var(--shadow, 0 18px 46px rgba(0, 0, 0, 0.42));
      overflow: hidden;
    }
    .project-switcher-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding: 12px;
      background: var(--panel-strong, var(--panel-2, var(--panel)));
    }
    .project-switcher-head h2 {
      margin: 0;
      font-size: 16px;
      line-height: 1.2;
    }
    .project-switcher-close {
      min-width: 40px;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: transparent;
      color: var(--text, var(--ink));
      padding: 0 10px;
      cursor: pointer;
    }
    .project-switcher-body {
      min-height: 0;
      display: grid;
      gap: 12px;
      overflow: auto;
      padding: 12px;
    }
    .project-switcher-section {
      display: grid;
      gap: 8px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 12px;
    }
    .project-switcher-section:last-child {
      border-bottom: 0;
      padding-bottom: 0;
    }
    .project-switcher-section h3 {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
      text-transform: uppercase;
    }
    .project-switcher-list {
      display: grid;
      gap: 6px;
    }
    .project-switcher-row {
      min-width: 0;
      min-height: 40px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--field, var(--panel-2, var(--panel)));
      color: var(--text, var(--ink));
      padding: 8px 10px;
    }
    .project-switcher-row.is-disabled {
      opacity: 0.55;
    }
    .project-switcher-row-main {
      min-width: 0;
      display: grid;
      gap: 2px;
    }
    .project-switcher-row-title,
    .project-switcher-path {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .project-switcher-row-title {
      font-size: 13px;
      font-weight: 700;
    }
    .project-switcher-path {
      color: var(--muted);
      font-size: 11px;
    }
    .project-switcher-status {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .project-switcher-actions {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .project-switcher-actions button,
    .project-switcher-path-form button,
    .project-switcher-confirm button {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--field, var(--panel));
      color: var(--text, var(--ink));
      padding: 0 10px;
      font: inherit;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .project-switcher-actions button.primary,
    .project-switcher-confirm button.primary {
      border-color: var(--accent, var(--medium));
      background: var(--accent, var(--medium));
      color: var(--accent-ink, #06110f);
    }
    .project-switcher-actions button:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    .project-switcher-path-form {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
    }
    .project-switcher-path-form input {
      min-width: 0;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--field, var(--panel-2, var(--panel)));
      color: var(--text, var(--ink));
      padding: 8px 10px;
      font: inherit;
      font-size: 13px;
    }
    .project-switcher-message,
    .project-switcher-confirm {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-strong, var(--panel-2, var(--panel)));
      color: var(--muted);
      padding: 9px 10px;
      font-size: 12px;
    }
    .project-switcher-message[hidden],
    .project-switcher-confirm[hidden] {
      display: none !important;
    }
    .project-switcher-confirm {
      display: grid;
      gap: 8px;
    }
    .project-switcher-confirm-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .project-switcher-breadcrumbs {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    @media (max-width: 720px) {
      .project-switcher-panel {
        max-height: calc(100svh - 24px);
      }
      .project-switcher-path-form {
        grid-template-columns: 1fr;
      }
      .project-switcher-row {
        grid-template-columns: 1fr;
      }
      .project-switcher-actions {
        justify-content: stretch;
      }
      .project-switcher-actions button {
        flex: 1 1 0;
      }
    }
"""


PROJECT_SWITCHER_HTML = r"""
<div class="project-switcher" data-project-switcher>
  <button class="project-switcher-trigger" type="button" aria-expanded="false" aria-haspopup="dialog" data-project-switcher-trigger>
    <span data-project-switcher-name>Project</span>
    <span aria-hidden="true">v</span>
  </button>
</div>
<template id="project-switcher-template">
  <div class="project-switcher-backdrop" data-project-switcher-backdrop hidden>
    <section class="project-switcher-panel" role="dialog" aria-modal="true" aria-labelledby="project-switcher-title" data-project-switcher-panel>
      <div class="project-switcher-head">
        <h2 id="project-switcher-title">Switch Project</h2>
        <button class="project-switcher-close" type="button" aria-label="Close project switcher" data-project-switcher-close>x</button>
      </div>
      <div class="project-switcher-body">
        <section class="project-switcher-section">
          <h3>Known Projects</h3>
          <div class="project-switcher-list" data-project-switcher-known></div>
        </section>
        <section class="project-switcher-section">
          <h3>Browse</h3>
          <form class="project-switcher-path-form" data-project-switcher-path-form>
            <input type="text" autocomplete="off" spellcheck="false" data-project-switcher-path aria-label="Directory path">
            <button type="submit">Go</button>
          </form>
          <div class="project-switcher-message" data-project-switcher-message hidden></div>
          <div class="project-switcher-confirm" data-project-switcher-confirm hidden>
            <div data-project-switcher-confirm-text></div>
            <div class="project-switcher-confirm-actions">
              <button class="primary" type="button" data-project-switcher-confirm-start>Initialize</button>
              <button type="button" data-project-switcher-confirm-cancel>Cancel</button>
            </div>
          </div>
          <div class="project-switcher-list" data-project-switcher-entries></div>
          <div class="project-switcher-breadcrumbs" data-project-switcher-breadcrumbs></div>
        </section>
      </div>
    </section>
  </div>
</template>
"""


PROJECT_SWITCHER_SCRIPT = r"""
(() => {
  "use strict";

  const host = document.querySelector("[data-project-switcher]");
  const trigger = document.querySelector("[data-project-switcher-trigger]");
  const nameEl = document.querySelector("[data-project-switcher-name]");
  const template = document.getElementById("project-switcher-template");
  if (!host || !trigger || !template) return;

  const data = readEmbeddedPayload();
  const live = data.live && typeof data.live === "object" ? data.live : {};
  const api = {
    dirs: safePath(live.dirs_path, "/api/dirs"),
    switchProject: safePath(live.switch_project_path, "/api/switch-project"),
    initStatus: safePath(live.init_status_path, "/api/init-status")
  };
  const state = {
    activePath: normalizePath((live.active_project && live.active_project.path) || data.root || ""),
    activeName: (live.active_project && live.active_project.name) || basename(data.root || "Project"),
    pendingInitPath: "",
    pollTimer: null
  };

  nameEl.textContent = state.activeName || "Project";
  document.body.appendChild(template.content.cloneNode(true));

  const backdrop = document.querySelector("[data-project-switcher-backdrop]");
  const panel = document.querySelector("[data-project-switcher-panel]");
  const closeButton = document.querySelector("[data-project-switcher-close]");
  const knownEl = document.querySelector("[data-project-switcher-known]");
  const entriesEl = document.querySelector("[data-project-switcher-entries]");
  const pathForm = document.querySelector("[data-project-switcher-path-form]");
  const pathInput = document.querySelector("[data-project-switcher-path]");
  const messageEl = document.querySelector("[data-project-switcher-message]");
  const confirmEl = document.querySelector("[data-project-switcher-confirm]");
  const confirmText = document.querySelector("[data-project-switcher-confirm-text]");
  const confirmStart = document.querySelector("[data-project-switcher-confirm-start]");
  const confirmCancel = document.querySelector("[data-project-switcher-confirm-cancel]");
  const breadcrumbsEl = document.querySelector("[data-project-switcher-breadcrumbs]");

  trigger.addEventListener("click", () => {
    openSwitcher();
  });
  if (closeButton) closeButton.addEventListener("click", closeSwitcher);
  if (backdrop) {
    backdrop.addEventListener("click", event => {
      if (event.target === backdrop) closeSwitcher();
    });
  }
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && backdrop && !backdrop.hidden) closeSwitcher();
  });
  if (pathForm) {
    pathForm.addEventListener("submit", event => {
      event.preventDefault();
      loadDirs(pathInput ? pathInput.value : "");
    });
  }
  if (confirmStart) {
    confirmStart.addEventListener("click", () => {
      if (state.pendingInitPath) initializeProject(state.pendingInitPath);
    });
  }
  if (confirmCancel) {
    confirmCancel.addEventListener("click", hideConfirm);
  }

  async function openSwitcher() {
    if (!backdrop) return;
    backdrop.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    hideMessage();
    hideConfirm();
    await loadDirs("");
    if (panel) panel.focus();
  }

  function closeSwitcher() {
    if (!backdrop) return;
    backdrop.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    clearPoll();
    hideConfirm();
  }

  async function loadDirs(path) {
    const params = path ? `?${new URLSearchParams({ path }).toString()}` : "";
    setMessage("Loading directories.");
    try {
      const payload = await requestJson(`${api.dirs}${params}`);
      if (pathInput) pathInput.value = payload.path || "";
      renderKnown(payload.known_projects || []);
      renderEntries(payload.entries || []);
      renderBreadcrumbs(payload.path || "", payload.parent || "");
      if (payload.error) setMessage(payload.error);
      else hideMessage();
    } catch (err) {
      setMessage(errorMessage(err));
    }
  }

  function renderKnown(projects) {
    clear(knownEl);
    if (!knownEl) return;
    if (!projects.length) {
      knownEl.appendChild(emptyRow("No registered projects."));
      return;
    }
    projects.forEach(project => {
      const row = rowShell(project.name || basename(project.path), project.path || "", isCurrent(project.path) ? "current" : "");
      const actions = row.querySelector(".project-switcher-actions");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "primary";
      button.textContent = isCurrent(project.path) ? "Current" : "Switch";
      button.disabled = isCurrent(project.path);
      button.addEventListener("click", () => switchProject(project.path));
      actions.appendChild(button);
      knownEl.appendChild(row);
    });
  }

  function renderEntries(entries) {
    clear(entriesEl);
    if (!entriesEl) return;
    if (!entries.length) {
      entriesEl.appendChild(emptyRow("No subdirectories."));
      return;
    }
    entries.forEach(entry => {
      const status = entry.error || (entry.indexed ? "indexed" : "not indexed");
      const row = rowShell(entry.name || basename(entry.path), entry.path || "", status);
      if (entry.error) row.classList.add("is-disabled");
      const actions = row.querySelector(".project-switcher-actions");
      const select = document.createElement("button");
      select.type = "button";
      select.className = entry.indexed ? "primary" : "";
      select.textContent = entry.indexed ? "Switch" : "Init";
      select.disabled = Boolean(entry.error);
      select.addEventListener("click", () => {
        if (entry.indexed) switchProject(entry.path);
        else showConfirm(entry.path);
      });
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "Open";
      open.disabled = Boolean(entry.error);
      open.addEventListener("click", () => loadDirs(entry.path));
      actions.append(select, open);
      entriesEl.appendChild(row);
    });
  }

  function rowShell(title, path, status) {
    const row = document.createElement("div");
    row.className = "project-switcher-row";
    const main = document.createElement("div");
    main.className = "project-switcher-row-main";
    const titleEl = document.createElement("div");
    titleEl.className = "project-switcher-row-title";
    titleEl.textContent = title || "Project";
    const pathEl = document.createElement("div");
    pathEl.className = "project-switcher-path";
    pathEl.textContent = normalizePath(path);
    main.append(titleEl, pathEl);
    const side = document.createElement("div");
    side.className = "project-switcher-actions";
    if (status) {
      const statusEl = document.createElement("span");
      statusEl.className = "project-switcher-status";
      statusEl.textContent = status;
      side.appendChild(statusEl);
    }
    row.append(main, side);
    return row;
  }

  function emptyRow(text) {
    const row = document.createElement("div");
    row.className = "project-switcher-message";
    row.textContent = text;
    return row;
  }

  function showConfirm(path) {
    state.pendingInitPath = path;
    if (confirmText) confirmText.textContent = `${basename(path)} is not indexed. Initialize it now?`;
    if (confirmEl) confirmEl.hidden = false;
  }

  function hideConfirm() {
    state.pendingInitPath = "";
    if (confirmEl) confirmEl.hidden = true;
  }

  async function initializeProject(path) {
    hideConfirm();
    setMessage(`Initializing ${basename(path)}.`);
    try {
      const result = await requestJson(api.switchProject, {
        method: "POST",
        body: JSON.stringify({ path, initialize: true }),
        headers: { "Content-Type": "application/json" }
      });
      if (result.status === "done") {
        await switchProject(path);
        return;
      }
      pollInit(path);
    } catch (err) {
      setMessage(errorMessage(err));
    }
  }

  async function pollInit(path) {
    clearPoll();
    try {
      const result = await requestJson(`${api.initStatus}?${new URLSearchParams({ path }).toString()}`);
      if (result.status === "done") {
        await switchProject(path);
        return;
      }
      if (result.status === "error") {
        setMessage(result.message || "Initialization failed.");
        showConfirm(path);
        return;
      }
      setMessage(`Initializing ${basename(path)} for ${result.elapsed || 0}s.`);
      state.pollTimer = window.setTimeout(() => pollInit(path), 2000);
    } catch (err) {
      setMessage(errorMessage(err));
      showConfirm(path);
    }
  }

  async function switchProject(path) {
    if (!path) return;
    if (isCurrent(path)) {
      closeSwitcher();
      return;
    }
    setMessage(`Switching to ${basename(path)}.`);
    try {
      const result = await requestJson(api.switchProject, {
        method: "POST",
        body: JSON.stringify({ path }),
        headers: { "Content-Type": "application/json" }
      });
      if (result.needs_init) {
        showConfirm(result.path || path);
        setMessage(`${basename(path)} needs an index first.`);
        return;
      }
      if (result.ok) {
        closeSwitcher();
        window.location.reload();
      }
    } catch (err) {
      setMessage(errorMessage(err));
    }
  }

  function renderBreadcrumbs(path, parent) {
    if (!breadcrumbsEl) return;
    const cleanPath = normalizePath(path);
    const cleanParent = normalizePath(parent);
    breadcrumbsEl.textContent = cleanPath ? `Path: ${cleanPath}${cleanParent && cleanParent !== cleanPath ? ` | Parent: ${cleanParent}` : ""}` : "";
  }

  async function requestJson(path, options) {
    const init = {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      ...(options || {})
    };
    init.headers = { Accept: "application/json", ...(init.headers || {}) };
    const response = await fetch(path, init);
    const text = await response.text();
    let body = {};
    if (text) {
      try {
        body = JSON.parse(text);
      } catch (_err) {
        body = { raw: text };
      }
    }
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  function setMessage(text) {
    if (!messageEl) return;
    messageEl.hidden = false;
    messageEl.textContent = text;
  }

  function hideMessage() {
    if (messageEl) messageEl.hidden = true;
  }

  function clearPoll() {
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function isCurrent(path) {
    return normalizePath(path) === state.activePath;
  }

  function safePath(value, fallback) {
    const raw = typeof value === "string" ? value.trim() : "";
    if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return fallback;
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw)) return fallback;
    return raw;
  }

  function readEmbeddedPayload() {
    const el = document.getElementById("graph-data") || document.getElementById("mobile-data");
    if (!el) return {};
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (_err) {
      return {};
    }
  }

  function basename(path) {
    const text = normalizePath(path);
    const parts = text.split("/").filter(Boolean);
    return parts[parts.length - 1] || text || "Project";
  }

  function normalizePath(path) {
    return String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
  }

  function errorMessage(err) {
    return err && err.message ? err.message : "Project switcher request failed.";
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }
})();
"""


__all__ = [
    "PROJECT_SWITCHER_CSS",
    "PROJECT_SWITCHER_HTML",
    "PROJECT_SWITCHER_SCRIPT",
]
