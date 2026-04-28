"""CSS for the standalone code graph HTML view."""

from __future__ import annotations


GRAPH_CSS = r"""
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
      grid-template-columns: minmax(220px, 280px) 7px minmax(0, 1fr) 7px minmax(320px, 430px);
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
    .nav-actions {
      margin-top: 9px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .nav-actions button {
      height: 28px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: inherit;
      font-size: 11px;
      cursor: pointer;
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
      padding: 4px 6px 4px calc(6px + var(--nav-indent, 0px));
      font: inherit;
      font-size: 12px;
      text-align: left;
      cursor: pointer;
    }
    .nav-row.tree-row {
      grid-template-columns: 18px minmax(0, 1fr) auto;
    }
    .nav-icon {
      width: 18px;
      color: inherit;
      text-align: center;
    }
    .nav-row:hover,
    .nav-row.active {
      background: #1f2833;
      color: var(--ink);
    }
    .nav-row.recent {
      color: #ffd166;
    }
    .nav-row.active-work {
      color: var(--focus);
    }
    .nav-row.connected {
      color: var(--medium);
    }
    .nav-row.missing {
      opacity: 0.58;
      cursor: default;
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
    .run-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto auto;
      align-items: center;
      gap: 4px;
      border-radius: 5px;
    }
    .run-row.active {
      background: #1f2833;
    }
    .run-select {
      min-width: 0;
      min-height: 27px;
      border: 0;
      border-radius: 5px;
      background: transparent;
      color: var(--muted);
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr);
      align-items: center;
      gap: 5px;
      padding: 4px 6px;
      font: inherit;
      font-size: 12px;
      text-align: left;
      cursor: pointer;
    }
    .run-select:hover {
      color: var(--ink);
    }
    .run-select .nav-badge {
      grid-column: 2;
    }
    .run-detail,
    .run-cancel,
    .run-archive {
      height: 24px;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--field);
      color: var(--muted);
      font: inherit;
      font-size: 10px;
      padding: 0 6px;
      cursor: pointer;
    }
    .run-detail:hover:not(:disabled),
    .run-cancel:hover:not(:disabled),
    .run-archive:hover:not(:disabled) {
      color: var(--ink);
      border-color: var(--line-strong);
    }
    .run-detail:disabled,
    .run-cancel:disabled,
    .run-archive:disabled,
    .run-select:disabled {
      opacity: 0.55;
      cursor: default;
    }
    .breadcrumb-list {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .crumb {
      max-width: 100%;
      min-height: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--field);
      color: var(--muted);
      padding: 3px 8px;
      font: inherit;
      font-size: 11px;
      cursor: pointer;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .crumb:hover,
    .crumb.active {
      color: var(--ink);
      border-color: var(--line-strong);
      background: #1f2833;
    }
    .graph-wrap {
      position: relative;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      user-select: none;
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
    .nav-resizer {
      border-left: 0;
    }
    .resizer:hover,
    body.resizing .resizer {
      background: var(--focus);
    }
    svg {
      width: 100%;
      height: 100%;
      display: block;
      cursor: grab;
      touch-action: none;
    }
    body.panning svg {
      cursor: grabbing;
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
    .community-label {
      pointer-events: none;
    }
    .community-label text {
      fill: var(--muted);
      font-size: 11px;
      font-weight: 700;
      paint-order: stroke;
      stroke: rgba(13,17,23,0.92);
      stroke-width: 3px;
      stroke-linejoin: round;
    }
    .node {
      cursor: pointer;
    }
    body.panning .node {
      cursor: grabbing;
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
    .graph-tools {
      position: absolute;
      right: 14px;
      top: 14px;
      display: flex;
      gap: 6px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(22,27,34,0.9);
      backdrop-filter: blur(10px);
    }
    .graph-tools button {
      min-width: 34px;
      height: 30px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: inherit;
      font-size: 12px;
      cursor: pointer;
    }
    .graph-tools button:hover {
      border-color: var(--line-strong);
      background: #1f2833;
    }
    .graph-tools span {
      min-width: 46px;
      height: 30px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
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
    .panel-body.terminal-view {
      overflow: hidden;
      padding: 0;
      display: grid;
      min-height: 0;
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
    textarea.task-box {
      min-height: 104px;
    }
    textarea.chat-box {
      min-height: 126px;
    }
    .agent-chat {
      display: grid;
      gap: 10px;
    }
    .chat-controls {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }
    .chat-controls label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .chat-controls select {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: inherit;
      font-size: 12px;
      padding: 0 10px;
    }
    .primary-action {
      border-color: rgba(88,166,255,0.75);
      background: rgba(88,166,255,0.16);
    }
    .inline-status {
      align-self: center;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
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
    .compact-event {
      padding: 8px 9px;
    }
    .stream-list {
      display: grid;
      gap: 8px;
    }
    .terminal-panel {
      border: 1px solid #202934;
      border-radius: 8px;
      overflow: hidden;
      background: #05080d;
    }
    .terminal-shell {
      min-width: 0;
      min-height: 0;
      height: 100%;
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr) auto;
      background: #05080d;
    }
    .terminal-bar {
      min-height: 30px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 7px 10px;
      border-bottom: 1px solid #202934;
      background: #0a0f16;
      color: var(--muted);
      font: 11px/1.3 "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      overflow-wrap: anywhere;
    }
    .terminal-context {
      max-height: 160px;
      overflow: auto;
      border-bottom: 1px solid #202934;
      background: #070b10;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .terminal-context summary {
      min-height: 30px;
      padding: 7px 10px;
      cursor: pointer;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }
    .terminal-context-grid {
      display: grid;
      gap: 12px;
      padding: 0 10px 10px;
    }
    .terminal-context-grid strong {
      display: block;
      margin-bottom: 5px;
      color: var(--ink);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .terminal-body {
      max-height: min(52vh, 560px);
      overflow-y: auto;
      padding: 10px;
      scroll-behavior: auto;
      display: grid;
      align-content: start;
    }
    .terminal-shell .terminal-body {
      max-height: none;
      min-height: 0;
      height: 100%;
      align-content: end;
    }
    .terminal-composer {
      min-width: 0;
      display: grid;
      gap: 8px;
      padding: 10px;
      border-top: 1px solid #202934;
      background: #0a0f16;
    }
    .terminal-composer .actions {
      margin-top: 0;
      align-items: center;
    }
    .terminal-target {
      grid-template-columns: minmax(0, 1fr);
    }
    textarea.terminal-input {
      min-height: 58px;
      max-height: 140px;
      resize: vertical;
      border-radius: 6px;
      font: 12px/1.45 "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      background: #05080d;
    }
    .stream-line {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: #080d13;
      font-size: 12px;
      line-height: 1.45;
    }
    .stream-line pre {
      margin-top: 6px;
      border-color: #202934;
      background: #05080d;
      white-space: pre-wrap;
    }
    .terminal-body .stream-line {
      border: 0;
      border-radius: 0;
      padding: 4px 0 6px;
      background: transparent;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }
    .terminal-body .stream-line + .stream-line {
      border-top: 1px solid rgba(48,54,61,0.55);
    }
    .terminal-body .stream-line pre {
      margin-top: 3px;
      padding: 0;
      border: 0;
      background: transparent;
      color: #dbeafe;
      white-space: pre-wrap;
    }
    .terminal-body .stream-line.stream-stderr pre,
    .terminal-body .stream-line.stream-stderr .stream-message {
      color: #ffb4b4;
    }
    .terminal-cursor {
      width: 8px;
      height: 15px;
      margin-top: 3px;
      background: #58a6ff;
      animation: terminal-cursor-blink 1s steps(2, start) infinite;
    }
    @keyframes terminal-cursor-blink {
      0%, 45% { opacity: 1; }
      46%, 100% { opacity: 0; }
    }
    .stream-meta {
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    .stream-message {
      overflow-wrap: anywhere;
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
      .graph-tools {
        right: 10px;
        top: 10px;
      }
    }
"""
