"""HTML shell for the standalone code graph view."""

from __future__ import annotations

import re

from code_index.commands.graph_script import GRAPH_SCRIPT
from code_index.commands.graph_styles import GRAPH_CSS


HTML_BEFORE_CSS = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>code_index graph</title>
  <style>"""

HTML_BETWEEN_CSS_AND_SCRIPT = r"""  </style>
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
        <option value="structure">Folder map</option>
        <option value="communities">Communities</option>
        <option value="roles">Function groups</option>
        <option value="flow">Dependency flow</option>
        <option value="layered">Layered context</option>
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
      <label class="toggle"><input id="live-refresh" type="checkbox"> live activity</label>
      <button id="refresh-graph" type="button">Refresh</button>
      <button id="reset-view" type="button">Reset</button>
    </div>
  </header>
  <main class="workspace">
    <aside class="navigator">
      <div class="navigator-head">
        <h2>Navigator</h2>
        <p id="navigator-summary">Tree and recent activity</p>
        <div class="nav-actions">
          <button id="nav-parent" type="button">Parent</button>
          <button id="nav-center" type="button">Center</button>
        </div>
      </div>
      <div class="navigator-body">
        <section class="nav-section">
          <h3>Path</h3>
          <div class="breadcrumb-list" id="breadcrumb-view"></div>
        </section>
        <section class="nav-section">
          <h3>Active Work</h3>
          <div class="nav-list" id="active-files"></div>
        </section>
        <section class="nav-section">
          <h3>File Claims</h3>
          <div class="nav-list" id="file-claims"></div>
        </section>
        <section class="nav-section">
          <h3>Agent Runs</h3>
          <div class="nav-list" id="agent-runs"></div>
        </section>
        <section class="nav-section">
          <h3>Search Results</h3>
          <div class="nav-list" id="search-results"></div>
        </section>
        <section class="nav-section">
          <h3>Connected Files</h3>
          <div class="nav-list" id="related-files"></div>
        </section>
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
    <div class="resizer nav-resizer" id="nav-resizer" title="Resize navigator"></div>
    <section class="graph-wrap" id="graph-wrap">
      <svg id="graph" role="img" aria-label="Repository graph">
        <g id="viewport">
          <g id="edges"></g>
          <g id="nodes"></g>
        </g>
      </svg>
      <div class="graph-tools" aria-label="Graph controls">
        <button id="zoom-out" type="button" title="Zoom out">−</button>
        <button id="zoom-in" type="button" title="Zoom in">+</button>
        <button id="fit-view" type="button" title="Fit visible graph">Fit</button>
        <button id="focus-view" type="button" title="Center selected node">Focus</button>
        <button id="collapse-neighborhood" type="button" title="Collapse selected neighborhood">- Hop</button>
        <button id="expand-neighborhood" type="button" title="Expand selected neighborhood">+ Hop</button>
        <span id="neighborhood-status">1 hop</span>
      </div>
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
        <button class="tab" id="tab-chat" type="button">Chat</button>
        <button class="tab" id="tab-edits" type="button">Edits</button>
        <button class="tab" id="tab-notes" type="button">Notes</button>
        <button class="tab" id="tab-code" type="button">Code</button>
        <button class="tab" id="tab-debug" type="button">Debug</button>
      </div>
      <div class="panel-body" id="panel-body"></div>
    </aside>
  </main>
</div>
<script id="graph-data" type="application/json">__GRAPH_JSON__</script>
<script>"""

HTML_AFTER_SCRIPT = r"""</script>
</body>
</html>
"""


def _compact_html_fragment(fragment: str) -> str:
    return "".join(line.strip() for line in fragment.splitlines() if line.strip())


def _compact_css(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,>~])\s*", r"\1", css)
    return css.replace(";}", "}").strip()


def _compact_script(script: str) -> str:
    lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        lines.append(stripped)
    return "\n".join(lines)


HTML_TEMPLATE = (
    _compact_html_fragment(HTML_BEFORE_CSS)
    + _compact_css(GRAPH_CSS)
    + _compact_html_fragment(HTML_BETWEEN_CSS_AND_SCRIPT)
    + _compact_script(GRAPH_SCRIPT)
    + _compact_html_fragment(HTML_AFTER_SCRIPT)
)
