# Unified Chat/Context Interface Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Unify the desktop and mobile agent chat interfaces around a shared `AgentTaskContext` contract, add `edit_policy`, bring context_packet into preflight, expose `/api/symbols`, and add a `/find` chat command.

**Architecture:** A new server-side `chat_context.py` module normalises `selected_paths`, `selected_nodes`, `selected_symbols`, and `edit_policy` into a canonical shape that both UIs submit; preflight is extended to return a `context_preview` built from the existing `build_context_packet` helper; a new `/api/symbols` route wraps the existing `symbol_search.lookup` function; and the desktop `agentTaskPayload` is upgraded to track a persistent selected-file basket matching the mobile model.

**Tech Stack:** Python 3.11+, SQLite (via `db_router`), vanilla JS (inline in `.py` HTML strings), `pytest`, existing `code_index` modules (`context_cmd`, `symbol_search`, `graph_server_preflight`, `graph_server_routes`, `graph_server_http`)

---

## Context Map

| Concern | Key file | Entry point |
|---|---|---|
| Preflight logic | `code_index/commands/graph_server_preflight.py` | `_build_task_draft`, `_preflight_from_draft` |
| Context packet builder | `code_index/commands/context_cmd.py` | `build_context_packet` (line 31) |
| Dispatch / context packet | `code_index/commands/graph_server_dispatch.py` | `_build_task_context_packet` (line ~700) |
| HTTP router registration | `code_index/commands/graph_server_http.py` | `_build_router` (line 199) |
| Route handlers | `code_index/commands/graph_server_routes.py` | `_make_routes_class` (line 67) |
| Symbol search | `code_index/search/symbol_search.py` | `lookup` (line 8) |
| Desktop chat JS | `code_index/commands/graph_client/activity.py` | `agentTaskPayload` (line 189) |
| Mobile chat JS | `code_index/commands/graph_mobile.py` | `taskPayload` (line 2510), `state.selectedPaths` |
| Existing server tests | `tests/test_graph_server_cmd.py` | — |

---

## Task 1: Shared `ChatTaskContext` normaliser module

**What:** Create `code_index/commands/chat_context.py` — a pure Python module that normalises the incoming payload dict into a canonical `ChatTaskContext` typed dict, resolves `selected_symbols`, and validates `edit_policy`.

**Files:**
- Create: `code_index/commands/chat_context.py`
- Test: `tests/test_chat_context.py`

**Step 1: Write the failing test**

```python
# tests/test_chat_context.py
from code_index.commands.chat_context import normalise_chat_task

def test_normalise_minimal_payload():
    result = normalise_chat_task({
        "message": "review this",
        "selected_paths": ["code_index/commands/graph_server_routes.py"],
        "edit_policy": "review_before_edit",
        "provider": "codex",
    })
    assert result["message"] == "review this"
    assert result["selected_paths"] == ["code_index/commands/graph_server_routes.py"]
    assert result["edit_policy"] == "review_before_edit"
    assert result["selected_symbols"] == []
    assert result["selected_nodes"] == []

def test_normalise_defaults_edit_policy_to_review_before_edit():
    result = normalise_chat_task({"message": "go"})
    assert result["edit_policy"] == "review_before_edit"

def test_normalise_rejects_unknown_edit_policy():
    from code_index.commands.chat_context import InvalidEditPolicy
    try:
        normalise_chat_task({"message": "go", "edit_policy": "nuke_it"})
        assert False, "should raise"
    except InvalidEditPolicy:
        pass

def test_normalise_deduplicates_selected_paths():
    result = normalise_chat_task({
        "message": "x",
        "selected_paths": ["a.py", "a.py", "b.py"],
    })
    assert result["selected_paths"] == ["a.py", "b.py"]

def test_normalise_selected_symbols_shape():
    sym = {
        "symbol_uid": "abc",
        "canonical_name": "mod.func",
        "kind": "function",
        "def_file": "mod.py",
        "def_line": 10,
    }
    result = normalise_chat_task({"message": "x", "selected_symbols": [sym]})
    assert result["selected_symbols"][0]["canonical_name"] == "mod.func"
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_chat_context.py -v
```
Expected: `ModuleNotFoundError` or `ImportError` — module does not exist yet.

**Step 3: Implement `chat_context.py`**

```python
# code_index/commands/chat_context.py
"""Normalise incoming chat/task payloads into a canonical ChatTaskContext."""
from __future__ import annotations
from typing import Any

VALID_EDIT_POLICIES = {"review_before_edit", "apply_after_review", "direct_edit"}


class InvalidEditPolicy(ValueError):
    pass


def normalise_chat_task(payload: dict[str, Any]) -> dict[str, Any]:
    edit_policy = str(payload.get("edit_policy") or "review_before_edit").strip()
    if edit_policy not in VALID_EDIT_POLICIES:
        raise InvalidEditPolicy(
            f"edit_policy must be one of {sorted(VALID_EDIT_POLICIES)}, got {edit_policy!r}"
        )

    raw_paths = payload.get("selected_paths") or []
    seen: set[str] = set()
    selected_paths: list[str] = []
    for p in raw_paths:
        s = str(p).strip()
        if s and s not in seen:
            seen.add(s)
            selected_paths.append(s)

    raw_symbols = payload.get("selected_symbols") or []
    selected_symbols: list[dict[str, Any]] = []
    for sym in raw_symbols:
        if not isinstance(sym, dict):
            continue
        selected_symbols.append({
            "symbol_uid": str(sym.get("symbol_uid") or ""),
            "canonical_name": str(sym.get("canonical_name") or ""),
            "kind": str(sym.get("kind") or ""),
            "def_file": str(sym.get("def_file") or ""),
            "def_line": int(sym.get("def_line") or 0),
        })

    raw_nodes = payload.get("selected_nodes") or []
    selected_nodes = [str(n).strip() for n in raw_nodes if str(n).strip()]

    return {
        "message": str(payload.get("message") or "").strip(),
        "selected_paths": selected_paths,
        "selected_nodes": selected_nodes,
        "selected_symbols": selected_symbols,
        "edit_policy": edit_policy,
        "provider": str(payload.get("provider") or "").strip().lower(),
    }
```

**Step 4: Run tests to confirm pass**

```bash
pytest tests/test_chat_context.py -v
```
Expected: all 5 tests PASS.

**Step 5: Commit**

```bash
git add code_index/commands/chat_context.py tests/test_chat_context.py
git commit -m "feat: add chat_context normaliser with edit_policy validation"
```

---

## Task 2: Wire `chat_context.normalise_chat_task` into preflight

**What:** In `graph_server_preflight.py`, call `normalise_chat_task` inside `_task_request_from_payload` to attach `selected_symbols` and `edit_policy` to the request dict, then thread them through to the draft and the preflight record.

**Files:**
- Modify: `code_index/commands/graph_server_preflight.py`
- Test: `tests/test_graph_server_cmd.py` (add cases)

**Step 1: Write failing tests**

Add to `tests/test_graph_server_cmd.py` (find the fixture/helper setup near line 244 and follow the existing pattern):

```python
def test_preflight_returns_edit_policy_in_draft(tmp_path, capsys, monkeypatch):
    """Preflight draft must echo edit_policy from the request."""
    # Use the same server-fixture pattern already in the file.
    # Submit a preflight POST with edit_policy="direct_edit" and assert
    # the returned draft contains that field.
    from tests.test_graph_server_cmd import _make_server  # adjust to actual helper
    server = _make_server(tmp_path, monkeypatch)
    payload = {
        "message": "do it",
        "selected_paths": [],
        "edit_policy": "direct_edit",
        "provider": "codex",
    }
    resp = server.post_json("/api/agent-task-preflight", payload)
    assert resp["draft"]["edit_policy"] == "direct_edit"


def test_preflight_selected_symbols_threaded_to_draft(tmp_path, capsys, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    sym = {
        "symbol_uid": "uid1",
        "canonical_name": "mod.fn",
        "kind": "function",
        "def_file": "mod.py",
        "def_line": 5,
    }
    payload = {
        "message": "review",
        "selected_symbols": [sym],
        "selected_paths": ["mod.py"],
    }
    resp = server.post_json("/api/agent-task-preflight", payload)
    symbols = resp["draft"].get("selected_symbols", [])
    assert any(s["canonical_name"] == "mod.fn" for s in symbols)
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_graph_server_cmd.py -k "edit_policy or selected_symbols_threaded" -v
```

**Step 3: Implement**

In `graph_server_preflight.py`, import and call `normalise_chat_task`:

```python
# Add near top imports:
from code_index.commands.chat_context import normalise_chat_task, InvalidEditPolicy

# In _task_request_from_payload, after building the base return dict (~line 105),
# call normalise and merge back:
def _task_request_from_payload(payload, args):
    # ... existing code ...
    try:
        chat = normalise_chat_task(payload)
    except InvalidEditPolicy:
        chat = normalise_chat_task({**payload, "edit_policy": "review_before_edit"})
    request = { ...existing fields... }
    request["selected_symbols"] = chat["selected_symbols"]
    request["edit_policy"] = chat["edit_policy"]
    return request
```

In `_build_task_draft`, thread the two new fields through to the draft:

```python
task["selected_symbols"] = request.get("selected_symbols", [])
task["edit_policy"] = request.get("edit_policy", "review_before_edit")
```

**Step 4: Run**

```bash
pytest tests/test_graph_server_cmd.py -k "edit_policy or selected_symbols_threaded" -v
```
Expected: PASS.

**Step 5: Run full server test suite**

```bash
pytest tests/test_graph_server_cmd.py -v -q 2>&1 | tail -20
```
Expected: no regressions.

**Step 6: Commit**

```bash
git add code_index/commands/graph_server_preflight.py tests/test_graph_server_cmd.py
git commit -m "feat: thread edit_policy and selected_symbols through preflight"
```

---

## Task 3: Add `context_preview` to preflight response

**What:** Extend `_build_preflight_record` (or `_build_task_draft`) to call `build_context_packet` and attach a compact `context_preview` to the draft. The preview should include: `selected_file` (first path), `language`, `parse_status`, `symbols` (up to 10 from the packet), `chunks` (up to 3 snippets), `related_files` (from graph context), and `affected_tests`.

**Files:**
- Modify: `code_index/commands/graph_server_preflight.py`
- Test: `tests/test_graph_server_cmd.py` (add case)

**Step 1: Write failing test**

```python
def test_preflight_returns_context_preview_for_selected_path(tmp_path, capsys, monkeypatch):
    """Preflight must return context_preview with at least selected_file when a path is given."""
    server = _make_server(tmp_path, monkeypatch)
    # Write a real file so the index can see it
    (tmp_path / "code_index").mkdir(exist_ok=True)
    (tmp_path / "code_index" / "demo.py").write_text("def hello(): pass\n")
    server.reindex()  # call whatever helper reindexes in these tests
    payload = {
        "message": "review",
        "selected_paths": ["code_index/demo.py"],
        "edit_policy": "review_before_edit",
    }
    resp = server.post_json("/api/agent-task-preflight", payload)
    preview = resp["draft"].get("context_preview", {})
    assert preview.get("selected_file") == "code_index/demo.py"
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_graph_server_cmd.py -k "context_preview" -v
```

**Step 3: Implement**

In `graph_server_preflight.py`, add a helper:

```python
def _build_context_preview(
    config: cfg_mod.Config,
    *,
    selected_paths: list[str],
    selected_nodes: list[str],
    graph_context: dict[str, Any],
    task: str = "",
) -> dict[str, Any]:
    if not selected_paths:
        return {}
    primary = selected_paths[0]
    try:
        from code_index.commands.context_cmd import build_context_packet
        packet = build_context_packet(
            config,
            task,
            selected_paths=selected_paths,
            selected_nodes=selected_nodes,
            budget_tokens=600,
            limit=5,
        )
    except Exception:
        packet = {}
    
    # Extract language and parse_status from packet files list
    file_info = {}
    for f in packet.get("files") or []:
        if isinstance(f, dict) and f.get("file_path") == primary:
            file_info = f
            break

    related = [
        str(n.get("path"))
        for n in graph_context.get("related_nodes") or []
        if isinstance(n, dict) and n.get("path")
    ][:8]

    return {
        "selected_file": primary,
        "language": file_info.get("language"),
        "parse_status": file_info.get("parse_status"),
        "symbols": (packet.get("symbols") or [])[:10],
        "chunks": (packet.get("chunks") or [])[:3],
        "related_files": related,
        "affected_tests": packet.get("tests") or [],
    }
```

Call `_build_context_preview` inside `_build_task_draft` and attach as `task["context_preview"]`.

**Step 4: Run**

```bash
pytest tests/test_graph_server_cmd.py -k "context_preview" -v
```

**Step 5: Run full suite, no regressions**

```bash
pytest tests/test_graph_server_cmd.py -q 2>&1 | tail -10
```

**Step 6: Commit**

```bash
git add code_index/commands/graph_server_preflight.py tests/test_graph_server_cmd.py
git commit -m "feat: add context_preview to preflight draft"
```

---

## Task 4: Add `/api/symbols` endpoint

**What:** Register a new `GET /api/symbols` route that calls `symbol_search.lookup` and returns a JSON array. Query parameters: `q` (required), `kind` (optional), `limit` (optional, default 20).

**Files:**
- Modify: `code_index/commands/graph_server_routes.py` (add `_route_symbols_get`)
- Modify: `code_index/commands/graph_server_http.py` (register route)
- Test: `tests/test_graph_server_cmd.py` (add case)

**Step 1: Write failing test**

```python
def test_symbols_endpoint_returns_function_hit(tmp_path, capsys, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    # Index a file that has a function named "build_context_packet"
    (tmp_path / "mymod.py").write_text("def build_context_packet(x): return x\n")
    server.reindex()
    resp = server.get("/api/symbols?q=build_context_packet&kind=function&limit=5")
    assert resp["kind"] == "symbol_results"
    hits = resp["results"]
    assert any(h["canonical_name"].endswith("build_context_packet") for h in hits)
    first = hits[0]
    assert "symbol_uid" in first
    assert "def_file" in first
    assert "def_line" in first


def test_symbols_endpoint_requires_q_param(tmp_path, capsys, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    resp = server.get("/api/symbols", expect_status=400)
    assert resp["error"]
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_graph_server_cmd.py -k "symbols_endpoint" -v
```

**Step 3: Implement route handler in `graph_server_routes.py`**

Add inside `RoutesBase` class (after the existing `_route_search` method):

```python
def _route_symbols_get(self, qs: dict[str, list[str]]) -> None:
    q = (qs.get("q") or [""])[0].strip()
    if not q:
        self._send_bytes(
            HTTPStatus.BAD_REQUEST,
            _json_bytes({"error": "q parameter is required"}),
            "application/json",
        )
        return
    kind = (qs.get("kind") or [""])[0].strip() or None
    try:
        limit = int((qs.get("limit") or ["20"])[0])
    except ValueError:
        limit = 20
    limit = max(1, min(100, limit))
    conn = db_mod.connect(config.db_path)
    try:
        from code_index.search import symbol_search
        raw = symbol_search.lookup(conn, q, kind=kind, limit=limit)
    finally:
        db_mod.close(conn)
    results = [
        {
            "kind": "symbol_definition",
            "symbol_uid": r.get("symbol_uid", ""),
            "canonical_name": r.get("canonical_name", ""),
            "display_name": r.get("display_name", ""),
            "symbol_kind": r.get("kind", ""),
            "def_file": r.get("def_file", ""),
            "def_line": r.get("def_line"),
            "signature": r.get("signature_norm", ""),
            "confidence": r.get("confidence"),
        }
        for r in raw
    ]
    self._send_bytes(
        HTTPStatus.OK,
        _json_bytes({"kind": "symbol_results", "query": q, "results": results}),
        "application/json",
    )
```

**Step 4: Register route in `graph_server_http.py`**

In `_build_router` (line ~217), add:

```python
router.get("/api/symbols", cls._route_symbols_get)
```

**Step 5: Run**

```bash
pytest tests/test_graph_server_cmd.py -k "symbols_endpoint" -v
```

**Step 6: Run full suite**

```bash
pytest tests/test_graph_server_cmd.py -q 2>&1 | tail -10
```

**Step 7: Commit**

```bash
git add code_index/commands/graph_server_routes.py code_index/commands/graph_server_http.py tests/test_graph_server_cmd.py
git commit -m "feat: add GET /api/symbols endpoint backed by symbol_search.lookup"
```

---

## Task 5: Desktop — persistent selected-file basket

**What:** In `code_index/commands/graph_client/activity.py`, add a `selectedContext` state object (like mobile's `state.selectedPaths`), wire up add/remove helpers, render removable file chips in the chat panel, and update `agentTaskPayload` to send `selected_paths` from the basket.

**Files:**
- Modify: `code_index/commands/graph_client/activity.py`

Note: This file is a large inline JS string inside Python. All edits are to the JS sections. There are no pytest tests for this layer — validate by reading the rendered HTML in a browser via the graph server.

**Step 1: Add `selectedContext` to state initialisation**

Find the section in `activity.py` that initialises state variables (search for `defaultChatProvider` or similar state setup). Add:

```javascript
// Persistent file basket — mirrors mobile's state.selectedPaths
let selectedContextPaths = [];
```

**Step 2: Add basket helpers**

```javascript
function addToContextBasket(path) {
  const clean = String(path || "").trim();
  if (!clean || selectedContextPaths.includes(clean)) return;
  selectedContextPaths.push(clean);
  renderContextBasket();
}

function removeFromContextBasket(path) {
  selectedContextPaths = selectedContextPaths.filter(p => p !== path);
  renderContextBasket();
}

function renderContextBasket() {
  const container = document.getElementById("context-basket");
  if (!container) return;
  container.innerHTML = "";
  selectedContextPaths.forEach(path => {
    const chip = document.createElement("span");
    chip.className = "context-chip";
    chip.textContent = path.split("/").pop();
    chip.title = path;
    const remove = document.createElement("button");
    remove.className = "context-chip-remove";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove ${path}`);
    remove.onclick = () => removeFromContextBasket(path);
    chip.appendChild(remove);
    container.appendChild(chip);
  });
}
```

**Step 3: Add basket HTML to the chat panel template**

Find where the chat input / send button are rendered in the desktop HTML template. Insert:

```html
<div id="context-basket" class="context-basket" aria-label="Selected files"></div>
```

And the minimal CSS:

```css
.context-basket { display: flex; flex-wrap: wrap; gap: 4px; padding: 4px 0; min-height: 20px; }
.context-chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 12px; background: var(--accent-soft, rgba(45,212,191,0.16)); color: var(--accent, #2dd4bf); font-size: 12px; }
.context-chip-remove { border: none; background: none; cursor: pointer; color: inherit; padding: 0 2px; font-size: 14px; line-height: 1; }
```

**Step 4: Update `agentTaskPayload` to include basket paths**

Find `agentTaskPayload` at line 189 and update `selectedPaths` construction:

```javascript
function agentTaskPayload(node, message, options = {}) {
  // Merge: basket takes precedence, then node-derived paths
  const nodePaths = node.kind === "file"
    ? [node.path]
    : uniquePaths(((node.metrics || {}).active_files || []).concat((node.metrics || {}).recent_files || []));
  const selectedPaths = selectedContextPaths.length > 0
    ? [...selectedContextPaths]
    : nodePaths;
  // ... rest of existing payload construction, replace old selectedPaths var ...
}
```

**Step 5: Wire "Add to context" from graph node click**

Find the node click / context menu handler. Add a call to `addToContextBasket(node.path)` when the user right-clicks or uses a secondary action on a file node.

**Step 6: Manual smoke test**

```bash
python -m code_index graph-server --port 7890
# Open http://localhost:7890 in browser
# Click a file node → verify chip appears in basket
# Click × on chip → verify it removes
# Submit a chat message → open Network tab and confirm selected_paths contains basket paths
```

**Step 7: Commit**

```bash
git add code_index/commands/graph_client/activity.py
git commit -m "feat: add persistent selected-file basket to desktop graph chat"
```

---

## Task 6: Mobile — add symbol results to context basket

**What:** In `code_index/commands/graph_mobile.py`, after a search returns symbol hits, add an "Add to context" button per result that pushes the `def_file` into `state.selectedPaths` and re-renders the paths input.

**Files:**
- Modify: `code_index/commands/graph_mobile.py`

**Step 1: Find the search result rendering in mobile**

Search for `renderSearchResults` or similar in `graph_mobile.py`.

**Step 2: Add "Add to context" button on symbol results**

In the rendering loop for symbol-kind results, add:

```javascript
if (hit.kind === "symbol_definition" && hit.def_file) {
  const addBtn = document.createElement("button");
  addBtn.className = "add-context-btn";
  addBtn.textContent = "+ context";
  addBtn.onclick = () => {
    if (!state.selectedPaths.includes(hit.def_file)) {
      state.selectedPaths.push(hit.def_file);
      els.taskPaths.value = state.selectedPaths.join(", ");
    }
  };
  resultEl.appendChild(addBtn);
}
```

**Step 3: Manual smoke test**

```bash
python -m code_index graph-server --port 7890
# Open http://localhost:7890/m on mobile or browser mobile emulation
# Search for a symbol name
# Verify symbol results have "+ context" button
# Click it → verify path appears in task paths input
```

**Step 4: Commit**

```bash
git add code_index/commands/graph_mobile.py
git commit -m "feat: add context basket button on mobile symbol search results"
```

---

## Task 7: `/find` chat command — symbol resolver

**What:** Add a `/find <name>` command parser to both UIs. When the user types `/find build_context_packet` in the message input, intercept the submit, call `/api/symbols?q=<name>`, and display results inline (not dispatch an agent run). Optionally allow `/find type <Name>` or `/find function <name>`.

**Files:**
- Modify: `code_index/commands/graph_client/activity.py` (desktop)
- Modify: `code_index/commands/graph_mobile.py` (mobile)

**Step 1: Add command parser utility (shared JS helper)**

Add to both files (in the utility functions section):

```javascript
function parseChatCommand(message) {
  const m = message.trim().match(/^\/find\s+(?:(function|type|method|class)\s+)?(.+)$/i);
  if (!m) return null;
  return { command: "find", kind: m[1] ? m[1].toLowerCase() : null, query: m[2].trim() };
}
```

**Step 2: Intercept submit in desktop**

In the chat submit handler (near `agentTaskPayload`), add before dispatch:

```javascript
const cmd = parseChatCommand(messageInput.value);
if (cmd && cmd.command === "find") {
  handleFindCommand(cmd);
  return;
}
```

```javascript
async function handleFindCommand(cmd) {
  const qs = new URLSearchParams({ q: cmd.query, limit: "10" });
  if (cmd.kind) qs.set("kind", cmd.kind);
  const resp = await fetch(`/api/symbols?${qs}`);
  const data = await resp.json();
  renderFindResults(data.results || []);
}

function renderFindResults(results) {
  const panel = document.getElementById("find-results");
  if (!panel) return;
  panel.innerHTML = "";
  if (!results.length) {
    panel.textContent = "No symbols found.";
    return;
  }
  results.forEach(r => {
    const row = document.createElement("div");
    row.className = "find-result-row";
    row.innerHTML = `<code>${r.canonical_name}</code> <span class="find-kind">${r.symbol_kind}</span> <span class="find-file">${r.def_file}:${r.def_line}</span>`;
    const addBtn = document.createElement("button");
    addBtn.textContent = "+ context";
    addBtn.onclick = () => addToContextBasket(r.def_file);
    row.appendChild(addBtn);
    panel.appendChild(row);
  });
}
```

**Step 3: Add `find-results` panel to desktop chat HTML**

After the context basket div, add:

```html
<div id="find-results" class="find-results" hidden></div>
```

Show/hide it based on whether results are present:
```javascript
function renderFindResults(results) {
  const panel = document.getElementById("find-results");
  if (!panel) return;
  panel.hidden = results.length === 0;
  // ... rest as above
}
```

**Step 4: Add same command interception in mobile**

Find the mobile task submit handler (line ~2475) and add equivalent logic using the same `parseChatCommand` helper pattern.

**Step 5: Manual smoke test (both UIs)**

```bash
python -m code_index graph-server --port 7890
# Desktop: type "/find build_context_packet" in chat, press Enter
# → symbol results appear without dispatching an agent run
# → "+ context" button adds def_file to basket
# Mobile: same via /m
```

**Step 6: Commit**

```bash
git add code_index/commands/graph_client/activity.py code_index/commands/graph_mobile.py
git commit -m "feat: add /find chat command for symbol lookup on desktop and mobile"
```

---

## Task 8: Strengthen provider prompt for symbol-first resolution

**What:** In `code_index/commands/agent_adapter_cmd.py`, extend `_build_provider_prompt` so that when `selected_symbols` is non-empty, the prompt instructs the agent to call the `find_symbol` MCP tool first (before broad scanning). When `edit_policy` is `review_before_edit`, add an explicit instruction to read the context and propose edits rather than applying them immediately.

**Files:**
- Modify: `code_index/commands/agent_adapter_cmd.py`
- Test: `tests/test_agent_activity.py` (or a new `tests/test_agent_adapter.py`)

**Step 1: Write failing test**

```python
# tests/test_agent_adapter.py
from pathlib import Path
from code_index.commands.agent_adapter_cmd import _build_provider_prompt

def _make_task(edit_policy="review_before_edit", selected_symbols=None):
    return {
        "message": "review this",
        "selected_paths": ["mod.py"],
        "edit_policy": edit_policy,
        "selected_symbols": selected_symbols or [],
        "graph_context": {},
        "collaboration": {},
        "run_id": "r1",
    }

def test_review_before_edit_policy_adds_instruction(tmp_path):
    task = _make_task(edit_policy="review_before_edit")
    prompt = _build_provider_prompt(task, root=tmp_path, task_json_path=tmp_path / "task.json")
    assert "propose" in prompt.lower() or "suggest" in prompt.lower()

def test_direct_edit_policy_does_not_add_review_instruction(tmp_path):
    task = _make_task(edit_policy="direct_edit")
    prompt = _build_provider_prompt(task, root=tmp_path, task_json_path=tmp_path / "task.json")
    assert "propose edits" not in prompt.lower()

def test_selected_symbols_adds_find_symbol_instruction(tmp_path):
    task = _make_task(selected_symbols=[{
        "symbol_uid": "u1",
        "canonical_name": "mod.fn",
        "kind": "function",
        "def_file": "mod.py",
        "def_line": 5,
    }])
    prompt = _build_provider_prompt(task, root=tmp_path, task_json_path=tmp_path / "task.json")
    assert "find_symbol" in prompt or "mod.fn" in prompt
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_agent_adapter.py -v
```

**Step 3: Implement**

In `agent_adapter_cmd.py`, find `_build_provider_prompt` (line ~234) and add near the beginning of the returned string:

```python
edit_policy = str(task.get("edit_policy") or "review_before_edit")
selected_symbols = task.get("selected_symbols") or []

edit_policy_instruction = ""
if edit_policy == "review_before_edit":
    edit_policy_instruction = (
        "\n\nIMPORTANT: Read the context fully and PROPOSE your intended edits "
        "with a summary before making any file changes."
    )

symbol_instruction = ""
if selected_symbols:
    names = ", ".join(s.get("canonical_name", "") for s in selected_symbols[:5] if s.get("canonical_name"))
    symbol_instruction = (
        f"\n\nThe user has selected these symbols: {names}. "
        "Call the find_symbol MCP tool to locate their definitions before scanning broadly."
    )
```

Append `edit_policy_instruction` and `symbol_instruction` to the returned prompt string.

**Step 4: Run**

```bash
pytest tests/test_agent_adapter.py -v
```

**Step 5: Run related tests**

```bash
pytest tests/test_agent_activity.py -v -q 2>&1 | tail -10
```

**Step 6: Commit**

```bash
git add code_index/commands/agent_adapter_cmd.py tests/test_agent_adapter.py
git commit -m "feat: strengthen provider prompt for edit_policy and selected_symbols"
```

---

## Task 9: Final integration test

**What:** Write one end-to-end test that exercises the full path: payload in → preflight → draft with `context_preview`, `edit_policy`, `selected_symbols` → `/api/symbols` hit.

**Files:**
- Test: `tests/test_unified_chat_integration.py`

**Step 1: Write the test**

```python
# tests/test_unified_chat_integration.py
"""
Integration: send a unified AgentTaskContext through the server's
preflight endpoint and verify the response shape matches the contract.
"""
import json
from pathlib import Path
import pytest


def test_full_agent_task_context_contract(tmp_path, capsys, monkeypatch):
    """
    POST /api/agent-task-preflight with the full AgentTaskContext shape.
    Verify the draft echoes edit_policy, selected_symbols, and context_preview.
    """
    # This test uses the same _make_server pattern from test_graph_server_cmd.py
    # Import helpers from there; adjust the import path as needed.
    from tests.test_graph_server_cmd import _make_server  # or however it's exposed

    # Write a real Python file so the index can produce a real context_preview
    src = tmp_path / "mymod.py"
    src.write_text("def do_the_thing(x: int) -> str:\n    return str(x)\n")
    server = _make_server(tmp_path, monkeypatch)
    server.reindex()

    payload = {
        "message": "Review this function and suggest an edit",
        "selected_paths": ["mymod.py"],
        "selected_nodes": ["file:mymod.py"],
        "selected_symbols": [
            {
                "symbol_uid": "sym_do_the_thing",
                "canonical_name": "mymod.do_the_thing",
                "kind": "function",
                "def_file": "mymod.py",
                "def_line": 1,
            }
        ],
        "edit_policy": "review_before_edit",
        "provider": "codex",
    }

    resp = server.post_json("/api/agent-task-preflight", payload)
    draft = resp["draft"]

    # edit_policy echoed
    assert draft["edit_policy"] == "review_before_edit"

    # selected_symbols echoed
    syms = draft.get("selected_symbols", [])
    assert any(s["canonical_name"] == "mymod.do_the_thing" for s in syms)

    # context_preview present with selected_file
    preview = draft.get("context_preview", {})
    assert preview.get("selected_file") == "mymod.py"
```

**Step 2: Run**

```bash
pytest tests/test_unified_chat_integration.py -v
```

**Step 3: Fix any failures**

If the server fixture helper isn't importable, replicate the setup inline following the pattern in `test_graph_server_cmd.py`.

**Step 4: Commit**

```bash
git add tests/test_unified_chat_integration.py
git commit -m "test: end-to-end integration test for unified AgentTaskContext contract"
```

---

## Task 10: Run full test suite and triage

**Step 1: Run everything**

```bash
pytest tests/ -q 2>&1 | tail -30
```

**Step 2: Fix any regressions**

Address any failing tests introduced by Tasks 2–8. Common culprits:
- Preflight tests that assert the exact shape of `draft` — add `edit_policy` and `selected_symbols` to expected dicts.
- Tests that mock `build_context_packet` — ensure the mock returns a dict with a `files` key.

**Step 3: Final commit if clean**

```bash
pytest tests/ -q && git add -p && git commit -m "fix: triage regressions from unified chat context changes"
```

---

## Implementation Order Summary

| # | Task | Scope | Risk |
|---|------|-------|------|
| 1 | `chat_context.py` normaliser | New file, pure Python | Low |
| 2 | Wire normaliser into preflight | Modify preflight | Medium |
| 3 | `context_preview` in preflight | Modify preflight | Medium |
| 4 | `/api/symbols` endpoint | New route | Low |
| 5 | Desktop file basket | JS-in-Python | Medium |
| 6 | Mobile symbol → context | JS-in-Python | Low |
| 7 | `/find` command | JS-in-Python | Low |
| 8 | Provider prompt strengthening | Modify adapter | Low |
| 9 | Integration test | New test | Low |
| 10 | Full suite triage | Tests | — |
