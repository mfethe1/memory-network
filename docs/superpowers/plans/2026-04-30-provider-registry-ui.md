# Provider Registry UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python agent provider registry the single source of truth for graph UI provider options and follow-up run defaults.

**Architecture:** Keep `code_index.agent_providers` as the provider registry Implementation. Add a graph-server payload and endpoint Adapter, then render provider selects from that payload in `graph_client/inspector.py`.

**Tech Stack:** Python, graph server, generated HTML/JS client, pytest.

---

## File Structure

- Modify: `code_index/commands/graph_server_state.py:149`
  - Include provider registry path and initial provider payload in live graph data.
- Modify: `code_index/commands/graph_server_http.py`
  - Add `GET /api/agent-providers`.
- Modify: `code_index/commands/graph_client/inspector.py:241`
  - Replace hard-coded provider select options with rendered registry options.
- Modify: `tests/test_graph_server_cmd.py`
  - Add endpoint test.
- Modify: `tests/test_graph_cmd.py`
  - Add generated HTML smoke assertions.

## Task 1: Add Provider Endpoint Test

**Files:**
- Modify: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Write failing endpoint test**

Add near other graph-server GET tests:

```python
def test_graph_server_exposes_agent_provider_registry(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=0.1,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = _request_json(f"{base_url}/api/agent-providers", {})

        providers = {provider["id"]: provider for provider in payload["providers"]}
        assert payload["kind"] == "code_index_agent_provider_registry"
        assert providers["codex"]["display_name"] == "Codex"
        assert "stream_json_output" in providers["kimi"]["capabilities"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ] **Step 2: Run the endpoint test to verify failure**

Run: `python -m pytest tests/test_graph_server_cmd.py::test_graph_server_exposes_agent_provider_registry -q`

Expected: FAIL with 404 or unexpected response.

- [ ] **Step 3: Commit failing test**

```bash
git add tests/test_graph_server_cmd.py
git commit -m "test: cover graph provider registry endpoint"
```

## Task 2: Add Provider Registry Payload To Graph Server

**Files:**
- Modify: `code_index/commands/graph_server_state.py:14`
- Modify: `code_index/commands/graph_server_state.py:149`
- Modify: `code_index/commands/graph_server_http.py`
- Test: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Include providers in graph payload**

In `graph_server_state.py`, import `agent_providers`:

```python
from code_index import agent_activity
from code_index import agent_providers
```

Then add these keys to `payload["live"]`:

```python
            "agent_providers_path": "/api/agent-providers",
            "agent_providers": agent_providers.provider_registry_payload(),
```

- [ ] **Step 2: Add GET route in graph server**

In the GET route handler, add:

```python
            if route == "/api/agent-providers":
                self._send_agent_providers()
                return
```

- [ ] **Step 3: Add handler method**

```python
        def _send_agent_providers(self) -> None:
            from code_index import agent_providers

            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "kind": "code_index_agent_provider_registry",
                        "providers": agent_providers.provider_registry_payload(),
                    }
                ),
            )
```

- [ ] **Step 4: Run endpoint test**

Run: `python -m pytest tests/test_graph_server_cmd.py::test_graph_server_exposes_agent_provider_registry -q`

Expected: PASS.

- [ ] **Step 5: Commit server payload**

```bash
git add code_index/commands/graph_server_state.py code_index/commands/graph_server_http.py tests/test_graph_server_cmd.py
git commit -m "feat: expose agent provider registry"
```

## Task 3: Render Provider Selects From Registry

**Files:**
- Modify: `code_index/commands/graph_client/inspector.py:241`
- Modify: `code_index/commands/graph_client/inspector.py:1044`
- Test: `tests/test_graph_cmd.py`

- [ ] **Step 1: Add registry helpers to inspector JS**

Add near `agentNameForProvider()`:

```javascript
function agentProviderRegistry() {
  const live = (graph && graph.live) || {};
  const providers = Array.isArray(live.agent_providers) ? live.agent_providers : [];
  if (providers.length) return providers;
  return [
    { id: "codex", display_name: "Codex" },
    { id: "claude", display_name: "Claude" },
    { id: "kimi", display_name: "Kimi" },
  ];
}

function providerOptionHtml(selected) {
  return agentProviderRegistry().map(provider => {
    const id = String(provider.id || "").toLowerCase();
    const name = provider.display_name || id || "Agent";
    return `<option value="${escapeHtml(id)}"${id === selected ? " selected" : ""}>${escapeHtml(name)}</option>`;
  }).join("");
}
```

- [ ] **Step 2: Replace hard-coded run-followup provider options**

Replace:

```html
            <select id="run-followup-provider">
              <option value="codex">Codex CLI</option>
              <option value="claude">Claude Code</option>
              <option value="kimi">Kimi Code CLI</option>
            </select>
```

with:

```javascript
            <select id="run-followup-provider">
              ${providerOptionHtml("codex")}
            </select>
```

- [ ] **Step 3: Replace hard-coded chat provider options**

Replace:

```html
            <select id="agent-provider">
              <option value="codex">Codex CLI</option>
              <option value="claude">Claude Code</option>
              <option value="kimi">Kimi Code CLI</option>
            </select>
```

with:

```javascript
            <select id="agent-provider">
              ${providerOptionHtml("codex")}
            </select>
```

- [ ] **Step 4: Update `agentNameForProvider()` to use registry display names**

Replace the function body with:

```javascript
function agentNameForProvider(provider) {
  const normalized = String(provider || "").toLowerCase();
  const match = agentProviderRegistry().find(item => String(item.id || "").toLowerCase() === normalized);
  return match ? String(match.display_name || match.id || "Agent") : "Agent";
}
```

- [ ] **Step 5: Add generated HTML smoke test**

In `tests/test_graph_cmd.py`, add assertions to the existing standalone HTML test:

```python
    assert "agent_providers" in html
    assert "providerOptionHtml" in html
    assert "Kimi Code CLI" not in html
```

- [ ] **Step 6: Run graph HTML tests**

Run: `python -m pytest tests/test_graph_cmd.py::test_graph_html_writes_standalone_view -q`

Expected: PASS.

- [ ] **Step 7: Commit UI rendering**

```bash
git add code_index/commands/graph_client/inspector.py tests/test_graph_cmd.py
git commit -m "feat: render graph providers from registry"
```

## Task 4: Preserve Swarm Provider Default Behavior

**Files:**
- Modify: `code_index/commands/graph_client/inspector.py:1100`
- Test: `tests/test_graph_cmd.py`

- [ ] **Step 1: Keep Kimi default for Agent Swarm**

Verify this function still exists and keeps behavior:

```javascript
function syncProviderForExecutionStrategy(providerSelect, strategySelect) {
  if (!providerSelect || !strategySelect) return;
  if (strategySelect.value === "agent_swarm" && providerSelect.value === "codex") {
    providerSelect.value = "kimi";
  }
}
```

- [ ] **Step 2: Add registry-safe fallback**

Replace it with:

```javascript
function syncProviderForExecutionStrategy(providerSelect, strategySelect) {
  if (!providerSelect || !strategySelect) return;
  if (strategySelect.value !== "agent_swarm") return;
  const providers = agentProviderRegistry().map(provider => String(provider.id || "").toLowerCase());
  if (providerSelect.value === "codex" && providers.includes("kimi")) {
    providerSelect.value = "kimi";
  }
}
```

- [ ] **Step 3: Run graph command tests**

Run: `python -m pytest tests/test_graph_cmd.py -q`

Expected: PASS.

- [ ] **Step 4: Commit swarm provider fallback**

```bash
git add code_index/commands/graph_client/inspector.py tests/test_graph_cmd.py
git commit -m "fix: keep registry-driven swarm provider defaults"
```

## Task 5: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run provider and graph tests**

Run: `python -m pytest tests/test_agent_adapter_cmd.py tests/test_graph_cmd.py tests/test_graph_server_cmd.py -q`

Expected: PASS.

- [ ] **Step 2: Compile**

Run: `python -m compileall -q code_index`

Expected: no output and exit code 0.

## Self-Review

- Spec coverage: provider registry is served to the graph UI, selects render from registry, and Kimi swarm defaults remain supported.
- Red-flag scan: clean.
- Type consistency: provider payload fields use `id`, `display_name`, `command_preset`, and `capabilities` consistently.
