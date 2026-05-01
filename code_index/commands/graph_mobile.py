"""Phone-first HTML renderer for the live graph server."""

from __future__ import annotations

import json
from typing import Any


def _json_for_html(payload: dict[str, Any]) -> str:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


MOBILE_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#090b0f">
  <title>Graph Agent Companion Mobile</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #090b0f;
      --panel: #161a22;
      --panel-strong: #202631;
      --text: #f5f7fb;
      --muted: #a8b1c2;
      --line: #333b4a;
      --accent: #2dd4bf;
      --accent-ink: #06110f;
      --accent-soft: rgba(45, 212, 191, 0.16);
      --ok: #34d399;
      --warn: #fbbf24;
      --bad: #fb7185;
      --shadow: 0 14px 34px rgba(0, 0, 0, 0.36);
    }
    * {
      box-sizing: border-box;
    }
    html {
      min-height: 100%;
      background: var(--bg);
    }
    body {
      min-height: 100%;
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      font-size: 15px;
      line-height: 1.4;
      letter-spacing: 0;
    }
    button,
    input,
    select,
    textarea {
      font: inherit;
      letter-spacing: 0;
    }
    button {
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 10px 12px;
      touch-action: manipulation;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: var(--accent-ink);
      font-weight: 650;
    }
    button.ghost {
      background: transparent;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    input,
    select,
    textarea {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #11161d;
      color: var(--text);
      padding: 10px 12px;
    }
    textarea {
      min-height: 132px;
      resize: vertical;
    }
    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
    }
    .app {
      min-height: 100svh;
      padding-bottom: calc(92px + env(safe-area-inset-bottom));
    }
    [hidden] {
      display: none !important;
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 12;
      display: grid;
      gap: 8px;
      padding: calc(10px + env(safe-area-inset-top)) 12px 10px;
      border-bottom: 1px solid var(--line);
      background: rgba(9, 11, 15, 0.94);
      backdrop-filter: blur(12px);
    }
    .topbar-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .topbar-title {
      min-width: 0;
    }
    .topbar-actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .topbar-actions button {
      min-height: 40px;
      padding: 8px 10px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }
    .context-toggle[aria-expanded="true"] {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
    }
    .top-context-details {
      display: grid;
      gap: 9px;
      border-top: 1px solid rgba(51, 59, 74, 0.72);
      padding-top: 9px;
    }
    .top-context-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .top-context-actions {
      display: flex;
      gap: 8px;
    }
    .top-context-actions button {
      min-height: 40px;
      padding: 8px 10px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }
    .kicker {
      margin: 0 0 2px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    h1,
    h2,
    h3,
    p {
      margin-top: 0;
    }
    h1 {
      margin-bottom: 2px;
      font-size: 18px;
      line-height: 1.2;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    h2 {
      margin-bottom: 10px;
      font-size: 17px;
    }
    h3 {
      margin-bottom: 8px;
      font-size: 15px;
    }
    .meta {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .screen {
      display: none;
      padding: 14px;
    }
    .screen.active {
      display: grid;
      gap: 14px;
    }
    .status {
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
    }
    .status strong {
      color: var(--text);
    }
    .view-strip {
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding: 0;
      scrollbar-width: none;
    }
    .view-strip::-webkit-scrollbar {
      display: none;
    }
    .view-button {
      flex: 0 0 auto;
      min-width: 86px;
      border-color: var(--line);
      background: var(--panel);
      color: var(--muted);
      font-size: 13px;
      font-weight: 750;
    }
    .view-button[aria-pressed="true"] {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
    }
    .graph-stage {
      position: relative;
      min-height: calc(100svh - 190px);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0c1118;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .mobile-graph {
      display: block;
      width: 100%;
      height: min(62svh, 620px);
      min-height: 430px;
      background: radial-gradient(circle at 50% 42%, #18202b 0, #10151d 54%, #090b0f 100%);
      touch-action: none;
    }
    .mobile-graph .edge {
      stroke: #465266;
      stroke-width: 1.35;
      stroke-opacity: 0.72;
      vector-effect: non-scaling-stroke;
    }
    .mobile-graph .node circle {
      fill: #151c26;
      stroke: var(--accent);
      stroke-width: 2;
      filter: drop-shadow(0 5px 12px rgba(0, 0, 0, 0.34));
    }
    .mobile-graph .node.active circle {
      fill: var(--accent-soft);
      stroke: var(--accent);
      stroke-width: 3;
    }
    .mobile-graph .node text {
      fill: var(--text);
      font-size: 11px;
      font-weight: 700;
      pointer-events: none;
      paint-order: stroke;
      stroke: #090b0f;
      stroke-width: 3px;
      stroke-linejoin: round;
    }
    .graph-overlay {
      position: absolute;
      right: 10px;
      bottom: 10px;
      left: 10px;
      display: grid;
      gap: 8px;
      pointer-events: none;
    }
    .graph-overlay .toolbar,
    .graph-overlay .quick-actions {
      pointer-events: auto;
    }
    .quick-actions {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .quick-actions button,
    .graph-tools button {
      min-height: 42px;
      padding: 8px 9px;
      background: rgba(22, 26, 34, 0.92);
      backdrop-filter: blur(10px);
    }
    .quick-actions button:first-child {
      border-color: var(--accent);
      background: rgba(45, 212, 191, 0.92);
      color: var(--accent-ink);
      font-weight: 750;
    }
    .graph-tools {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .selection-bar {
      display: grid;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      box-shadow: var(--shadow);
    }
    .section-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .section-row h2,
    .section-row p {
      margin-bottom: 0;
    }
    .section-row strong {
      display: block;
      margin-top: 2px;
      color: var(--text);
      font-size: 14px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .section-row button {
      min-height: 38px;
      padding: 7px 10px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }
    .chat-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .chat-head h2 {
      margin-bottom: 0;
      font-size: 19px;
    }
    .chat-head button {
      min-height: 40px;
      padding: 8px 11px;
      font-size: 13px;
      font-weight: 750;
      white-space: nowrap;
    }
    .chat-file-picker {
      background: linear-gradient(180deg, rgba(45, 212, 191, 0.1), var(--panel) 58%);
      border-color: rgba(45, 212, 191, 0.42);
    }
    .chat-form textarea {
      min-height: clamp(156px, 30svh, 260px);
      font-size: 16px;
      line-height: 1.45;
    }
    .chat-history-shell,
    .stream-panel {
      display: grid;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      box-shadow: var(--shadow);
    }
    .chat-history {
      display: grid;
      gap: 9px;
    }
    .chat-message,
    .stream-card {
      display: grid;
      gap: 7px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #11161d;
      padding: 10px;
    }
    .chat-message.user {
      border-color: rgba(45, 212, 191, 0.48);
      background: rgba(45, 212, 191, 0.09);
    }
    .chat-message.agent,
    .stream-card.active {
      border-color: rgba(52, 211, 153, 0.34);
    }
    .chat-message.stream {
      border-color: rgba(143, 183, 255, 0.28);
    }
    .message-body,
    .stream-body {
      margin: 0;
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .stream-list {
      display: grid;
      gap: 9px;
    }
    .work-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .work-list .chip {
      border-radius: 8px;
      background: var(--panel-strong);
    }
    .advanced-controls {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .advanced-controls summary {
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 10px 12px;
      color: var(--muted);
      cursor: pointer;
      font-size: 13px;
      font-weight: 750;
      list-style: none;
    }
    .advanced-controls summary::-webkit-details-marker {
      display: none;
    }
    .advanced-controls summary::after {
      content: "Show";
      color: var(--accent);
      font-size: 12px;
    }
    .advanced-controls[open] summary::after {
      content: "Hide";
    }
    .advanced-body {
      display: grid;
      gap: 12px;
      border-top: 1px solid var(--line);
      padding: 12px;
    }
    .chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      display: inline-flex;
      min-width: 0;
      max-width: 100%;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-strong);
      color: var(--text);
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }
    .chip button {
      min-height: 22px;
      border: 0;
      border-radius: 999px;
      background: transparent;
      padding: 0 3px;
      color: var(--muted);
      font-size: 14px;
    }
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }
    .stat {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      box-shadow: var(--shadow);
    }
    .stat span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .stat strong {
      display: block;
      margin-top: 3px;
      overflow: hidden;
      font-size: 18px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .toolbar,
    .form-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .toolbar button,
    .form-actions button {
      flex: 1 1 128px;
    }
    .orchestrator-intents {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .orchestrator-intents button[aria-pressed="true"] {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
      font-weight: 750;
    }
    .lane-list {
      display: grid;
      gap: 12px;
    }
    .lane {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .lane-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-strong);
    }
    .lane-head h3 {
      margin: 0;
    }
    .count {
      min-width: 28px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 750;
      line-height: 24px;
      text-align: center;
    }
    .run-list,
    .result-list,
    .event-list {
      display: grid;
      gap: 8px;
      padding: 10px;
    }
    .empty {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }
    .run-card,
    .result-card,
    .event-card,
    .notice {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
    }
    .run-card {
      display: grid;
      gap: 8px;
    }
    .run-main {
      display: grid;
      gap: 4px;
      min-width: 0;
    }
    .run-title {
      overflow-wrap: anywhere;
      font-weight: 700;
    }
    .run-detail {
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .badge-row {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .badge {
      border-radius: 999px;
      background: var(--panel-strong);
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      padding: 3px 8px;
    }
    .badge.ok {
      background: rgba(52, 211, 153, 0.14);
      color: var(--ok);
    }
    .badge.warn {
      background: rgba(251, 191, 36, 0.14);
      color: var(--warn);
    }
    .badge.bad {
      background: rgba(251, 113, 133, 0.14);
      color: var(--bad);
    }
    .card-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .search-form,
    .task-form {
      display: grid;
      gap: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      box-shadow: var(--shadow);
    }
    .two-up {
      display: grid;
      gap: 10px;
    }
    .preflight-box {
      display: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-strong);
      padding: 10px;
      color: var(--muted);
      font-size: 13px;
    }
    .preflight-box.active {
      display: block;
    }
    .preflight-box strong {
      color: var(--text);
    }
    .transcript {
      display: grid;
      gap: 10px;
    }
    .transcript-head {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      box-shadow: var(--shadow);
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .event-card {
      display: grid;
      gap: 4px;
    }
    .debug-grid {
      display: grid;
      gap: 10px;
    }
    .debug-row {
      display: grid;
      grid-template-columns: minmax(96px, 0.4fr) 1fr;
      gap: 8px;
      border-bottom: 1px solid var(--line);
      padding: 8px 0;
    }
    .debug-row span:first-child {
      color: var(--muted);
      font-weight: 650;
    }
    .mobile-dock {
      position: fixed;
      right: 0;
      bottom: 0;
      left: 0;
      z-index: 10;
      display: grid;
      gap: 0;
      padding: 6px 8px calc(6px + env(safe-area-inset-bottom));
      border-top: 1px solid var(--line);
      background: rgba(9, 11, 15, 0.96);
      box-shadow: 0 -8px 28px rgba(0, 0, 0, 0.3);
      backdrop-filter: blur(12px);
    }
    .dock-main,
    .dock-drawer {
      display: grid;
      gap: 6px;
    }
    .dock-main {
      grid-template-columns: minmax(94px, 1.35fr) minmax(76px, 1fr) minmax(76px, 1fr) 52px;
      align-items: stretch;
    }
    .dock-drawer {
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-top: 1px solid rgba(51, 59, 74, 0.72);
      margin-top: 6px;
      padding-top: 6px;
    }
    .tab-button {
      min-width: 0;
      min-height: 50px;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: var(--muted);
      padding: 7px 4px;
      font-size: 12px;
      font-weight: 750;
    }
    .tab-button[aria-selected="true"] {
      background: var(--accent-soft);
      color: var(--accent);
    }
    .tab-button.primary {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: var(--accent-ink);
      font-size: 13px;
    }
    .tab-button.primary[aria-selected="true"] {
      color: var(--accent-ink);
    }
    .dock-toggle {
      min-width: 0;
      min-height: 50px;
      border: 0;
      border-radius: 8px;
      background: var(--panel);
      color: var(--muted);
      padding: 7px 4px;
      font-size: 12px;
      font-weight: 750;
    }
    .dock-toggle[aria-expanded="true"] {
      background: var(--accent-soft);
      color: var(--accent);
    }
    .tab-button span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    @media (min-width: 720px) {
      .app {
        max-width: 680px;
        margin: 0 auto;
      }
      .mobile-dock {
        right: 50%;
        left: 50%;
        width: min(680px, 100vw);
        transform: translateX(-50%);
      }
      .two-up {
        grid-template-columns: 1fr 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="topbar-row">
        <div class="topbar-title">
          <p class="kicker">Graph Agent Companion</p>
          <h1 id="repo-title">Mobile graph</h1>
        </div>
        <div class="topbar-actions">
          <button type="button" class="context-toggle" id="top-context-toggle" aria-expanded="false" aria-controls="top-context-details">Context</button>
          <button type="button" id="refresh-current" aria-label="Refresh current tab">Refresh</button>
        </div>
      </div>
      <div class="top-context-details" id="top-context-details" hidden>
        <div class="top-context-grid">
          <div>
            <p class="meta" id="repo-meta">Preparing graph-server data</p>
            <p class="meta" id="top-selected-count">No files selected.</p>
          </div>
          <div class="top-context-actions">
            <button class="primary" type="button" data-open-view="task">Chat</button>
            <button type="button" data-open-view="files">Files</button>
          </div>
        </div>
        <div class="view-strip" role="navigation" aria-label="Mobile view shortcuts">
          <button class="view-button" type="button" aria-pressed="true" data-view-button="graph" data-tab="graph">Graph</button>
          <button class="view-button" type="button" aria-pressed="false" data-view-button="board" data-tab="board">Board</button>
          <button class="view-button" type="button" aria-pressed="false" data-view-button="task" data-tab="task">Chat</button>
          <button class="view-button" type="button" aria-pressed="false" data-view-button="files" data-tab="files">Files</button>
          <button class="view-button" type="button" aria-pressed="false" data-view-button="runs" data-tab="runs">Runs</button>
          <button class="view-button" type="button" aria-pressed="false" data-view-button="debug" data-tab="debug">Debug</button>
        </div>
      </div>
    </header>

    <main>
      <section class="screen active" id="panel-graph" role="tabpanel" aria-labelledby="dock-graph" tabindex="0">
        <div class="graph-stage">
          <svg class="mobile-graph" id="mobile-graph-svg" role="img" aria-label="Repository graph network" viewBox="0 0 1000 700">
            <g id="mobile-graph-viewport">
              <g id="mobile-graph-edges"></g>
              <g id="mobile-graph-nodes"></g>
            </g>
          </svg>
          <div class="graph-overlay">
            <div class="graph-tools" aria-label="Graph controls">
              <button type="button" id="graph-load" aria-label="Refresh graph network">Load</button>
              <button type="button" id="graph-fit" aria-label="Fit graph to screen">Fit</button>
              <button type="button" id="graph-zoom-out" aria-label="Zoom graph out">-</button>
              <button type="button" id="graph-zoom-in" aria-label="Zoom graph in">+</button>
            </div>
            <div class="quick-actions" aria-label="Graph view shortcuts">
              <button type="button" data-open-view="task">Chat</button>
              <button type="button" data-open-view="files">Files</button>
              <button type="button" data-open-view="runs">Runs</button>
              <button type="button" data-open-view="board">Board</button>
            </div>
          </div>
        </div>
        <div class="status" id="graph-status" aria-live="polite">Use two fingers to zoom. Drag with one finger to pan.</div>
      </section>

      <section class="screen" id="panel-board" role="tabpanel" aria-labelledby="tab-board" tabindex="0">
        <div class="stat-grid" aria-label="Repository summary">
          <div class="stat"><span>Files</span><strong id="stat-files">0</strong></div>
          <div class="stat"><span>Runs</span><strong id="stat-runs">0</strong></div>
          <div class="stat"><span>Status</span><strong id="stat-status">Idle</strong></div>
        </div>
        <div class="status" id="board-status" aria-live="polite">Loading board.</div>
        <div class="lane-list" id="board-lanes" aria-label="Agent Task board"></div>
      </section>

      <section class="screen" id="panel-files" role="tabpanel" aria-labelledby="dock-files" tabindex="0">
        <div class="selection-bar" aria-label="Selected files">
          <div>
            <h2>Files</h2>
            <p class="meta">Search, select paths, then jump into chat with that context.</p>
          </div>
          <div class="chip-row" id="selected-file-chips"></div>
          <div class="toolbar">
            <button type="button" id="files-open-chat">Open chat</button>
            <button type="button" id="files-clear">Clear files</button>
          </div>
        </div>
        <form class="search-form" id="search-form">
          <label for="search-query">Search files, symbols, and Agent Run transcripts
            <input id="search-query" name="q" type="search" autocomplete="off" placeholder="Search this repo" aria-label="Search query">
          </label>
          <div class="toolbar">
            <button class="primary" type="submit">Search</button>
            <button class="ghost" id="clear-search" type="button">Clear</button>
          </div>
        </form>
        <div class="status" id="search-status" aria-live="polite">Enter a query to search graph-server.</div>
        <div class="result-list" id="search-results" aria-label="Search results"></div>
      </section>

      <section class="screen" id="panel-task" role="tabpanel" aria-labelledby="dock-chat" tabindex="0">
        <form class="task-form chat-form" id="task-form">
          <div class="chat-head">
            <div>
              <p class="kicker">Agent Chat</p>
              <h2>Orchestrator Chat</h2>
            </div>
            <button class="primary" type="button" id="chat-add-files">Add files</button>
          </div>
          <div class="selection-bar chat-file-picker" id="chat-file-picker" aria-label="Chat file context">
            <div class="section-row">
              <div>
                <p class="meta">File context</p>
                <strong id="chat-selected-count">No files selected.</strong>
              </div>
              <button type="button" id="chat-clear-files">Clear</button>
            </div>
            <div class="chip-row" id="chat-file-chips"></div>
          </div>
          <label for="task-message">Message
            <textarea id="task-message" name="message" placeholder="Ask the agent to inspect, change, or explain the selected files" aria-label="Agent Task message" required></textarea>
          </label>
          <details class="advanced-controls" id="chat-advanced-controls">
            <summary>Run options</summary>
            <div class="advanced-body">
              <div class="orchestrator-intents" aria-label="Targeted run intent">
                <button type="button" data-orchestrator-intent="implement" aria-pressed="true">Implement</button>
                <button type="button" data-orchestrator-intent="impact" aria-pressed="false">Impact</button>
                <button type="button" data-orchestrator-intent="tests" aria-pressed="false">Tests</button>
              </div>
              <div class="two-up">
                <label for="task-agent">Agent name
                  <input id="task-agent" name="agent_name" autocomplete="off" placeholder="Codex">
                </label>
                <label for="task-provider">Provider
                  <select id="task-provider" name="provider" aria-label="Agent provider"></select>
                </label>
              </div>
              <label for="task-paths">Selected paths
                <input id="task-paths" name="selected_paths" autocomplete="off" placeholder="path/to/file.py">
              </label>
              <label for="task-blockers">Blocked by Agent Run IDs
                <input id="task-blockers" name="blocked_by_run_ids" autocomplete="off" placeholder="optional comma-separated run IDs">
              </label>
            </div>
          </details>
          <div class="preflight-box" id="preflight-box" aria-live="polite"></div>
          <div class="form-actions">
            <button class="primary" id="task-submit" type="submit">Submit task</button>
            <button type="button" id="task-send-run-message">Send to open run</button>
            <button type="button" id="task-reset">Reset</button>
          </div>
        </form>
        <div class="status" id="task-status" aria-live="polite">Preflight runs before dispatch.</div>
        <section class="chat-history-shell" aria-labelledby="chat-history-title">
          <div class="section-row">
            <div>
              <h2 id="chat-history-title">Chat history</h2>
              <p class="meta">Messages submitted from this phone session and open Agent Runs.</p>
            </div>
            <button type="button" id="chat-history-clear">Clear</button>
          </div>
          <div class="chat-history" id="chat-history" aria-label="Mobile chat history" aria-live="polite"></div>
        </section>
      </section>

      <section class="screen" id="panel-runs" role="tabpanel" aria-labelledby="tab-runs" tabindex="0">
        <div class="toolbar">
          <button class="primary" type="button" id="refresh-runs">Refresh runs</button>
          <button type="button" id="clear-transcript">Clear transcript</button>
        </div>
        <div class="status" id="runs-status" aria-live="polite">Select a run to inspect its transcript.</div>
        <section class="stream-panel" aria-labelledby="agent-streams-title">
          <div class="section-row">
            <div>
              <h2 id="agent-streams-title">Agent streams</h2>
              <p class="meta">Current work, latest event, and touched files.</p>
            </div>
            <button type="button" data-open-view="task">Open chat</button>
          </div>
          <div class="stream-list" id="agent-streams" aria-label="Agent streams and current work"></div>
        </section>
        <div class="run-list" id="runs-list" aria-label="Agent Runs"></div>
        <div class="transcript" id="run-transcript" aria-label="Selected Agent Run transcript"></div>
      </section>

      <section class="screen" id="panel-debug" role="tabpanel" aria-labelledby="tab-debug" tabindex="0">
        <div class="notice">
          <h2>Debug</h2>
          <div class="debug-grid" id="debug-grid"></div>
        </div>
        <div class="notice">
          <h3>Recent live events</h3>
          <div class="event-list" id="debug-events" aria-label="Recent Server-Sent Events"></div>
        </div>
      </section>
    </main>

    <nav class="mobile-dock" role="navigation" aria-label="Mobile navigation">
      <div class="dock-main">
        <button id="dock-chat" class="tab-button primary" type="button" role="tab" aria-controls="panel-task" aria-selected="false" data-tab="task" data-mobile-tab="chat"><span>Chat</span></button>
        <button id="dock-files" class="tab-button" type="button" role="tab" aria-controls="panel-files" aria-selected="false" data-tab="files" data-mobile-tab="files"><span>Files</span></button>
        <button id="dock-graph" class="tab-button" type="button" role="tab" aria-controls="panel-graph" aria-selected="true" data-tab="graph" data-mobile-tab="graph"><span>Graph</span></button>
        <button class="dock-toggle" id="bottom-context-toggle" type="button" aria-expanded="false" aria-controls="bottom-nav-drawer"><span>More</span></button>
      </div>
      <div class="dock-drawer" id="bottom-nav-drawer" hidden>
        <button class="tab-button" id="tab-board" type="button" role="tab" aria-controls="panel-board" aria-selected="false" data-tab="board" data-mobile-tab="board"><span>Board</span></button>
        <button class="tab-button" id="tab-runs" type="button" role="tab" aria-controls="panel-runs" aria-selected="false" data-tab="runs" data-mobile-tab="runs"><span>Runs</span></button>
        <button class="tab-button" id="tab-debug" type="button" role="tab" aria-controls="panel-debug" aria-selected="false" data-tab="debug" data-mobile-tab="debug"><span>Debug</span></button>
      </div>
    </nav>
  </div>
  <script id="mobile-data" type="application/json">__MOBILE_JSON__</script>
  <script>
(() => {
  "use strict";

  const dataEl = document.getElementById("mobile-data");
  let data = {};
  try {
    data = JSON.parse(dataEl ? dataEl.textContent || "{}" : "{}");
  } catch (err) {
    data = { parse_error: err && err.message ? err.message : "Invalid embedded data" };
  }

  const live = data.live && typeof data.live === "object" ? data.live : {};
  const api = {
    graph: sameOriginPath(live.graph_path, "/repo-graph.json"),
    board: sameOriginPath(live.agent_board_path, "/api/agent-board"),
    search: sameOriginPath(live.search_path, "/api/search"),
    preflight: sameOriginPath(live.agent_preflight_path, "/api/agent-task-preflight"),
    runs: sameOriginPath(live.agent_runs_path, "/api/agent-runs"),
    runDetail: sameOriginPath(live.agent_run_detail_path, "/api/agent-runs/{run_id}"),
    runMessages: sameOriginPath(live.agent_run_messages_path, "/api/agent-runs/{run_id}/messages"),
    runCancel: sameOriginPath(live.agent_run_cancel_path, "/api/agent-runs/{run_id}/cancel"),
    runAcceptReview: sameOriginPath(live.agent_run_accept_review_path, "/api/agent-runs/{run_id}/accept-review"),
    runArchive: sameOriginPath(live.agent_run_archive_path, "/api/agent-runs/{run_id}/archive"),
    events: sameOriginPath(live.events_path, "/events")
  };
  const columnsInOrder = ["blocked", "ready", "active", "review", "done"];
  const state = {
    tab: "graph",
    board: initialBoard(),
    graphPayload: null,
    graphTransform: { x: 0, y: 0, k: 1 },
    graphLayout: { nodes: [], edges: [] },
    selectedPaths: [],
    selectedRunId: null,
    orchestratorIntent: "implement",
    transcript: null,
    chatMessages: [],
    chatMessageKeys: new Set(),
    agentEvents: initialAgentEvents(),
    lastError: "",
    liveStatus: "not connected",
    eventLog: [],
    preflightConfirmation: false,
    preflightResult: null,
    searchResult: null
  };
  const graphPointers = new Map();
  let graphPanStart = null;
  let graphPinchStart = null;

  const els = {
    repoTitle: document.getElementById("repo-title"),
    repoMeta: document.getElementById("repo-meta"),
    topContextToggle: document.getElementById("top-context-toggle"),
    topContextDetails: document.getElementById("top-context-details"),
    topSelectedCount: document.getElementById("top-selected-count"),
    bottomContextToggle: document.getElementById("bottom-context-toggle"),
    bottomNavDrawer: document.getElementById("bottom-nav-drawer"),
    statFiles: document.getElementById("stat-files"),
    statRuns: document.getElementById("stat-runs"),
    statStatus: document.getElementById("stat-status"),
    graphSvg: document.getElementById("mobile-graph-svg"),
    graphViewport: document.getElementById("mobile-graph-viewport"),
    graphEdges: document.getElementById("mobile-graph-edges"),
    graphNodes: document.getElementById("mobile-graph-nodes"),
    graphStatus: document.getElementById("graph-status"),
    graphLoad: document.getElementById("graph-load"),
    graphFit: document.getElementById("graph-fit"),
    graphZoomIn: document.getElementById("graph-zoom-in"),
    graphZoomOut: document.getElementById("graph-zoom-out"),
    boardStatus: document.getElementById("board-status"),
    boardLanes: document.getElementById("board-lanes"),
    searchForm: document.getElementById("search-form"),
    searchQuery: document.getElementById("search-query"),
    searchStatus: document.getElementById("search-status"),
    searchResults: document.getElementById("search-results"),
    selectedFileChips: document.getElementById("selected-file-chips"),
    chatFileChips: document.getElementById("chat-file-chips"),
    chatSelectedCount: document.getElementById("chat-selected-count"),
    chatAddFiles: document.getElementById("chat-add-files"),
    chatClearFiles: document.getElementById("chat-clear-files"),
    chatHistory: document.getElementById("chat-history"),
    chatHistoryClear: document.getElementById("chat-history-clear"),
    filesOpenChat: document.getElementById("files-open-chat"),
    filesClear: document.getElementById("files-clear"),
    taskForm: document.getElementById("task-form"),
    taskMessage: document.getElementById("task-message"),
    taskAgent: document.getElementById("task-agent"),
    taskProvider: document.getElementById("task-provider"),
    taskPaths: document.getElementById("task-paths"),
    taskBlockers: document.getElementById("task-blockers"),
    taskSubmit: document.getElementById("task-submit"),
    taskSendRunMessage: document.getElementById("task-send-run-message"),
    taskReset: document.getElementById("task-reset"),
    taskStatus: document.getElementById("task-status"),
    preflightBox: document.getElementById("preflight-box"),
    runsStatus: document.getElementById("runs-status"),
    agentStreams: document.getElementById("agent-streams"),
    runsList: document.getElementById("runs-list"),
    runTranscript: document.getElementById("run-transcript"),
    debugGrid: document.getElementById("debug-grid"),
    debugEvents: document.getElementById("debug-events"),
    refreshCurrent: document.getElementById("refresh-current"),
    refreshRuns: document.getElementById("refresh-runs"),
    clearSearch: document.getElementById("clear-search"),
    clearTranscript: document.getElementById("clear-transcript")
  };

  boot();

  function boot() {
    renderShell();
    renderProviders();
    renderSelectedFiles();
    renderBoard();
    syncChatHistoryFromRuns();
    renderChatHistory();
    renderAgentStreams();
    renderRuns();
    renderDebug();
    bindTabs();
    bindForms();
    bindGraphGestures();
    bindEvents();
    loadGraph({ quiet: true });
    refreshBoard({ quiet: true });
  }

  function sameOriginPath(value, fallback) {
    const raw = typeof value === "string" ? value.trim() : "";
    if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return fallback;
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw)) return fallback;
    return raw;
  }

  function apiRunPath(runId) {
    return api.runDetail.replace("{run_id}", encodeURIComponent(runId));
  }

  function apiRunMessagePath(runId) {
    return api.runMessages.replace("{run_id}", encodeURIComponent(runId));
  }

  function apiCancelPath(runId) {
    return api.runCancel.replace("{run_id}", encodeURIComponent(runId));
  }

  function apiAcceptReviewPath(runId) {
    return api.runAcceptReview.replace("{run_id}", encodeURIComponent(runId));
  }

  function apiArchivePath(runId) {
    return api.runArchive.replace("{run_id}", encodeURIComponent(runId));
  }

  async function requestJson(path, options) {
    const init = {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      ...(options || {})
    };
    if (init.body && !(init.headers && init.headers["Content-Type"])) {
      init.headers = { ...init.headers, "Content-Type": "application/json" };
    }
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
    if (!response.ok) {
      const message = body && body.error ? body.error : `HTTP ${response.status}`;
      throw new Error(message);
    }
    return body;
  }

  function postJson(path, payload) {
    return requestJson(path, {
      method: "POST",
      body: JSON.stringify(payload || {}),
      headers: { Accept: "application/json", "Content-Type": "application/json" }
    });
  }

  function bindTabs() {
    document.querySelectorAll("[role='tab'][data-tab]").forEach(button => {
      button.addEventListener("click", () => openTab(button.dataset.tab));
    });
    document.querySelectorAll("[data-view-button][data-tab]").forEach(button => {
      button.addEventListener("click", () => openTab(button.dataset.tab));
    });
    document.querySelectorAll("[data-open-view]").forEach(button => {
      button.addEventListener("click", () => {
        openTab(button.dataset.openView);
      });
    });
  }

  function openTab(tab) {
    if (tab === "task") {
      openChatComposer();
      return;
    }
    setTab(tab);
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll("[role='tab'][data-tab]").forEach(button => {
      const selected = button.dataset.tab === tab;
      button.setAttribute("aria-selected", selected ? "true" : "false");
    });
    document.querySelectorAll("[data-view-button]").forEach(button => {
      const selected = button.dataset.viewButton === tab || button.dataset.tab === tab;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    document.querySelectorAll(".screen[id^='panel-']").forEach(panel => {
      panel.classList.toggle("active", panel.id === `panel-${tab}`);
    });
    const panel = document.getElementById(`panel-${tab}`);
    if (panel) panel.focus({ preventScroll: true });
    if (tab === "graph") loadGraph({ quiet: true });
    if (tab === "files" || tab === "task") renderSelectedFiles();
    if (tab === "runs") renderRuns();
    if (tab === "debug") renderDebug();
  }

  function bindForms() {
    els.refreshCurrent.addEventListener("click", () => refreshCurrentTab());
    els.topContextToggle.addEventListener("click", () => {
      toggleContextPanel(els.topContextToggle, els.topContextDetails);
    });
    els.bottomContextToggle.addEventListener("click", () => {
      toggleContextPanel(els.bottomContextToggle, els.bottomNavDrawer);
    });
    els.graphLoad.addEventListener("click", () => loadGraph({ quiet: false, force: true }));
    els.graphFit.addEventListener("click", () => fitGraph());
    els.graphZoomIn.addEventListener("click", () => zoomGraphBy(1.22));
    els.graphZoomOut.addEventListener("click", () => zoomGraphBy(1 / 1.22));
    els.refreshRuns.addEventListener("click", () => refreshBoard({ quiet: false, showRuns: true }));
    els.filesOpenChat.addEventListener("click", () => openChatComposer());
    els.chatAddFiles.addEventListener("click", () => openFilePicker());
    els.filesClear.addEventListener("click", () => clearSelectedPaths());
    els.chatClearFiles.addEventListener("click", () => clearSelectedPaths());
    els.chatHistoryClear.addEventListener("click", () => {
      state.chatMessages = [];
      state.chatMessageKeys = new Set();
      renderChatHistory();
    });
    els.taskPaths.addEventListener("input", () => {
      state.selectedPaths = splitList(els.taskPaths.value);
      renderSelectedFiles();
    });
    els.clearTranscript.addEventListener("click", () => {
      state.transcript = null;
      renderTranscript();
      setText(els.runsStatus, "Transcript cleared.");
    });
    els.clearSearch.addEventListener("click", () => {
      els.searchQuery.value = "";
      state.searchResult = null;
      renderSearchResults();
      setText(els.searchStatus, "Enter a query to search graph-server.");
    });
    els.searchForm.addEventListener("submit", event => {
      event.preventDefault();
      runSearch();
    });
    els.taskForm.addEventListener("submit", event => {
      event.preventDefault();
      submitTask();
    });
    document.querySelectorAll("[data-orchestrator-intent]").forEach(button => {
      button.addEventListener("click", () => setOrchestratorIntent(button.dataset.orchestratorIntent));
    });
    els.taskSendRunMessage.addEventListener("click", () => sendRunMessage());
    els.taskReset.addEventListener("click", () => resetTaskForm());
  }

  function toggleContextPanel(button, panel, forceExpanded) {
    if (!button || !panel) return;
    const current = button.getAttribute("aria-expanded") === "true";
    const expanded = typeof forceExpanded === "boolean" ? forceExpanded : !current;
    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    panel.hidden = !expanded;
  }

  function openChatComposer() {
    setTab("task");
    window.setTimeout(() => {
      if (els.taskMessage) els.taskMessage.focus();
    }, 0);
  }

  function openFilePicker() {
    setTab("files");
    window.setTimeout(() => {
      if (els.searchQuery) els.searchQuery.focus();
    }, 0);
  }

  function clearSelectedPaths() {
    state.selectedPaths = [];
    syncSelectedPathsInput();
    renderSelectedFiles();
    renderGraph();
  }

  function refreshCurrentTab() {
    if (state.tab === "graph") {
      loadGraph({ quiet: false, force: true });
      return;
    }
    if (state.tab === "search") {
      runSearch();
      return;
    }
    if (state.tab === "files") {
      runSearch();
      return;
    }
    if (state.tab === "task") {
      renderPreflight();
      return;
    }
    refreshBoard({ quiet: false, showRuns: state.tab === "runs" });
  }

  function bindEvents() {
    if (!window.EventSource) {
      state.liveStatus = "EventSource unavailable";
      renderDebug();
      return;
    }
    try {
      const source = new EventSource(api.events);
      source.onopen = () => {
        state.liveStatus = "connected";
        addLiveEvent("connection", "SSE connected");
        renderDebug();
      };
      source.onerror = () => {
        state.liveStatus = "disconnected";
        renderDebug();
      };
      source.addEventListener("agent", event => {
        addLiveEvent("agent", event.data || "");
        try {
          handleAgentSnapshot(JSON.parse(event.data || "{}"));
        } catch (_err) {
          renderDebug();
        }
        refreshBoard({ quiet: true });
      });
      source.addEventListener("graph", event => {
        addLiveEvent("graph", event.data || "graph changed");
        refreshBoard({ quiet: true });
      });
      source.addEventListener("connection", event => {
        addLiveEvent("connection", event.data || "");
        renderDebug();
      });
      source.addEventListener("perf:tick", event => {
        addLiveEvent("perf:tick", event.data || "");
        renderDebug();
      });
    } catch (err) {
      state.liveStatus = err && err.message ? err.message : "SSE failed";
      renderDebug();
    }
  }

  function addLiveEvent(type, detail) {
    state.eventLog.unshift({
      type,
      detail: compactText(detail, 360),
      at: new Date().toLocaleTimeString()
    });
    state.eventLog = state.eventLog.slice(0, 20);
  }

  async function loadGraph(options) {
    const force = options && options.force;
    const quiet = options && options.quiet;
    if (state.graphPayload && !force) {
      renderGraph();
      return;
    }
    if (!quiet) setText(els.graphStatus, "Loading graph network.");
    try {
      const payload = await requestJson(api.graph);
      state.graphPayload = payload;
      state.lastError = "";
      buildGraphLayout(payload);
      renderGraph();
      if (!quiet) setText(els.graphStatus, "Graph loaded. Use two fingers to zoom.");
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Graph load failed";
      setText(els.graphStatus, state.lastError);
      renderDebug();
    }
  }

  function buildGraphLayout(payload) {
    const nodes = Array.isArray(payload && payload.nodes) ? payload.nodes : [];
    const edges = Array.isArray(payload && payload.edges) ? payload.edges : [];
    const fileNodes = nodes
      .filter(node => node && node.kind === "file" && node.path)
      .slice()
      .sort((a, b) => {
        const rankA = Number(a.importance && a.importance.rank || 9999);
        const rankB = Number(b.importance && b.importance.rank || 9999);
        return rankA - rankB;
      })
      .slice(0, 72);
    const nodeById = new Map();
    const laidOut = fileNodes.map((node, index) => {
      const total = Math.max(1, fileNodes.length);
      const ring = index < 12 ? 150 : index < 34 ? 250 : 335;
      const angle = (index / total) * Math.PI * 2 - Math.PI / 2;
      const score = Number(node.importance && node.importance.score || 0);
      const radius = Math.max(8, Math.min(19, 8 + Math.sqrt(score || 1)));
      const item = {
        id: String(node.id || node.path || index),
        path: node.path || node.label || node.id || "",
        label: node.label || basename(node.path || node.id || ""),
        role: node.role || node.kind || "file",
        care: node.care_level || "",
        x: 500 + Math.cos(angle) * ring,
        y: 350 + Math.sin(angle) * ring,
        r: radius,
        raw: node
      };
      nodeById.set(item.id, item);
      if (item.path) nodeById.set(item.path, item);
      return item;
    });
    const laidEdges = edges
      .map(edge => {
        const source = nodeById.get(String(edge.source || ""));
        const target = nodeById.get(String(edge.target || ""));
        if (!source || !target || source === target) return null;
        return { source, target, kind: edge.kind || edge.label || "relation" };
      })
      .filter(Boolean)
      .slice(0, 120);
    state.graphLayout = { nodes: laidOut, edges: laidEdges };
    state.graphTransform = { x: 0, y: 0, k: 1 };
  }

  function renderGraph() {
    if (!els.graphNodes || !els.graphEdges) return;
    clear(els.graphNodes);
    clear(els.graphEdges);
    const layout = state.graphLayout || { nodes: [], edges: [] };
    if (!layout.nodes.length) {
      setText(els.graphStatus, "No graph nodes available yet.");
      return;
    }
    layout.edges.forEach(edge => {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", "edge");
      line.setAttribute("x1", String(edge.source.x));
      line.setAttribute("y1", String(edge.source.y));
      line.setAttribute("x2", String(edge.target.x));
      line.setAttribute("y2", String(edge.target.y));
      els.graphEdges.appendChild(line);
    });
    layout.nodes.forEach(node => {
      const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.setAttribute("class", `node ${state.selectedPaths.includes(node.path) ? "active" : ""}`.trim());
      group.setAttribute("transform", `translate(${node.x} ${node.y})`);
      group.setAttribute("tabindex", "0");
      group.setAttribute("role", "button");
      group.setAttribute("aria-label", node.path || node.label);
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("r", String(node.r));
      const textNode = document.createElementNS("http://www.w3.org/2000/svg", "text");
      textNode.setAttribute("x", String(node.r + 6));
      textNode.setAttribute("y", "4");
      textNode.textContent = compactText(node.label || node.path, 24);
      group.append(circle, textNode);
      group.addEventListener("click", event => {
        event.stopPropagation();
        selectPath(node.path, { focusChat: true });
      });
      group.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectPath(node.path, { focusChat: true });
        }
      });
      els.graphNodes.appendChild(group);
    });
    applyGraphTransform();
    setText(els.graphStatus, `${layout.nodes.length} files. Select a node, then open Files or Chat.`);
  }

  function bindGraphGestures() {
    if (!els.graphSvg) return;
    els.graphSvg.addEventListener("pointerdown", event => {
      const onNode = Boolean(event.target.closest && event.target.closest(".node"));
      graphPointers.set(event.pointerId, pointerPoint(event));
      try {
        els.graphSvg.setPointerCapture(event.pointerId);
      } catch (_err) {}
      if (graphPointers.size === 1 && !onNode) {
        graphPanStart = {
          pointer: pointerPoint(event),
          transform: { ...state.graphTransform }
        };
        graphPinchStart = null;
        event.preventDefault();
      } else if (graphPointers.size === 2) {
        graphPinchStart = pinchSnapshot();
        graphPanStart = null;
        event.preventDefault();
      } else if (onNode) {
        graphPanStart = null;
      }
    });
    els.graphSvg.addEventListener("pointermove", event => {
      if (!graphPointers.has(event.pointerId)) return;
      graphPointers.set(event.pointerId, pointerPoint(event));
      if (graphPointers.size >= 2 && graphPinchStart) {
        const next = pinchSnapshot();
        if (!next || !graphPinchStart.distance) return;
        const scale = clamp(graphPinchStart.transform.k * (next.distance / graphPinchStart.distance), 0.32, 4.4);
        zoomGraphAt(next.center.x, next.center.y, scale, graphPinchStart);
        return;
      }
      if (graphPointers.size === 1 && graphPanStart) {
        const point = pointerPoint(event);
        state.graphTransform.x = graphPanStart.transform.x + (point.x - graphPanStart.pointer.x);
        state.graphTransform.y = graphPanStart.transform.y + (point.y - graphPanStart.pointer.y);
        applyGraphTransform();
        event.preventDefault();
      }
    });
    els.graphSvg.addEventListener("pointerup", endGraphPointer);
    els.graphSvg.addEventListener("pointercancel", endGraphPointer);
    els.graphSvg.addEventListener("pointerleave", endGraphPointer);
    els.graphSvg.addEventListener("lostpointercapture", endGraphPointer);
  }

  function endGraphPointer(event) {
    graphPointers.delete(event.pointerId);
    try {
      els.graphSvg.releasePointerCapture(event.pointerId);
    } catch (_err) {}
    if (graphPointers.size === 1) {
      const remaining = Array.from(graphPointers.values())[0];
      graphPanStart = { pointer: remaining, transform: { ...state.graphTransform } };
      graphPinchStart = null;
    } else if (graphPointers.size === 0) {
      graphPanStart = null;
      graphPinchStart = null;
    } else {
      graphPinchStart = pinchSnapshot();
    }
  }

  function pointerPoint(event) {
    return { x: event.clientX, y: event.clientY };
  }

  function pinchSnapshot() {
    const points = Array.from(graphPointers.values());
    if (points.length < 2) return null;
    const a = points[0];
    const b = points[1];
    return {
      distance: pinchDistance(a, b),
      center: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      transform: { ...state.graphTransform }
    };
  }

  function pinchDistance(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function zoomGraphBy(factor) {
    const box = els.graphSvg ? els.graphSvg.getBoundingClientRect() : null;
    const centerX = box ? box.left + box.width / 2 : window.innerWidth / 2;
    const centerY = box ? box.top + box.height / 2 : window.innerHeight / 2;
    zoomGraphAt(centerX, centerY, clamp(state.graphTransform.k * factor, 0.32, 4.4));
  }

  function zoomGraphAt(clientX, clientY, nextScale, baseline) {
    if (!els.graphSvg) return;
    const box = els.graphSvg.getBoundingClientRect();
    const svgX = ((clientX - box.left) / Math.max(1, box.width)) * 1000;
    const svgY = ((clientY - box.top) / Math.max(1, box.height)) * 700;
    const base = baseline ? baseline.transform : state.graphTransform;
    const graphX = (svgX - base.x) / base.k;
    const graphY = (svgY - base.y) / base.k;
    state.graphTransform.k = nextScale;
    state.graphTransform.x = svgX - graphX * nextScale;
    state.graphTransform.y = svgY - graphY * nextScale;
    applyGraphTransform();
  }

  function fitGraph() {
    state.graphTransform = { x: 0, y: 0, k: 1 };
    applyGraphTransform();
  }

  function applyGraphTransform() {
    if (!els.graphViewport) return;
    const transform = state.graphTransform;
    els.graphViewport.setAttribute("transform", `translate(${transform.x} ${transform.y}) scale(${transform.k})`);
  }

  function initialAgentEvents() {
    const activity = data.activity && typeof data.activity === "object" ? data.activity : {};
    return Array.isArray(activity.agent_events) ? activity.agent_events.slice(0, 80) : [];
  }

  function renderShell() {
    const summary = data.summary && typeof data.summary === "object" ? data.summary : {};
    const agent = data.agent && typeof data.agent === "object" ? data.agent : {};
    const root = data.root || "graph-server";
    setText(els.repoTitle, basename(root) || "Mobile graph");
    setText(
      els.repoMeta,
      `${summary.node_count || 0} nodes, ${summary.edge_count || 0} edges, agent ${agent.name || "Agent"}`
    );
    setText(els.statFiles, String(summary.file_count || 0));
    setText(els.statStatus, agent.status || "idle");
  }

  function renderProviders() {
    const providers = Array.isArray(live.agent_providers) ? live.agent_providers : [];
    clear(els.taskProvider);
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Default";
    els.taskProvider.appendChild(empty);
    const seen = new Set();
    providers.forEach(provider => {
      if (!provider || !provider.id || seen.has(provider.id)) return;
      seen.add(provider.id);
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.display_name || provider.id;
      els.taskProvider.appendChild(option);
    });
    if (!providers.length) {
      ["codex", "claude", "kimi"].forEach(id => {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = id.charAt(0).toUpperCase() + id.slice(1);
        els.taskProvider.appendChild(option);
      });
    }
    const agent = data.agent && typeof data.agent === "object" ? data.agent : {};
    els.taskAgent.value = agent.name || "Codex";
  }

  function initialBoard() {
    const agent = data.agent && typeof data.agent === "object" ? data.agent : {};
    const kanban = agent.kanban && typeof agent.kanban === "object" ? agent.kanban : null;
    if (kanban && kanban.columns) return kanban;
    return null;
  }

  async function refreshBoard(options) {
    const quiet = options && options.quiet;
    const showRuns = options && options.showRuns;
    if (!quiet) setText(els.boardStatus, "Loading /api/agent-board.");
    try {
      const board = await requestJson(api.board);
      state.board = board;
      state.lastError = "";
      syncChatHistoryFromRuns();
      renderBoard();
      renderRuns();
      renderChatHistory();
      renderAgentStreams();
      renderDebug();
      if (showRuns) setTab("runs");
      if (!quiet) setText(els.boardStatus, "Board refreshed.");
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Board refresh failed";
      if (!quiet) setText(els.boardStatus, state.lastError);
      renderDebug();
    }
  }

  function renderBoard() {
    const board = state.board;
    const columns = board && board.columns && typeof board.columns === "object" ? board.columns : {};
    clear(els.boardLanes);
    const runs = flattenRuns();
    setText(els.statRuns, String(runs.length));
    columnsInOrder.forEach(name => {
      const column = columns[name] || { title: titleCase(name), runs: [] };
      const lane = document.createElement("section");
      lane.className = "lane";
      lane.setAttribute("aria-label", `${column.title || titleCase(name)} runs`);
      const head = document.createElement("div");
      head.className = "lane-head";
      const title = document.createElement("h3");
      title.textContent = column.title || titleCase(name);
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = String((column.runs || []).length);
      head.append(title, count);
      const list = document.createElement("div");
      list.className = "run-list";
      const columnRuns = Array.isArray(column.runs) ? column.runs : [];
      if (!columnRuns.length) {
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "No runs.";
        list.appendChild(empty);
      } else {
        columnRuns.forEach(run => list.appendChild(runCard(run)));
      }
      lane.append(head, list);
      els.boardLanes.appendChild(lane);
    });
    const counts = board && board.counts ? board.counts : {};
    setText(
      els.boardStatus,
      `Blocked ${counts.blocked || 0}, ready ${counts.ready || 0}, active ${counts.active || 0}, review ${counts.review || 0}.`
    );
  }

  function runCard(run) {
    const card = document.createElement("article");
    card.className = "run-card";
    const main = document.createElement("div");
    main.className = "run-main";
    const title = document.createElement("div");
    title.className = "run-title";
    title.textContent = runTitle(run);
    const detail = document.createElement("div");
    detail.className = "run-detail";
    detail.textContent = compactText(run.prompt || run.message || "No prompt", 150);
    const badges = document.createElement("div");
    badges.className = "badge-row";
    badges.appendChild(badge(run.status || "working", statusClass(run.status)));
    const health = run.run_health && run.run_health.health;
    if (health) badges.appendChild(badge(`health ${health}`, statusClass(health)));
    if ((run.active_files || []).length) badges.appendChild(badge(`${run.active_files.length} files`, ""));
    main.append(title, detail, badges);
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "Open";
    open.setAttribute("aria-label", `Open Agent Run ${run.run_id || ""}`);
    open.addEventListener("click", () => openRun(run.run_id));
    const chat = document.createElement("button");
    chat.type = "button";
    chat.textContent = "Open chat";
    chat.setAttribute("aria-label", `Open chat for Agent Run ${run.run_id || ""}`);
    chat.addEventListener("click", () => selectRunForChat(run));
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.setAttribute("aria-label", `Cancel Agent Run ${run.run_id || ""}`);
    cancel.disabled = !canCancel(run);
    cancel.addEventListener("click", () => cancelRun(run.run_id, cancel));
    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = "Accept";
    accept.setAttribute("aria-label", `Accept review for Agent Run ${run.run_id || ""}`);
    accept.disabled = String(run.status || "").toLowerCase() !== "review";
    accept.addEventListener("click", () => acceptReview(run.run_id, accept));
    const archive = document.createElement("button");
    archive.type = "button";
    archive.textContent = "Archive";
    archive.setAttribute("aria-label", `Archive Agent Run ${run.run_id || ""}`);
    archive.addEventListener("click", () => archiveRun(run.run_id, archive));
    actions.append(open, chat, cancel, accept, archive);
    card.append(main, actions);
    return card;
  }

  function renderRuns() {
    const runs = flattenRuns();
    clear(els.runsList);
    if (!runs.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No Agent Runs found on the board.";
      els.runsList.appendChild(empty);
    } else {
      runs.forEach(run => els.runsList.appendChild(runCard(run)));
    }
    setText(els.runsStatus, `${runs.length} Agent Runs loaded.`);
    renderAgentStreams();
    renderTranscript();
  }

  function flattenRuns() {
    const out = [];
    const seen = new Set();
    const board = state.board;
    const columns = board && board.columns && typeof board.columns === "object" ? board.columns : {};
    columnsInOrder.forEach(name => {
      const runs = columns[name] && Array.isArray(columns[name].runs) ? columns[name].runs : [];
      runs.forEach(run => {
        const id = run && run.run_id;
        if (!id || seen.has(id)) return;
        seen.add(id);
        out.push(run);
      });
    });
    const agent = data.agent && typeof data.agent === "object" ? data.agent : {};
    ["active_runs", "recent_runs"].forEach(key => {
      (Array.isArray(agent[key]) ? agent[key] : []).forEach(run => {
        const id = run && run.run_id;
        if (!id || seen.has(id)) return;
        seen.add(id);
        out.push(run);
      });
    });
    return out;
  }

  async function openRun(runId, options) {
    if (!runId) return;
    state.selectedRunId = runId;
    setText(els.runsStatus, `Loading ${runId}.`);
    try {
      const transcript = await requestJson(apiRunPath(runId));
      state.transcript = transcript;
      state.lastError = "";
      syncChatHistoryFromTranscript(transcript);
      renderTranscript();
      renderChatHistory();
      renderAgentStreams();
      if (!(options && options.stayOnCurrentTab)) {
        setTab("runs");
      }
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Run load failed";
      setText(els.runsStatus, state.lastError);
      renderDebug();
    }
  }

  async function cancelRun(runId, button) {
    return runAction(runId, apiCancelPath(runId), button, "Cancelled");
  }

  async function acceptReview(runId, button) {
    return runAction(runId, apiAcceptReviewPath(runId), button, "Accepted review for");
  }

  async function archiveRun(runId, button) {
    return runAction(runId, apiArchivePath(runId), button, "Archived");
  }

  async function runAction(runId, path, button, label) {
    if (!runId) return;
    const original = button ? button.textContent : "Action";
    if (button) {
      button.disabled = true;
      button.textContent = "Working";
    }
    try {
      const result = await postJson(path, {});
      if (result.board) state.board = result.board;
      setText(els.runsStatus, `${label} ${runId}.`);
      refreshBoard({ quiet: true });
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Run action failed";
      setText(els.runsStatus, state.lastError);
    } finally {
      if (button) {
        button.textContent = original;
        button.disabled = false;
      }
      renderRuns();
      renderDebug();
    }
  }

  function renderTranscript() {
    clear(els.runTranscript);
    const transcript = state.transcript;
    if (!transcript) return;
    const run = transcript.run || {};
    const head = document.createElement("div");
    head.className = "transcript-head";
    const title = document.createElement("h2");
    title.textContent = runTitle(run);
    const meta = document.createElement("p");
    meta.className = "meta mono";
    meta.textContent = run.run_id || "";
    const prompt = document.createElement("p");
    prompt.textContent = run.prompt || "No prompt recorded.";
    head.append(title, meta, prompt);
    els.runTranscript.appendChild(head);
    const events = Array.isArray(transcript.events) ? transcript.events : [];
    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No transcript events.";
      els.runTranscript.appendChild(empty);
      return;
    }
    events.forEach(event => {
      const card = document.createElement("article");
      card.className = "event-card";
      const top = document.createElement("div");
      top.className = "badge-row";
      top.appendChild(badge(event.event_type || "event", ""));
      if (event.timestamp) top.appendChild(badge(event.timestamp, ""));
      const msg = document.createElement("div");
      msg.textContent = event.message || event.file_path || "No message";
      const file = document.createElement("div");
      file.className = "run-detail mono";
      file.textContent = event.file_path || "";
      card.append(top, msg, file);
      els.runTranscript.appendChild(card);
    });
  }

  function renderChatHistory() {
    if (!els.chatHistory) return;
    clear(els.chatHistory);
    if (!state.chatMessages.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No chat messages yet.";
      els.chatHistory.appendChild(empty);
      return;
    }
    state.chatMessages.slice(-40).forEach(message => {
      const card = document.createElement("article");
      card.className = `chat-message ${message.role || "stream"}`.trim();
      const badges = document.createElement("div");
      badges.className = "badge-row";
      badges.appendChild(badge(message.role || "stream", message.role === "user" ? "" : statusClass(message.status)));
      if (message.status) badges.appendChild(badge(message.status, statusClass(message.status)));
      if (message.run_id) badges.appendChild(badge(String(message.run_id).slice(0, 8), ""));
      const body = document.createElement("p");
      body.className = "message-body";
      body.textContent = message.message || "";
      card.append(badges, body);
      if (message.file_path) {
        const file = document.createElement("div");
        file.className = "run-detail mono";
        file.textContent = message.file_path;
        card.appendChild(file);
      }
      if (message.run_id) {
        const actions = document.createElement("div");
        actions.className = "card-actions";
        const stream = document.createElement("button");
        stream.type = "button";
        stream.textContent = "Open stream";
        stream.addEventListener("click", () => openRun(message.run_id));
        const reply = document.createElement("button");
        reply.type = "button";
        reply.textContent = "Reply";
        reply.addEventListener("click", () => {
          state.selectedRunId = message.run_id;
          openChatComposer();
          setText(els.taskStatus, `Replying to ${message.run_id}.`);
        });
        actions.append(stream, reply);
        card.appendChild(actions);
      }
      els.chatHistory.appendChild(card);
    });
  }

  function renderAgentStreams() {
    if (!els.agentStreams) return;
    clear(els.agentStreams);
    const runs = flattenRuns();
    if (!runs.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No Agent Run streams yet.";
      els.agentStreams.appendChild(empty);
      return;
    }
    runs.forEach(run => {
      const latest = latestEventForRun(run);
      const files = workingFilesForRun(run);
      const card = document.createElement("article");
      card.className = `stream-card ${canCancel(run) ? "active" : ""}`.trim();
      const title = document.createElement("div");
      title.className = "run-title";
      title.textContent = runTitle(run);
      const badges = document.createElement("div");
      badges.className = "badge-row";
      badges.appendChild(badge(run.status || "working", statusClass(run.status)));
      if (latest && latest.event_type) {
        badges.appendChild(badge(latest.event_type, statusClass(latest.event_type)));
      }
      const body = document.createElement("p");
      body.className = "stream-body";
      body.textContent = compactText(
        (latest && latest.message) || run.prompt || run.message || "No current work summary.",
        220
      );
      const workList = document.createElement("div");
      workList.className = "work-list";
      if (!files.length) {
        const empty = document.createElement("span");
        empty.className = "meta";
        empty.textContent = "No files attached.";
        workList.appendChild(empty);
      } else {
        files.slice(0, 6).forEach(path => {
          const chip = document.createElement("span");
          chip.className = "chip";
          chip.textContent = path;
          workList.appendChild(chip);
        });
      }
      const actions = document.createElement("div");
      actions.className = "card-actions";
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "Open stream";
      open.addEventListener("click", () => openRun(run.run_id));
      const chat = document.createElement("button");
      chat.type = "button";
      chat.textContent = "Open chat";
      chat.addEventListener("click", () => selectRunForChat(run));
      actions.append(open, chat);
      card.append(title, badges, body, workList, actions);
      els.agentStreams.appendChild(card);
    });
  }

  function appendChatMessage(messageOrItem, options) {
    const opts = options || {};
    const item = typeof messageOrItem === "object" && messageOrItem !== null
      ? { ...messageOrItem }
      : { message: String(messageOrItem || "") };
    const message = String(item.message || "").trim();
    if (!message) return "";
    const key = item.key || opts.key || [
      item.role || opts.role || "user",
      item.run_id || opts.runId || "",
      item.created_at || opts.createdAt || "",
      item.status || opts.status || "",
      message
    ].join("|");
    if (state.chatMessageKeys.has(key)) return key;
    state.chatMessageKeys.add(key);
    state.chatMessages.push({
      key,
      role: item.role || opts.role || "user",
      message,
      run_id: item.run_id || opts.runId || null,
      agent_name: item.agent_name || opts.agentName || null,
      status: item.status || opts.status || "",
      file_path: item.file_path || opts.filePath || "",
      created_at: item.created_at || opts.createdAt || new Date().toISOString()
    });
    state.chatMessages = state.chatMessages.slice(-80);
    renderChatHistory();
    return key;
  }

  function updateChatMessage(key, patch) {
    if (!key) return;
    const item = state.chatMessages.find(message => message.key === key);
    if (!item) return;
    Object.assign(item, patch || {});
    renderChatHistory();
  }

  function syncChatHistoryFromRuns(runs) {
    (runs || flattenRuns()).forEach(run => {
      if (!run || !run.run_id || !(run.prompt || run.message)) return;
      appendChatMessage({
        key: `run:${run.run_id}:prompt`,
        role: "user",
        run_id: run.run_id,
        agent_name: run.agent_name,
        status: run.status || "queued",
        message: run.prompt || run.message,
        created_at: run.started_at || run.updated_at || ""
      });
    });
  }

  function syncChatHistoryFromTranscript(transcript) {
    if (!transcript || !transcript.run) return;
    syncChatHistoryFromRuns([transcript.run]);
    const events = Array.isArray(transcript.events) ? transcript.events : [];
    syncChatHistoryFromEvents(events, transcript.run);
  }

  function syncChatHistoryFromEvents(events, run) {
    (Array.isArray(events) ? events : []).forEach(event => {
      if (!event) return;
      const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
      const type = String(event.event_type || "").toLowerCase();
      if (!event.message && !event.file_path) return;
      const role = payload.same_run_message
        ? "user"
        : (type === "decision" ? "agent" : "stream");
      appendChatMessage({
        key: `event:${eventIdentity(event)}`,
        role,
        run_id: event.run_id || (run && run.run_id),
        agent_name: event.agent_name || (run && run.agent_name),
        status: event.event_type || "",
        message: event.message || event.file_path || "",
        file_path: event.file_path || "",
        created_at: event.timestamp || ""
      });
    });
  }

  function handleAgentSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    if (snapshot.agent && typeof snapshot.agent === "object") {
      data.agent = { ...(data.agent || {}), ...snapshot.agent };
      if (snapshot.agent.kanban && snapshot.agent.kanban.columns) {
        state.board = snapshot.agent.kanban;
      }
    }
    if (snapshot.activity && typeof snapshot.activity === "object") {
      data.activity = { ...(data.activity || {}), ...snapshot.activity };
      state.agentEvents = uniqueEvents(
        (snapshot.activity.agent_events || []).concat(state.agentEvents || [])
      ).slice(0, 100);
      syncChatHistoryFromEvents(state.agentEvents);
    }
    syncChatHistoryFromRuns();
    renderShell();
    renderBoard();
    renderRuns();
    renderChatHistory();
    renderAgentStreams();
    renderDebug();
  }

  function selectRunForChat(run) {
    if (!run || !run.run_id) return;
    state.selectedRunId = run.run_id;
    workingFilesForRun(run).forEach(path => {
      if (path && !state.selectedPaths.includes(path)) state.selectedPaths.push(path);
    });
    syncSelectedPathsInput();
    renderSelectedFiles();
    openChatComposer();
    setText(els.taskStatus, `Replying to ${runTitle(run)}.`);
  }

  function workingFilesForRun(run) {
    const metadata = run && run.metadata && typeof run.metadata === "object" ? run.metadata : {};
    const latest = latestEventForRun(run);
    return uniqueList(
      []
        .concat((run && run.active_files) || [])
        .concat(metadata.selected_paths || [])
        .concat(latest && latest.file_path ? [latest.file_path] : [])
    );
  }

  function latestEventForRun(run) {
    const runId = run && run.run_id;
    if (!runId) return null;
    return (state.agentEvents || []).find(event => event && event.run_id === runId) || null;
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

  function uniqueEvents(events) {
    const seen = new Set();
    const out = [];
    (events || []).forEach(event => {
      const key = eventIdentity(event);
      if (!key || seen.has(key)) return;
      seen.add(key);
      out.push(event);
    });
    return out;
  }

  async function runSearch() {
    const query = els.searchQuery.value.trim();
    if (!query) {
      setText(els.searchStatus, "Enter a search query.");
      return;
    }
    setText(els.searchStatus, `Searching /api/search for ${query}.`);
    const params = new URLSearchParams({ q: query, scope: "all", limit: "20" });
    try {
      const result = await requestJson(`${api.search}?${params.toString()}`);
      state.searchResult = result;
      state.lastError = "";
      renderSearchResults();
      setText(els.searchStatus, `${(result.counts && result.counts.files) || 0} file results, ${(result.counts && result.counts.transcripts) || 0} transcript results.`);
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Search failed";
      state.searchResult = null;
      renderSearchResults();
      setText(els.searchStatus, state.lastError);
      renderDebug();
    }
  }

  function renderSearchResults() {
    clear(els.searchResults);
    const result = state.searchResult;
    if (!result) return;
    const files = Array.isArray(result.files) ? result.files : [];
    const transcripts = Array.isArray(result.transcripts) ? result.transcripts : [];
    if (!files.length && !transcripts.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No results.";
      els.searchResults.appendChild(empty);
      return;
    }
    files.forEach(item => els.searchResults.appendChild(resultCard(item, "file")));
    transcripts.forEach(item => els.searchResults.appendChild(resultCard(item, "transcript")));
  }

  function selectPath(path, options) {
    const clean = String(path || "").trim();
    if (!clean) return;
    if (!state.selectedPaths.includes(clean)) {
      state.selectedPaths.push(clean);
    }
    syncSelectedPathsInput();
    renderSelectedFiles();
    renderGraph();
    if (options && options.focusFiles) {
      setTab("files");
    }
    if (options && options.focusChat) {
      openChatComposer();
    }
  }

  function removeSelectedPath(path) {
    state.selectedPaths = state.selectedPaths.filter(item => item !== path);
    syncSelectedPathsInput();
    renderSelectedFiles();
    renderGraph();
  }

  function syncSelectedPathsInput() {
    els.taskPaths.value = state.selectedPaths.join(", ");
  }

  function renderSelectedFiles() {
    const paths = state.selectedPaths.slice();
    const countLabel = selectedCountLabel(paths.length);
    setText(els.chatSelectedCount, countLabel);
    setText(els.topSelectedCount, countLabel);
    [els.selectedFileChips, els.chatFileChips].forEach(target => {
      if (!target) return;
      clear(target);
      if (!paths.length) {
        const empty = document.createElement("span");
        empty.className = "meta";
        empty.textContent = "No files selected.";
        target.appendChild(empty);
        return;
      }
      paths.forEach(path => {
        const chip = document.createElement("span");
        chip.className = "chip";
        const label = document.createElement("span");
        label.textContent = path;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "x";
        remove.setAttribute("aria-label", `Remove ${path}`);
        remove.addEventListener("click", () => removeSelectedPath(path));
        chip.append(label, remove);
        target.appendChild(chip);
      });
    });
  }

  function selectedCountLabel(count) {
    if (!count) return "No files selected.";
    return `${count} file${count === 1 ? "" : "s"} selected.`;
  }

  function resultCard(item, kind) {
    const card = document.createElement("article");
    card.className = "result-card";
    const title = document.createElement("div");
    title.className = "run-title";
    title.textContent = item.file_path || item.symbol_path || item.run_id || kind;
    const snippet = document.createElement("p");
    snippet.className = "run-detail";
    snippet.textContent = compactText(item.snippet || item.message || item.prompt || "", 240);
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const usePath = document.createElement("button");
    usePath.type = "button";
    usePath.textContent = "Use path";
    usePath.disabled = !item.file_path;
    usePath.addEventListener("click", () => {
      selectPath(item.file_path || "", { focusFiles: true });
    });
    const openRunButton = document.createElement("button");
    openRunButton.type = "button";
    openRunButton.textContent = "Open run";
    openRunButton.disabled = !item.run_id;
    openRunButton.addEventListener("click", () => openRun(item.run_id));
    const askButton = document.createElement("button");
    askButton.type = "button";
    askButton.textContent = "Ask";
    askButton.disabled = !item.file_path;
    askButton.addEventListener("click", () => {
      selectPath(item.file_path || "", { focusChat: true });
    });
    actions.append(usePath, askButton, openRunButton);
    card.append(title, snippet, actions);
    return card;
  }

  function setOrchestratorIntent(intent, options) {
    state.orchestratorIntent = intent || "implement";
    document.querySelectorAll("[data-orchestrator-intent]").forEach(button => {
      const selected = button.dataset.orchestratorIntent === state.orchestratorIntent;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    const shouldFillPrompt = !(options && options.fillPrompt === false);
    if (shouldFillPrompt && !els.taskMessage.value.trim()) {
      els.taskMessage.value = orchestratorPrompt(state.orchestratorIntent);
    }
    setText(els.taskStatus, `Orchestrator intent: ${state.orchestratorIntent}. Submit creates a targeted Agent Run.`);
  }

  function orchestratorPrompt(intent) {
    const paths = uniqueList([...state.selectedPaths, ...splitList(els.taskPaths.value)]);
    const context = paths.length ? ` for ${paths.join(", ")}` : " for the selected graph context";
    if (intent === "impact") {
      return `Assess the implementation impact${context}. Identify risky dependencies, likely affected tests, and the smallest safe implementation path.`;
    }
    if (intent === "tests") {
      return `Create and run the targeted verification plan${context}. Prioritize affected tests and report any blocked checks separately from failures.`;
    }
    return `Implement the targeted change${context}. Keep the patch focused, respect active file claims, and run the narrowest useful verification.`;
  }

  async function sendRunMessage() {
    const runId = state.selectedRunId;
    const payload = taskPayload();
    if (!runId) {
      setText(els.taskStatus, "Open an Agent Run before sending a follow-up message.");
      return;
    }
    if (!payload.message) {
      setText(els.taskStatus, "Message is required before sending to the open run.");
      return;
    }
    const chatKey = appendChatMessage(payload.message, {
      role: "user",
      runId,
      status: "sending",
      key: `draft:${runId}:${payload.message}:${(payload.selected_paths || []).join("|")}`
    });
    els.taskSendRunMessage.disabled = true;
    setText(els.taskStatus, `Sending follow-up to ${runId}.`);
    try {
      const result = await postJson(apiRunMessagePath(runId), payload);
      if (result.board) state.board = result.board;
      if (result.event) {
        state.agentEvents = uniqueEvents([result.event].concat(state.agentEvents || []));
      }
      if (result.transcript) {
        state.transcript = result.transcript;
        syncChatHistoryFromTranscript(result.transcript);
      }
      updateChatMessage(chatKey, { run_id: runId, status: "queued" });
      setText(els.taskStatus, `Follow-up queued for ${runId}.`);
      await openRun(runId, { stayOnCurrentTab: true });
      setTab("task");
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Run message failed";
      updateChatMessage(chatKey, { status: "failed" });
      setText(els.taskStatus, state.lastError);
      renderDebug();
    } finally {
      els.taskSendRunMessage.disabled = false;
    }
  }

  async function submitTask() {
    const payload = taskPayload();
    if (!payload.message) {
      setText(els.taskStatus, "Agent Task message is required.");
      return;
    }
    const chatKey = appendChatMessage(payload.message, {
      role: "user",
      status: "preflight",
      key: `draft:new:${payload.message}:${(payload.selected_paths || []).join("|")}`
    });
    els.taskSubmit.disabled = true;
    setText(els.taskStatus, "Running /api/agent-task-preflight.");
    try {
      const preflight = await postJson(api.preflight, {
        ...payload,
        preflight_confirmed: state.preflightConfirmation
      });
      state.preflightResult = preflight;
      renderPreflight();
      const gate = normalizePreflight(preflight);
      if (gate.requires_confirmation && !state.preflightConfirmation) {
        state.preflightConfirmation = true;
        els.taskSubmit.disabled = false;
        els.taskSubmit.textContent = "Send anyway";
        updateChatMessage(chatKey, { status: "confirmation" });
        setText(els.taskStatus, "Preflight needs confirmation. Review warnings, then send again.");
        return;
      }
      const dispatchPayload = {
        ...payload,
        preflight: preflight.preflight,
        preflight_confirmed: state.preflightConfirmation
      };
      setText(els.taskStatus, "Dispatching /api/agent-runs.");
      const result = await postJson(api.runs, dispatchPayload);
      if (result.board) state.board = result.board;
      if (result.run) {
        state.selectedRunId = result.run.run_id || state.selectedRunId;
        syncChatHistoryFromRuns([result.run]);
        updateChatMessage(chatKey, {
          run_id: result.run.run_id || null,
          agent_name: result.run.agent_name || payload.agent_name,
          status: result.run.status || "queued"
        });
      }
      if (result.event) {
        state.agentEvents = uniqueEvents([result.event].concat(state.agentEvents || []));
        syncChatHistoryFromEvents([result.event], result.run);
      }
      state.preflightConfirmation = false;
      state.preflightResult = null;
      els.taskSubmit.textContent = "Submit task";
      renderPreflight();
      renderBoard();
      renderRuns();
      renderChatHistory();
      renderAgentStreams();
      setText(els.taskStatus, `Agent Run queued: ${result.run && result.run.run_id ? result.run.run_id : "created"}.`);
      setTab("task");
    } catch (err) {
      state.lastError = err && err.message ? err.message : "Task submission failed";
      updateChatMessage(chatKey, { status: "failed" });
      setText(els.taskStatus, state.lastError);
      renderDebug();
    } finally {
      els.taskSubmit.disabled = false;
    }
  }

  function taskPayload() {
    const selectedPaths = uniqueList([...state.selectedPaths, ...splitList(els.taskPaths.value)]);
    const blockedBy = splitList(els.taskBlockers.value);
    const payload = {
      message: els.taskMessage.value.trim(),
      agent_name: els.taskAgent.value.trim() || "Codex",
      run_context: {
        source: "mobile-orchestrator",
        kind: "targeted_run",
        intent: state.orchestratorIntent,
        selected_paths: selectedPaths,
        selected_run_id: state.selectedRunId || null
      }
    };
    const provider = els.taskProvider.value.trim();
    if (provider) payload.provider = provider;
    if (selectedPaths.length) {
      payload.selected_paths = selectedPaths;
      payload.node = { id: `file:${selectedPaths[0]}`, kind: "file", path: selectedPaths[0] };
    }
    if (blockedBy.length) payload.blocked_by_run_ids = blockedBy;
    return payload;
  }

  function resetTaskForm() {
    els.taskForm.reset();
    const agent = data.agent && typeof data.agent === "object" ? data.agent : {};
    els.taskAgent.value = agent.name || "Codex";
    state.preflightConfirmation = false;
    state.preflightResult = null;
    state.orchestratorIntent = "implement";
    setOrchestratorIntent("implement", { fillPrompt: false });
    els.taskSubmit.textContent = "Submit task";
    renderPreflight();
    setText(els.taskStatus, "Preflight runs before dispatch.");
  }

  function renderPreflight() {
    const result = state.preflightResult;
    els.preflightBox.classList.toggle("active", Boolean(result));
    clear(els.preflightBox);
    if (!result) return;
    const gate = normalizePreflight(result);
    const summary = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = gate.requires_confirmation ? "Confirmation required" : "Preflight clear";
    summary.appendChild(strong);
    els.preflightBox.appendChild(summary);
    if (gate.preflight_id) {
      const id = document.createElement("div");
      id.className = "mono";
      id.textContent = gate.preflight_id;
      els.preflightBox.appendChild(id);
    }
    if (gate.warnings.length) {
      gate.warnings.forEach(warning => {
        const line = document.createElement("p");
        line.textContent = warning.message || warning.kind || "Preflight warning";
        els.preflightBox.appendChild(line);
      });
    }
  }

  function normalizePreflight(result) {
    const raw = result && result.preflight && result.preflight.preflight
      ? result.preflight.preflight
      : (result && result.preflight ? result.preflight : {});
    return {
      requires_confirmation: Boolean(raw.requires_confirmation),
      preflight_id: raw.preflight_id || "",
      warnings: Array.isArray(raw.warnings) ? raw.warnings : []
    };
  }

  function renderDebug() {
    clear(els.debugGrid);
    const summary = data.summary && typeof data.summary === "object" ? data.summary : {};
    const rows = [
      ["Live", state.liveStatus],
      ["Board API", api.board],
      ["Search API", api.search],
      ["Preflight API", api.preflight],
      ["Runs API", api.runs],
      ["Run detail", api.runDetail],
      ["Run message", api.runMessages],
      ["Run cancel", api.runCancel],
      ["Accept review", api.runAcceptReview],
      ["Archive", api.runArchive],
      ["Events", api.events],
      ["Generated", data.generated_at || "unknown"],
      ["Payload", `${summary.node_count || 0} nodes, ${summary.edge_count || 0} edges`],
      ["Last error", state.lastError || "none"]
    ];
    rows.forEach(([name, value]) => {
      const row = document.createElement("div");
      row.className = "debug-row";
      const key = document.createElement("span");
      key.textContent = name;
      const val = document.createElement("span");
      val.className = "mono";
      val.textContent = String(value || "");
      row.append(key, val);
      els.debugGrid.appendChild(row);
    });
    clear(els.debugEvents);
    if (!state.eventLog.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No live events yet.";
      els.debugEvents.appendChild(empty);
      return;
    }
    state.eventLog.forEach(event => {
      const card = document.createElement("article");
      card.className = "event-card";
      const title = document.createElement("div");
      title.className = "run-title";
      title.textContent = `${event.type} at ${event.at}`;
      const detail = document.createElement("div");
      detail.className = "run-detail mono";
      detail.textContent = event.detail || "";
      card.append(title, detail);
      els.debugEvents.appendChild(card);
    });
  }

  function badge(text, klass) {
    const span = document.createElement("span");
    span.className = `badge ${klass || ""}`.trim();
    span.textContent = String(text || "");
    return span;
  }

  function statusClass(status) {
    const value = String(status || "").toLowerCase();
    if (["completed", "healthy", "passed"].includes(value)) return "ok";
    if (["failed", "cancelled", "canceled", "orphaned"].includes(value)) return "bad";
    if (["blocked", "review", "needs_review", "needs-review", "stale", "quiet"].includes(value)) return "warn";
    return "";
  }

  function canCancel(run) {
    const status = String(run && run.status ? run.status : "").toLowerCase();
    return !["completed", "failed", "cancelled", "canceled"].includes(status);
  }

  function runTitle(run) {
    const agent = run.agent_name || "Agent";
    const shortId = run.run_id ? String(run.run_id).slice(0, 8) : "new";
    return `${agent} ${shortId}`;
  }

  function splitList(value) {
    return String(value || "")
      .split(/[\n,]+/)
      .map(item => item.trim())
      .filter(Boolean);
  }

  function uniqueList(values) {
    const out = [];
    values.forEach(value => {
      const clean = String(value || "").trim();
      if (clean && !out.includes(clean)) out.push(clean);
    });
    return out;
  }

  function compactText(value, limit) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (!limit || text.length <= limit) return text;
    return `${text.slice(0, Math.max(0, limit - 3))}...`;
  }

  function basename(path) {
    const text = String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
    return text.split("/").filter(Boolean).pop() || text;
  }

  function titleCase(value) {
    const text = String(value || "");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function setText(node, value) {
    if (node) node.textContent = String(value || "");
  }
})();
  </script>
</body>
</html>
"""


def render_mobile_html(payload: dict) -> str:
    """Return a self-contained mobile graph-server HTML document."""

    return MOBILE_HTML_TEMPLATE.replace("__MOBILE_JSON__", _json_for_html(payload))


__all__ = ["render_mobile_html"]
