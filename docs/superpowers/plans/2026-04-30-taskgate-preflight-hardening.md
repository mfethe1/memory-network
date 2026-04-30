# TaskGate Preflight Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make graph-scoped Agent Task preflights tamper-proof across provider, execution strategy, Agent Swarm shape, selected graph context, blockers, and warnings.

**Architecture:** Add a focused `TaskGate` Module that owns the canonical preflight subject and keeps graph-server HTTP as an Adapter. The first Implementation is intentionally narrow: move hash subject construction into `code_index/task_gate.py`, bind `execution_strategy` and public `swarm` config into the hash, and keep the existing storage/consume flow intact.

**Tech Stack:** Python, SQLite, `http.server` graph server, pytest.

---

## File Structure

- Create: `code_index/task_gate.py`
  - Owns canonical preflight hash subjects and normalization helpers for task-gate fields.
- Modify: `code_index/commands/graph_server_http.py:22`
  - Import `task_gate`.
- Modify: `code_index/commands/graph_server_http.py:583`
  - Delegate `_preflight_hash_subject()` to `task_gate.preflight_hash_subject()`.
- Modify: `tests/test_graph_server_cmd.py`
  - Add HTTP tests proving preflight mutation of strategy and swarm shape is rejected.
- Create: `tests/test_task_gate.py`
  - Unit tests for canonical subject stability and tamper sensitivity.

## Task 1: Add Unit Coverage For Strategy And Swarm Binding

**Files:**
- Create: `tests/test_task_gate.py`
- No production code yet.

- [ ] **Step 1: Write the failing unit tests**

```python
from code_index import task_gate


def _request(**overrides):
    request = {
        "agent_name": "Codex",
        "message": "Implement the graph task.",
        "provider": "codex",
        "selected_nodes": ["file:pkg/a.py"],
        "selected_paths": ["pkg/a.py"],
        "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
        "parent_run_id": "",
        "run_context": None,
        "blocked_by_run_ids": [],
        "slice": {},
        "execution_strategy": "single",
        "swarm": {"enabled": False, "execution_strategy": "single"},
    }
    request.update(overrides)
    return request


def _draft():
    return {
        "root": "E:/repo",
        "context_policy": {
            "initial_budget_tokens": 1600,
            "runtime_retrieval": True,
            "retrieval_handles": {
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
            },
        },
    }


def test_preflight_subject_binds_execution_strategy():
    single = task_gate.preflight_hash_subject(request=_request(), draft=_draft())
    swarm = task_gate.preflight_hash_subject(
        request=_request(
            execution_strategy="swarm",
            swarm={
                "enabled": True,
                "execution_strategy": "swarm",
                "provider": "kimi",
                "size": 3,
                "coordination": "parallel",
                "roles": [{"role": "implementer", "claim_mode": "edit"}],
            },
        ),
        draft=_draft(),
    )

    assert single["request"]["execution_strategy"] == "single"
    assert swarm["request"]["execution_strategy"] == "swarm"
    assert single != swarm


def test_preflight_subject_binds_public_swarm_shape():
    first = task_gate.preflight_hash_subject(
        request=_request(
            execution_strategy="swarm",
            swarm={
                "enabled": True,
                "execution_strategy": "swarm",
                "provider": "kimi",
                "size": 2,
                "coordination": "parallel",
                "roles": [{"role": "coordinator"}, {"role": "implementer"}],
            },
        ),
        draft=_draft(),
    )
    second = task_gate.preflight_hash_subject(
        request=_request(
            execution_strategy="swarm",
            swarm={
                "enabled": True,
                "execution_strategy": "swarm",
                "provider": "kimi",
                "size": 3,
                "coordination": "parallel",
                "roles": [
                    {"role": "coordinator"},
                    {"role": "implementer"},
                    {"role": "tester"},
                ],
            },
        ),
        draft=_draft(),
    )

    assert first["request"]["swarm"]["size"] == 2
    assert second["request"]["swarm"]["size"] == 3
    assert first != second
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_task_gate.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'code_index.task_gate'`.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_task_gate.py
git commit -m "test: cover task preflight strategy binding"
```

## Task 2: Add The TaskGate Module

**Files:**
- Create: `code_index/task_gate.py`
- Test: `tests/test_task_gate.py`

- [ ] **Step 1: Add the minimal `TaskGate` Implementation**

```python
"""Task preflight canonicalization for Graph Agent Companion."""

from __future__ import annotations

from typing import Any

from code_index import agent_swarm


def preflight_hash_subject(
    *,
    request: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    """Return the canonical fields that define one dispatchable Agent Task."""

    swarm = agent_swarm.public_swarm_config(request.get("swarm"))
    execution_strategy = str(request.get("execution_strategy") or "single")
    return {
        "request": {
            "agent_name": request.get("agent_name"),
            "message": request.get("message"),
            "provider": request.get("provider") or "",
            "selected_nodes": request.get("selected_nodes") or [],
            "selected_paths": request.get("selected_paths") or [],
            "node": request.get("node") or {},
            "parent_run_id": request.get("parent_run_id") or "",
            "run_context": request.get("run_context"),
            "blocked_by_run_ids": request.get("blocked_by_run_ids") or [],
            "slice": request.get("slice") or {},
            "execution_strategy": execution_strategy,
            "swarm": swarm or {"enabled": False, "execution_strategy": "single"},
        },
        "draft": {
            "root": draft.get("root"),
            "context_policy": draft.get("context_policy") or {},
        },
    }
```

- [ ] **Step 2: Run the unit tests**

Run: `python -m pytest tests/test_task_gate.py -q`

Expected: PASS.

- [ ] **Step 3: Commit the module**

```bash
git add code_index/task_gate.py tests/test_task_gate.py
git commit -m "feat: add task gate preflight subject"
```

## Task 3: Wire Graph Server Preflight Hashing Through TaskGate

**Files:**
- Modify: `code_index/commands/graph_server_http.py:22`
- Modify: `code_index/commands/graph_server_http.py:583`
- Test: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Import `task_gate`**

Change the imports near the top of `code_index/commands/graph_server_http.py`:

```python
from code_index import agent_activity
from code_index import agent_swarm
from code_index import task_gate
```

- [ ] **Step 2: Delegate `_preflight_hash_subject()`**

Replace the body of `_preflight_hash_subject()` with:

```python
def _preflight_hash_subject(
    *,
    request: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    return task_gate.preflight_hash_subject(request=request, draft=draft)
```

- [ ] **Step 3: Run focused tests**

Run: `python -m pytest tests/test_task_gate.py tests/test_graph_server_cmd.py::test_graph_server_requires_and_consumes_preflight_for_graph_tasks -q`

Expected: PASS.

- [ ] **Step 4: Commit the graph-server wiring**

```bash
git add code_index/commands/graph_server_http.py code_index/task_gate.py tests/test_task_gate.py
git commit -m "fix: bind task gate preflight hashing"
```

## Task 4: Add HTTP Tamper Tests

**Files:**
- Modify: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Add the strategy tamper test**

Add this test near `test_graph_server_requires_and_consumes_preflight_for_graph_tasks`:

```python
def test_graph_server_rejects_strategy_tamper_after_preflight(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("CODE_INDEX_GRAPH_PREFLIGHT_SECRET", "test-preflight-secret")
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
        payload = {
            "message": "Implement selected file.",
            "selected_paths": ["pkg/a.py"],
            "provider": "codex",
        }
        preflighted = _preflight_task_payload(base_url, payload)
        preflighted["execution_strategy"] = "agent_swarm"
        preflighted["swarm"] = {
            "enabled": True,
            "provider": "kimi",
            "size": 3,
            "roles": [{"role": "coordinator"}, {"role": "implementer"}, {"role": "tester"}],
        }

        status = _request_status(f"{base_url}/api/agent-runs", preflighted)

        assert status == 412
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ] **Step 2: Add the swarm shape tamper test**

```python
def test_graph_server_rejects_swarm_shape_tamper_after_preflight(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("CODE_INDEX_GRAPH_PREFLIGHT_SECRET", "test-preflight-secret")
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
        payload = {
            "message": "Coordinate implementation.",
            "selected_paths": ["pkg/a.py"],
            "provider": "kimi",
            "execution_strategy": "agent_swarm",
            "swarm": {
                "enabled": True,
                "provider": "kimi",
                "size": 2,
                "roles": [{"role": "coordinator"}, {"role": "implementer"}],
            },
        }
        preflighted = _preflight_task_payload(base_url, payload)
        preflighted["swarm"]["size"] = 3
        preflighted["swarm"]["roles"].append({"role": "tester"})

        status = _request_status(f"{base_url}/api/agent-runs", preflighted)

        assert status == 412
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ] **Step 3: Run the HTTP tests**

Run: `python -m pytest tests/test_graph_server_cmd.py -k "preflight and tamper" -q`

Expected: PASS.

- [ ] **Step 4: Commit the HTTP tests**

```bash
git add tests/test_graph_server_cmd.py
git commit -m "test: reject graph task preflight tampering"
```

## Task 5: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused test suite**

Run: `python -m pytest tests/test_task_gate.py tests/test_graph_server_cmd.py -q`

Expected: PASS.

- [ ] **Step 2: Run compile check**

Run: `python -m compileall -q code_index`

Expected: no output and exit code 0.

- [ ] **Step 3: Commit any missed files**

```bash
git status --short
git add code_index/task_gate.py code_index/commands/graph_server_http.py tests/test_task_gate.py tests/test_graph_server_cmd.py
git commit -m "fix: harden graph task preflight gate"
```

## Self-Review

- Spec coverage: binds `execution_strategy`, `swarm`, provider, selected graph context, blockers, and context policy through the preflight hash subject.
- Red-flag scan: clean.
- Type consistency: all snippets use `dict[str, Any]`, `request`, `draft`, and existing graph-server helper names consistently.
