# Agent Swarm Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent Swarm a durable execution strategy for one Agent Task with a first-class Swarm Lead, child completion policy, failed-child handling, and parent review handoff.

**Architecture:** Keep `agent_runs` as the durable model and deepen `agent_swarm.py` into the swarm lifecycle Module. `graph_server_http.py` should create the parent and child runs, but parent status transitions are reconciled by `agent_swarm.reconcile_swarm_parent()` from Run Orchestrator.

**Tech Stack:** Python, SQLite, graph-server dispatch, pytest.

---

## File Structure

- Modify: `code_index/agent_swarm.py`
  - Add Swarm Lead metadata, implementer edit claim mode, and reconciliation helpers.
- Modify: `code_index/run_orchestrator.py:229`
  - Reconcile active swarm parents after expired claim cleanup.
- Modify: `code_index/agent_activity.py`
  - Add child-run lookup by `metadata.parent_run_id`.
- Modify: `code_index/commands/graph_server_http.py:1494`
  - Keep creation flow but store parent policy metadata.
- Test: `tests/test_agent_swarm.py`
- Test: `tests/test_graph_server_cmd.py`

## Task 1: Add Swarm Role And Lifecycle Unit Tests

**Files:**
- Create: `tests/test_agent_swarm.py`

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

from code_index import agent_activity
from code_index import agent_swarm
from code_index import config as cfg_mod
from code_index import db_router as db_mod


def _config(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    return cfg_mod.Config(root=tmp_path, db_path=tmp_path / ".code_index" / "index.db")


def test_default_implementer_claims_edit():
    config = agent_swarm.normalize_swarm_config(
        {"execution_strategy": "agent_swarm"},
        request_provider="kimi",
    )
    roles = {role["role"]: role for role in config["roles"]}

    assert roles["coordinator"]["claim_mode"] == "read"
    assert roles["implementer"]["claim_mode"] == "edit"
    assert roles["reviewer"]["claim_mode"] == "read"


def test_reconcile_parent_moves_to_review_when_children_complete(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {"role": "lead", "completion_policy": "all_children_terminal"},
            },
            status="working",
        )
        child_1 = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child 1",
            metadata={"parent_run_id": parent["run_id"], "swarm": {"role": "implementer"}},
            status="completed",
        )
        child_2 = agent_activity.start_run(
            conn,
            agent_name="Kimi Reviewer",
            prompt="child 2",
            metadata={"parent_run_id": parent["run_id"], "swarm": {"role": "reviewer"}},
            status="completed",
        )

        result = agent_swarm.reconcile_swarm_parent(conn, parent_run_id=parent["run_id"])

        assert result["status"] == "review"
        assert {item["run_id"] for item in result["children"]} == {child_1["run_id"], child_2["run_id"]}
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "review"
    finally:
        db_mod.close(conn)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_agent_swarm.py -q`

Expected: FAIL because implementer defaults to `review` and `reconcile_swarm_parent()` does not exist.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_agent_swarm.py
git commit -m "test: cover swarm lifecycle reconciliation"
```

## Task 2: Fix Role Defaults And Add Child Lookup

**Files:**
- Modify: `code_index/agent_swarm.py:37`
- Modify: `code_index/agent_activity.py`
- Test: `tests/test_agent_swarm.py`

- [ ] **Step 1: Change implementer default claim mode**

In `code_index/agent_swarm.py`, change the implementer role:

```python
    SwarmRole(
        role="implementer",
        title="Implementer",
        responsibility=(
            "Make the primary code changes for the task. Claim files before "
            "editing and avoid overwriting peer work."
        ),
        claim_mode="edit",
    ),
```

- [ ] **Step 2: Add child-run lookup in `agent_activity.py`**

Add near `recent_runs()`:

```python
def child_runs(
    conn: sqlite3.Connection,
    *,
    parent_run_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
          FROM agent_runs
         WHERE json_extract(metadata_json, '$.parent_run_id') = ?
           AND archived_at IS NULL
         ORDER BY started_at ASC, run_pk ASC
         LIMIT ?
        """,
        (parent_run_id, max(1, int(limit))),
    ).fetchall()
    return [_row_to_run(row) for row in rows]
```

- [ ] **Step 3: Run role tests**

Run: `python -m pytest tests/test_agent_swarm.py::test_default_implementer_claims_edit -q`

Expected: PASS.

- [ ] **Step 4: Commit role and lookup**

```bash
git add code_index/agent_swarm.py code_index/agent_activity.py tests/test_agent_swarm.py
git commit -m "feat: add swarm child lookup and edit role"
```

## Task 3: Add Swarm Parent Reconciliation

**Files:**
- Modify: `code_index/agent_swarm.py`
- Test: `tests/test_agent_swarm.py`

- [ ] **Step 1: Add terminal helpers and reconciliation function**

Append to `code_index/agent_swarm.py`:

```python
SWARM_PARENT_TERMINAL_STATUSES = {"review", "needs_review", "completed", "failed", "cancelled", "canceled"}


def is_swarm_parent(run: dict[str, Any]) -> bool:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    return str(metadata.get("execution_strategy") or "").lower() == "swarm"


def reconcile_swarm_parent(conn, *, parent_run_id: str) -> dict[str, Any]:
    from code_index import agent_activity

    parent = agent_activity.get_run(conn, parent_run_id)
    if parent is None:
        raise ValueError(f"unknown parent run_id: {parent_run_id}")
    children = agent_activity.child_runs(conn, parent_run_id=parent_run_id)
    if not children:
        return {"run_id": parent_run_id, "status": parent["status"], "children": []}

    child_statuses = [str(child.get("status") or "").lower() for child in children]
    any_failed = any(status == "failed" for status in child_statuses)
    all_terminal = all(status in agent_activity.STOPPED_STATUS_VALUES for status in child_statuses)
    parent_status = str(parent.get("status") or "").lower()
    if parent_status in SWARM_PARENT_TERMINAL_STATUSES:
        return {"run_id": parent_run_id, "status": parent_status, "children": children}

    if all_terminal:
        next_status = "review"
        message = (
            "Swarm Lead moved parent run to review after all child runs reached "
            "terminal or review status."
        )
        payload = {
            "status": next_status,
            "swarm": {
                "child_count": len(children),
                "failed_child_count": sum(1 for status in child_statuses if status == "failed"),
                "completion_policy": "all_children_terminal",
            },
        }
        if any_failed:
            payload["swarm"]["requires_attention"] = True
        updated = agent_activity.end_run(conn, run_id=parent_run_id, status=next_status)
        agent_activity.record_event(
            conn,
            run_id=parent_run_id,
            event_type="status",
            message=message,
            payload=payload,
        )
        return {"run_id": parent_run_id, "status": updated["status"], "children": children}

    return {"run_id": parent_run_id, "status": parent["status"], "children": children}
```

- [ ] **Step 2: Run lifecycle test**

Run: `python -m pytest tests/test_agent_swarm.py -q`

Expected: PASS.

- [ ] **Step 3: Commit reconciliation helper**

```bash
git add code_index/agent_swarm.py tests/test_agent_swarm.py
git commit -m "feat: reconcile swarm parent lifecycle"
```

## Task 4: Apply Swarm Reconciliation From Run Orchestrator

**Files:**
- Modify: `code_index/run_orchestrator.py:10`
- Modify: `code_index/run_orchestrator.py:229`
- Test: `tests/test_agent_swarm.py`

- [ ] **Step 1: Import `agent_swarm`**

```python
from code_index import agent_activity
from code_index import agent_swarm
```

- [ ] **Step 2: Reconcile active swarm parents in `apply()`**

After `runs = agent_activity.active_runs(...)`, add:

```python
    swarm_reconciliations: list[dict[str, Any]] = []
    for run in runs:
        if agent_swarm.is_swarm_parent(run):
            swarm_reconciliations.append(
                agent_swarm.reconcile_swarm_parent(
                    conn,
                    parent_run_id=str(run["run_id"]),
                )
            )
```

Before `return result`, add:

```python
    result["swarm_reconciliations"] = swarm_reconciliations
```

- [ ] **Step 3: Add orchestrator reconciliation test**

```python
def test_run_orchestrator_reconciles_swarm_parent(tmp_path: Path):
    from code_index import run_orchestrator

    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={"execution_strategy": "swarm", "swarm": {"role": "lead"}},
            status="working",
        )
        agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child",
            metadata={"parent_run_id": parent["run_id"], "swarm": {"role": "implementer"}},
            status="completed",
        )

        result = run_orchestrator.apply(conn)

        assert result["swarm_reconciliations"][0]["status"] == "review"
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "review"
    finally:
        db_mod.close(conn)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_agent_swarm.py tests/test_agent_activity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit orchestrator integration**

```bash
git add code_index/run_orchestrator.py tests/test_agent_swarm.py
git commit -m "feat: reconcile swarm parents in orchestrator"
```

## Task 5: Add Graph Server Swarm Lifecycle Test

**Files:**
- Modify: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Extend existing swarm test assertions**

In `test_graph_server_starts_agent_swarm_as_child_runs`, add:

```python
        assert result["run"]["metadata"]["execution_strategy"] == "swarm"
        assert result["run"]["status"] == "working"
        assert any(
            child["role"] == "implementer" and child["provider"] == "custom"
            for child in result["task"]["swarm_children"]
        )
```

- [ ] **Step 2: Add child claim mode assertion**

After loading child runs, query claims:

```python
        conn = db_mod.connect(config.db_path)
        try:
            claims = agent_activity.active_file_claims(conn, file_path="pkg/a.py", limit=20)
        finally:
            db_mod.close(conn)
        modes_by_agent = {claim["agent_name"]: claim["mode"] for claim in claims}
        assert any(mode == "edit" for mode in modes_by_agent.values())
```

- [ ] **Step 3: Run swarm graph test**

Run: `python -m pytest tests/test_graph_server_cmd.py::test_graph_server_starts_agent_swarm_as_child_runs -q`

Expected: PASS.

- [ ] **Step 4: Commit graph-server coverage**

```bash
git add tests/test_graph_server_cmd.py
git commit -m "test: cover swarm run lifecycle metadata"
```

## Task 6: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused tests**

Run: `python -m pytest tests/test_agent_swarm.py tests/test_graph_server_cmd.py tests/test_agent_activity.py -q`

Expected: PASS.

- [ ] **Step 2: Compile**

Run: `python -m compileall -q code_index`

Expected: no output and exit code 0.

## Self-Review

- Spec coverage: Swarm Lead behavior, parent/child status, failed child attention, implementer edit leases, and review handoff are covered.
- Red-flag scan: clean.
- Type consistency: `parent_run_id`, `swarm`, `role`, `completion_policy`, and `execution_strategy` names match existing metadata.
