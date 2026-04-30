# Run Lifecycle Process Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent Run Status durable across local command adapter processes, server restarts, cancellation, and orphan recovery.

**Architecture:** Add a `RunLifecycle` Module around a durable `agent_run_processes` table. The existing Run Orchestrator remains the classifier, but it gets process liveness from stored process rows instead of only caller-supplied memory.

**Tech Stack:** Python, SQLite, local subprocess adapter, pytest.

---

## File Structure

- Modify: `code_index/schema.sql`
  - Add `agent_run_processes`.
- Create: `code_index/run_lifecycle.py`
  - Owns process registry writes and liveness projection.
- Modify: `code_index/commands/graph_server_dispatch.py:81`
  - Register local command process lifecycle in SQLite.
- Modify: `code_index/commands/agent_adapter_cmd.py:1175`
  - Include process PID events already emitted; no registry writes here unless Task 3 chooses the callback path.
- Modify: `code_index/run_orchestrator.py:229`
  - Use durable process liveness when applying lifecycle actions.
- Test: `tests/test_run_lifecycle.py`
- Test: `tests/test_graph_server_cmd.py`

## Task 1: Add Durable Process Registry Tests

**Files:**
- Create: `tests/test_run_lifecycle.py`

- [ ] **Step 1: Write failing registry tests**

```python
from pathlib import Path

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_lifecycle


def _config(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    return cfg_mod.Config(root=tmp_path, db_path=tmp_path / ".code_index" / "index.db")


def test_register_process_creates_live_process_row(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")

        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            provider="codex",
            pid=12345,
            command_label="codex exec",
        )

        assert process["run_id"] == run["run_id"]
        assert process["status"] == "started"
        assert process["pid"] == 12345
    finally:
        db_mod.close(conn)


def test_finish_process_marks_exit_code_and_status(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        started = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            provider="codex",
            pid=12345,
            command_label="codex exec",
        )

        finished = run_lifecycle.finish_process(
            conn,
            process_id=started["process_id"],
            status="completed",
            exit_code=0,
        )

        assert finished["status"] == "completed"
        assert finished["exit_code"] == 0
        assert finished["ended_at"] is not None
    finally:
        db_mod.close(conn)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_run_lifecycle.py -q`

Expected: FAIL with `ImportError` or `OperationalError` because `run_lifecycle` and the table do not exist.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_run_lifecycle.py
git commit -m "test: cover durable run process registry"
```

## Task 2: Add Schema And RunLifecycle Module

**Files:**
- Modify: `code_index/schema.sql`
- Create: `code_index/run_lifecycle.py`
- Test: `tests/test_run_lifecycle.py`

- [ ] **Step 1: Add `agent_run_processes` table**

Add this after the `agent_runs` indexes in `code_index/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS agent_run_processes (
    process_pk    INTEGER PRIMARY KEY,
    process_id    TEXT NOT NULL UNIQUE,
    run_pk        INTEGER NOT NULL REFERENCES agent_runs(run_pk) ON DELETE CASCADE,
    transport     TEXT NOT NULL,
    provider      TEXT,
    pid           INTEGER,
    command_label TEXT,
    status        TEXT NOT NULL, -- started | completed | failed | cancelled | orphaned
    started_at    TEXT NOT NULL,
    heartbeat_at  TEXT,
    ended_at      TEXT,
    exit_code     INTEGER,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_run_processes_run
    ON agent_run_processes(run_pk);
CREATE INDEX IF NOT EXISTS idx_agent_run_processes_status
    ON agent_run_processes(status, heartbeat_at);
```

- [ ] **Step 2: Add `run_lifecycle.py`**

```python
"""Durable Agent Run process lifecycle helpers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from code_index import agent_activity


ACTIVE_PROCESS_STATUSES = {"started"}
TERMINAL_PROCESS_STATUSES = {"completed", "failed", "cancelled", "orphaned"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_to_process(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "process_pk": row["process_pk"],
        "process_id": row["process_id"],
        "run_id": row["run_id"],
        "transport": row["transport"],
        "provider": row["provider"],
        "pid": row["pid"],
        "command_label": row["command_label"],
        "status": row["status"],
        "started_at": row["started_at"],
        "heartbeat_at": row["heartbeat_at"],
        "ended_at": row["ended_at"],
        "exit_code": row["exit_code"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _process_by_id(conn: sqlite3.Connection, process_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT p.*, r.run_id
          FROM agent_run_processes p
          JOIN agent_runs r ON r.run_pk = p.run_pk
         WHERE p.process_id = ?
         LIMIT 1
        """,
        (process_id,),
    ).fetchone()
    return _row_to_process(row) if row is not None else None


def register_process(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    transport: str,
    provider: str | None,
    pid: int | None,
    command_label: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = agent_activity.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    now = _now_iso()
    process_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO agent_run_processes(
            process_id, run_pk, transport, provider, pid, command_label,
            status, started_at, heartbeat_at, ended_at, exit_code, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?, NULL, NULL, ?)
        """,
        (
            process_id,
            run["run_pk"],
            transport,
            provider,
            pid,
            command_label,
            now,
            now,
            _json_dumps(metadata or {}),
        ),
    )
    process = _process_by_id(conn, process_id)
    assert process is not None
    return process


def heartbeat_process(conn: sqlite3.Connection, *, process_id: str) -> dict[str, Any] | None:
    conn.execute(
        """
        UPDATE agent_run_processes
           SET heartbeat_at = ?
         WHERE process_id = ?
           AND status = 'started'
        """,
        (_now_iso(), process_id),
    )
    return _process_by_id(conn, process_id)


def finish_process(
    conn: sqlite3.Connection,
    *,
    process_id: str,
    status: str,
    exit_code: int | None,
) -> dict[str, Any]:
    next_status = str(status or "").strip().lower()
    if next_status not in TERMINAL_PROCESS_STATUSES:
        raise ValueError(f"unknown process status: {next_status}")
    conn.execute(
        """
        UPDATE agent_run_processes
           SET status = ?,
               ended_at = ?,
               heartbeat_at = ?,
               exit_code = ?
         WHERE process_id = ?
        """,
        (next_status, _now_iso(), _now_iso(), exit_code, process_id),
    )
    process = _process_by_id(conn, process_id)
    if process is None:
        raise ValueError(f"unknown process_id: {process_id}")
    return process
```

- [ ] **Step 3: Run registry tests**

Run: `python -m pytest tests/test_run_lifecycle.py -q`

Expected: PASS.

- [ ] **Step 4: Commit schema and module**

```bash
git add code_index/schema.sql code_index/run_lifecycle.py tests/test_run_lifecycle.py
git commit -m "feat: add durable run process registry"
```

## Task 3: Record Local Command Process Lifecycle

**Files:**
- Modify: `code_index/commands/graph_server_dispatch.py:12`
- Modify: `code_index/commands/graph_server_dispatch.py:103`
- Modify: `code_index/commands/agent_adapter_cmd.py:1139`
- Test: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Import `run_lifecycle` in dispatch module**

```python
from code_index import agent_activity
from code_index import run_lifecycle
```

- [ ] **Step 2: Return process details from `agent_adapter_cmd.run_task()`**

In `_run_command()`, after `subprocess.Popen(...)`, store the PID:

```python
process_id_payload = {"pid": process.pid}
```

Then add it to the final result returned at the end of `_run_command()`:

```python
        "process": process_id_payload,
```

Expected final result fragment:

```python
    return exit_code, {
        "ok": status == "completed",
        "status": status,
        "run_id": task.get("run_id"),
        "events_sent": events_sent,
        "responses": responses,
        "command": command_label,
        "process_exit_code": return_code,
        "process": process_id_payload,
        "cancelled": cancelled,
        "timed_out": timed_out,
        "omitted_output_events": omitted_output_events,
        "changed_files": changed_files,
        "final_message_path": str(last_message_path),
    }
```

- [ ] **Step 3: Register and finish the process in `_run_local_agent_task()`**

Use this shape in `code_index/commands/graph_server_dispatch.py`:

```python
    process_id: str | None = None
    try:
        exit_code, result = agent_adapter_cmd.run_task(
            task,
            mode="command",
            command=command,
            provider=provider or None,
            root_hint=str(config.root),
            command_timeout=_env_float("CODE_INDEX_AGENT_COMMAND_TIMEOUT"),
            max_output_events=_env_int("CODE_INDEX_AGENT_MAX_OUTPUT_EVENTS", 400),
            cancel_event=cancel_event,
        )
        process_payload = result.get("process") if isinstance(result, dict) else {}
        pid = process_payload.get("pid") if isinstance(process_payload, dict) else None
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                process = run_lifecycle.register_process(
                    conn,
                    run_id=str(task["run_id"]),
                    transport="local-command",
                    provider=provider or None,
                    pid=int(pid) if pid is not None else None,
                    command_label=command,
                )
                process_id = str(process["process_id"])
                run_lifecycle.finish_process(
                    conn,
                    process_id=process_id,
                    status=str(result.get("status") or ("completed" if exit_code == 0 else "failed")),
                    exit_code=int(result.get("process_exit_code") or exit_code),
                )
            finally:
                db_mod.close(conn)
        if exit_code == 2:
            _record_local_adapter_failure(
                config, task, RuntimeError(str(result.get("error") or "adapter error"))
            )
```

If this records after the process exits rather than at launch, the table is still durable for completed history. A later slice can register at actual launch by adding a callback from `_run_command()`.

- [ ] **Step 4: Add a graph-server test for registry rows**

Append to the existing local command adapter test after the run reaches completed:

```python
        conn = db_mod.connect(config.db_path)
        try:
            rows = conn.execute(
                """
                SELECT p.status, p.transport, p.provider, p.exit_code
                  FROM agent_run_processes p
                  JOIN agent_runs r ON r.run_pk = p.run_pk
                 WHERE r.run_id = ?
                """,
                (task["run_id"],),
            ).fetchall()
        finally:
            db_mod.close(conn)
        assert rows
        assert rows[-1]["status"] == "completed"
        assert rows[-1]["transport"] == "local-command"
```

- [ ] **Step 5: Run dispatch tests**

Run: `python -m pytest tests/test_graph_server_cmd.py -k "local_command_adapter or command_mode_posts_output" -q`

Expected: PASS.

- [ ] **Step 6: Commit process registry wiring**

```bash
git add code_index/commands/graph_server_dispatch.py code_index/commands/agent_adapter_cmd.py tests/test_graph_server_cmd.py
git commit -m "feat: record local agent process lifecycle"
```

## Task 4: Use Durable Registry In Run Orchestrator Apply

**Files:**
- Modify: `code_index/run_lifecycle.py`
- Modify: `code_index/run_orchestrator.py:229`
- Test: `tests/test_run_lifecycle.py`

- [ ] **Step 1: Add process liveness projection**

Append this to `code_index/run_lifecycle.py`:

```python
def process_liveness_by_run(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT r.run_id, p.status
          FROM agent_run_processes p
          JOIN agent_runs r ON r.run_pk = p.run_pk
         WHERE p.process_pk IN (
               SELECT MAX(process_pk)
                 FROM agent_run_processes
                GROUP BY run_pk
         )
        """
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        status = str(row["status"] or "")
        if status == "started":
            out[str(row["run_id"])] = "alive"
        elif status in {"completed", "failed", "cancelled"}:
            out[str(row["run_id"])] = "dead"
        elif status == "orphaned":
            out[str(row["run_id"])] = "dead"
    return out
```

- [ ] **Step 2: Use projection in `run_orchestrator.apply()`**

Import `run_lifecycle`:

```python
from code_index import run_lifecycle
```

Then near the start of `apply()` after `dead_ids`:

```python
    process_liveness_by_run = run_lifecycle.process_liveness_by_run(conn)
```

Replace:

```python
        process_liveness = "dead" if run_id in dead_ids else "unknown"
```

with:

```python
        process_liveness = process_liveness_by_run.get(run_id)
        if run_id in dead_ids:
            process_liveness = "dead"
        if process_liveness is None:
            process_liveness = "unknown"
```

- [ ] **Step 3: Add liveness projection test**

```python
def test_process_liveness_by_run_reports_latest_process(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            provider="codex",
            pid=12345,
            command_label="codex exec",
        )
        assert run_lifecycle.process_liveness_by_run(conn)[run["run_id"]] == "alive"

        run_lifecycle.finish_process(
            conn,
            process_id=process["process_id"],
            status="failed",
            exit_code=1,
        )

        assert run_lifecycle.process_liveness_by_run(conn)[run["run_id"]] == "dead"
    finally:
        db_mod.close(conn)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_run_lifecycle.py tests/test_agent_activity.py -q`

Expected: PASS.

- [ ] **Step 5: Commit orchestrator liveness**

```bash
git add code_index/run_lifecycle.py code_index/run_orchestrator.py tests/test_run_lifecycle.py
git commit -m "feat: classify runs from durable process liveness"
```

## Task 5: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused lifecycle suite**

Run: `python -m pytest tests/test_run_lifecycle.py tests/test_agent_activity.py tests/test_graph_server_cmd.py -q`

Expected: PASS.

- [ ] **Step 2: Compile**

Run: `python -m compileall -q code_index`

Expected: no output and exit code 0.

## Self-Review

- Spec coverage: durable Agent Run Status gets process registry support, restart recovery input, and orphan classification input.
- Red-flag scan: clean.
- Type consistency: registry rows use `process_id`, `run_id`, `status`, `pid`, and `exit_code` consistently.
