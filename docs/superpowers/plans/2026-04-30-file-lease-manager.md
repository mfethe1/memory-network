# File Lease Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote file claims into durable leases with renewal, lifecycle events, and token/fence enforcement for graph-supervised writes.

**Architecture:** Add a `LeaseManager` Module while preserving the existing `agent_activity.claim_file()` Interface as a compatibility Adapter. Lease tokens are returned only to the caller that creates or renews a lease; snapshots and prompts expose claim IDs and fence tokens, not secrets.

**Tech Stack:** Python, SQLite, pytest, existing writer lock and claim verification.

---

## File Structure

- Modify: `code_index/schema.sql`
  - Extend `agent_file_claims`.
  - Add `agent_file_claim_events`.
- Create: `code_index/lease_manager.py`
  - Owns lease token creation, hashing, event recording, renewal, release, and expiry.
- Modify: `code_index/agent_activity.py:635`
  - Delegate claim creation, renewal, release, and active claim projection to `lease_manager` where possible.
- Modify: `code_index/commands/graph_server_http.py`
  - Expose renew/release endpoints after module tests pass.
- Test: `tests/test_lease_manager.py`
- Test: `tests/test_agent_activity.py`

## Task 1: Add Lease Schema Tests

**Files:**
- Create: `tests/test_lease_manager.py`

- [ ] **Step 1: Write failing tests for lifecycle events and hidden tokens**

```python
from pathlib import Path

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import lease_manager


def _config(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    return cfg_mod.Config(root=tmp_path, db_path=tmp_path / ".code_index" / "index.db")


def test_create_lease_returns_token_but_active_claims_do_not(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")

        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
            metadata={"source": "test"},
        )
        active = agent_activity.active_file_claims(conn, file_path="pkg/a.py")

        assert lease["lease_token"]
        assert lease["claim"]["mode"] == "edit"
        assert "lease_token" not in active[0]
        assert "lease_token_hash" not in active[0]
    finally:
        db_mod.close(conn)


def test_lease_lifecycle_events_are_recorded(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
        )

        lease_manager.renew_lease(
            conn,
            claim_id=lease["claim"]["claim_id"],
            lease_token=lease["lease_token"],
            fence_token=lease["claim"]["fence_token"],
            ttl_seconds=600,
        )
        lease_manager.release_lease(
            conn,
            claim_id=lease["claim"]["claim_id"],
            lease_token=lease["lease_token"],
            status="released",
        )
        events = lease_manager.claim_events(conn, claim_id=lease["claim"]["claim_id"])

        assert [event["event_type"] for event in events] == ["created", "renewed", "released"]
    finally:
        db_mod.close(conn)
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `python -m pytest tests/test_lease_manager.py -q`

Expected: FAIL because `code_index.lease_manager` and new columns do not exist.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_lease_manager.py
git commit -m "test: cover file lease lifecycle"
```

## Task 2: Add Lease Schema

**Files:**
- Modify: `code_index/schema.sql`
- Test: `tests/test_lease_manager.py`

- [ ] **Step 1: Extend `agent_file_claims`**

Add these columns to the table definition after `fence_token`:

```sql
    lease_token_hash TEXT,
    lease_kind       TEXT NOT NULL DEFAULT 'claim',
    owner_agent      TEXT,
    heartbeat_interval_ms INTEGER,
    conflict_policy  TEXT,
    last_conflict_json TEXT,
```

- [ ] **Step 2: Add lifecycle event table**

Add this after the claim indexes:

```sql
CREATE TABLE IF NOT EXISTS agent_file_claim_events (
    claim_event_pk INTEGER PRIMARY KEY,
    claim_pk       INTEGER NOT NULL REFERENCES agent_file_claims(claim_pk) ON DELETE CASCADE,
    event_type     TEXT NOT NULL, -- created | renewed | released | expired | denied
    timestamp      TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    mode           TEXT NOT NULL,
    fence_token    INTEGER,
    message        TEXT,
    metadata_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_file_claim_events_claim
    ON agent_file_claim_events(claim_pk, timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_file_claim_events_file
    ON agent_file_claim_events(file_path, timestamp);
```

- [ ] **Step 3: Run schema-backed failing tests**

Run: `python -m pytest tests/test_lease_manager.py -q`

Expected: still FAIL because `lease_manager` is not implemented.

- [ ] **Step 4: Commit schema**

```bash
git add code_index/schema.sql tests/test_lease_manager.py
git commit -m "feat: add file lease schema"
```

## Task 3: Implement LeaseManager Core

**Files:**
- Create: `code_index/lease_manager.py`
- Modify: `code_index/agent_activity.py:635`
- Test: `tests/test_lease_manager.py`

- [ ] **Step 1: Add `lease_manager.py`**

```python
"""Durable file leases for graph-supervised Agent Runs."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_after(ttl_seconds: float | None) -> str | None:
    if ttl_seconds is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=float(ttl_seconds))).isoformat(
        timespec="milliseconds"
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _claim_row(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, r.run_id, r.agent_name
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE c.claim_id = ?
         LIMIT 1
        """,
        (claim_id,),
    ).fetchone()


def _public_claim(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "claim_pk": row["claim_pk"],
        "claim_id": row["claim_id"],
        "run_id": row["run_id"],
        "agent_name": row["agent_name"],
        "file_path": row["file_path"],
        "mode": row["mode"],
        "status": row["status"],
        "reason": row["reason"],
        "fence_token": row["fence_token"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def record_claim_event(
    conn: sqlite3.Connection,
    *,
    claim_pk: int,
    event_type: str,
    file_path: str,
    mode: str,
    fence_token: int | None,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_file_claim_events(
            claim_pk, event_type, timestamp, file_path, mode, fence_token, message, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_pk,
            event_type,
            _now_iso(),
            file_path,
            mode,
            fence_token,
            message,
            _json_dumps(metadata or {}),
        ),
    )


def create_lease(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    mode: str,
    reason: str,
    ttl_seconds: float | None = 1800,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from code_index import agent_activity

    token = _new_token()
    claim = agent_activity.claim_file(
        conn,
        run_id=run_id,
        file_path=file_path,
        mode=mode,
        reason=reason,
        ttl_seconds=ttl_seconds,
        metadata=metadata,
    )
    conn.execute(
        """
        UPDATE agent_file_claims
           SET lease_token_hash = ?,
               lease_kind = ?,
               owner_agent = ?,
               heartbeat_interval_ms = ?
         WHERE claim_id = ?
        """,
        (_hash_token(token), "lease", claim.get("agent_name"), 30000, claim["claim_id"]),
    )
    row = _claim_row(conn, claim["claim_id"])
    assert row is not None
    record_claim_event(
        conn,
        claim_pk=int(row["claim_pk"]),
        event_type="created",
        file_path=str(row["file_path"]),
        mode=str(row["mode"]),
        fence_token=int(row["fence_token"]),
        message=f"Lease created for {row['file_path']}.",
        metadata=metadata,
    )
    return {"claim": _public_claim(row), "lease_token": token}


def renew_lease(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    lease_token: str,
    fence_token: int,
    ttl_seconds: float | None,
) -> dict[str, Any]:
    row = _claim_row(conn, claim_id)
    if row is None:
        raise ValueError(f"unknown claim_id: {claim_id}")
    if str(row["lease_token_hash"] or "") != _hash_token(lease_token):
        raise ValueError("lease token does not match claim")
    if int(row["fence_token"] or 0) != int(fence_token):
        raise ValueError("lease fence token is stale")
    now = _now_iso()
    conn.execute(
        """
        UPDATE agent_file_claims
           SET heartbeat_at = ?,
               updated_at = ?,
               expires_at = ?
         WHERE claim_id = ?
           AND status = 'active'
        """,
        (now, now, _iso_after(ttl_seconds), claim_id),
    )
    row = _claim_row(conn, claim_id)
    assert row is not None
    record_claim_event(
        conn,
        claim_pk=int(row["claim_pk"]),
        event_type="renewed",
        file_path=str(row["file_path"]),
        mode=str(row["mode"]),
        fence_token=int(row["fence_token"]),
        message=f"Lease renewed for {row['file_path']}.",
    )
    return _public_claim(row)


def release_lease(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    lease_token: str,
    status: str = "released",
) -> dict[str, Any]:
    row = _claim_row(conn, claim_id)
    if row is None:
        raise ValueError(f"unknown claim_id: {claim_id}")
    if str(row["lease_token_hash"] or "") != _hash_token(lease_token):
        raise ValueError("lease token does not match claim")
    next_status = "expired" if status == "expired" else "released"
    now = _now_iso()
    conn.execute(
        """
        UPDATE agent_file_claims
           SET status = ?,
               updated_at = ?,
               released_at = ?
         WHERE claim_id = ?
        """,
        (next_status, now, now, claim_id),
    )
    row = _claim_row(conn, claim_id)
    assert row is not None
    record_claim_event(
        conn,
        claim_pk=int(row["claim_pk"]),
        event_type=next_status,
        file_path=str(row["file_path"]),
        mode=str(row["mode"]),
        fence_token=int(row["fence_token"]),
        message=f"Lease {next_status} for {row['file_path']}.",
    )
    return _public_claim(row)


def claim_events(conn: sqlite3.Connection, *, claim_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.*
          FROM agent_file_claim_events e
          JOIN agent_file_claims c ON c.claim_pk = e.claim_pk
         WHERE c.claim_id = ?
         ORDER BY e.claim_event_pk
        """,
        (claim_id,),
    ).fetchall()
    return [
        {
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "file_path": row["file_path"],
            "mode": row["mode"],
            "fence_token": row["fence_token"],
            "message": row["message"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }
        for row in rows
    ]
```

- [ ] **Step 2: Run lease tests**

Run: `python -m pytest tests/test_lease_manager.py -q`

Expected: PASS.

- [ ] **Step 3: Commit LeaseManager core**

```bash
git add code_index/lease_manager.py tests/test_lease_manager.py
git commit -m "feat: add file lease manager"
```

## Task 4: Record Events For Compatibility Claim APIs

**Files:**
- Modify: `code_index/agent_activity.py:635`
- Modify: `code_index/agent_activity.py:959`
- Test: `tests/test_agent_activity.py`

- [ ] **Step 1: Add compatibility event test**

```python
def test_claim_file_records_claim_lifecycle_event(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")

        claim = agent_activity.claim_file(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="compatibility claim",
        )

        rows = conn.execute(
            """
            SELECT e.event_type
              FROM agent_file_claim_events e
              JOIN agent_file_claims c ON c.claim_pk = e.claim_pk
             WHERE c.claim_id = ?
             ORDER BY e.claim_event_pk
            """,
            (claim["claim_id"],),
        ).fetchall()
        assert [row["event_type"] for row in rows] == ["created"]
    finally:
        db_mod.close(conn)
```

- [ ] **Step 2: Record created events at the end of `claim_file()`**

After `rows = _claim_rows(...)` and before `return _row_to_claim(rows[0])`, add:

```python
    from code_index import lease_manager

    claim = _row_to_claim(rows[0])
    lease_manager.record_claim_event(
        conn,
        claim_pk=int(rows[0]["claim_pk"]),
        event_type="created",
        file_path=claim["file_path"],
        mode=claim["mode"],
        fence_token=claim.get("fence_token"),
        message=f"Claim created for {claim['file_path']}.",
        metadata=metadata,
    )
    return claim
```

Remove the old direct `return _row_to_claim(rows[0])`.

- [ ] **Step 3: Record release events in `release_claims()`**

After `updated_rows = _claim_rows(...)`, add:

```python
    from code_index import lease_manager

    claims = [_row_to_claim(row) for row in updated_rows]
    for row, claim in zip(updated_rows, claims):
        lease_manager.record_claim_event(
            conn,
            claim_pk=int(row["claim_pk"]),
            event_type=next_status,
            file_path=claim["file_path"],
            mode=claim["mode"],
            fence_token=claim.get("fence_token"),
            message=f"Claim {next_status} for {claim['file_path']}.",
        )
    return claims
```

Remove the old direct `return [_row_to_claim(row) for row in updated_rows]`.

- [ ] **Step 4: Run activity tests**

Run: `python -m pytest tests/test_agent_activity.py tests/test_lease_manager.py -q`

Expected: PASS.

- [ ] **Step 5: Commit compatibility event recording**

```bash
git add code_index/agent_activity.py tests/test_agent_activity.py
git commit -m "feat: record claim lifecycle events"
```

## Task 5: Expose Renew And Release Endpoints

**Files:**
- Modify: `code_index/commands/graph_server_http.py`
- Test: `tests/test_graph_server_cmd.py`

- [ ] **Step 1: Add route handlers**

In the POST route dispatch area, add:

```python
            if route.startswith("/api/file-claims/") and route.endswith("/renew"):
                parts = route.strip("/").split("/")
                if len(parts) >= 3:
                    self._renew_file_claim(parts[2], payload)
                    return
            if route.startswith("/api/file-claims/") and route.endswith("/release"):
                parts = route.strip("/").split("/")
                if len(parts) >= 3:
                    self._release_file_claim(parts[2], payload)
                    return
```

- [ ] **Step 2: Add handler methods**

```python
        def _renew_file_claim(self, claim_id: str, payload: dict[str, Any]) -> None:
            from code_index import lease_manager

            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    claim = lease_manager.renew_lease(
                        conn,
                        claim_id=claim_id,
                        lease_token=str(payload.get("lease_token") or ""),
                        fence_token=int(payload.get("fence_token") or 0),
                        ttl_seconds=float(payload.get("ttl_seconds") or 1800),
                    )
                finally:
                    db_mod.close(conn)
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "claim": claim}))

        def _release_file_claim(self, claim_id: str, payload: dict[str, Any]) -> None:
            from code_index import lease_manager

            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    claim = lease_manager.release_lease(
                        conn,
                        claim_id=claim_id,
                        lease_token=str(payload.get("lease_token") or ""),
                        status=str(payload.get("status") or "released"),
                    )
                finally:
                    db_mod.close(conn)
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "claim": claim}))
```

- [ ] **Step 3: Add HTTP endpoint test**

```python
def test_graph_server_renews_and_releases_file_claim(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="endpoint test",
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(no_code=False, max_code_bytes=200_000, focus=[], agent_name="Codex", event_interval=0.1, quiet=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        renewed = _request_json(
            f"{base_url}/api/file-claims/{lease['claim']['claim_id']}/renew",
            {"lease_token": lease["lease_token"], "fence_token": lease["claim"]["fence_token"]},
        )
        released = _request_json(
            f"{base_url}/api/file-claims/{lease['claim']['claim_id']}/release",
            {"lease_token": lease["lease_token"]},
        )

        assert renewed["ok"] is True
        assert released["claim"]["status"] == "released"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ] **Step 4: Run graph endpoint test**

Run: `python -m pytest tests/test_graph_server_cmd.py -k "file_claim" -q`

Expected: PASS.

- [ ] **Step 5: Commit endpoints**

```bash
git add code_index/commands/graph_server_http.py tests/test_graph_server_cmd.py
git commit -m "feat: expose file lease renewal endpoints"
```

## Task 6: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run lease and activity tests**

Run: `python -m pytest tests/test_lease_manager.py tests/test_agent_activity.py tests/test_graph_server_cmd.py -q`

Expected: PASS.

- [ ] **Step 2: Compile**

Run: `python -m compileall -q code_index`

Expected: no output and exit code 0.

## Self-Review

- Spec coverage: leases have tokens, renewals, releases, lifecycle events, fence tokens, and sanitized active-claim projection.
- Red-flag scan: clean.
- Type consistency: `claim_id`, `lease_token`, `fence_token`, and `event_type` names are consistent across module, tests, and HTTP payloads.
