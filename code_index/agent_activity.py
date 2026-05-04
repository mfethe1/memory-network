"""Append-only agent activity records for the code graph.

The code graph already knows what the index saw after a reindex. This module
captures what an agent is doing before or between reindexes: active runs,
file reads/edits, tests, notes, and navigation events. The data is intentionally
small, JSON-friendly, and easy for CLI/MCP/webhook adapters to write.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}
TERMINAL_STATUS_VALUES = tuple(sorted(TERMINAL_STATUSES))
TERMINAL_STATUS_PLACEHOLDERS = ",".join("?" for _ in TERMINAL_STATUS_VALUES)
REVIEW_STATUSES = {"review", "needs_review", "needs-review"}
STOPPED_STATUSES = TERMINAL_STATUSES | REVIEW_STATUSES
STOPPED_STATUS_VALUES = tuple(sorted(STOPPED_STATUSES))
STOPPED_STATUS_PLACEHOLDERS = ",".join("?" for _ in STOPPED_STATUS_VALUES)
WORK_EVENT_TYPES = {"read", "edit", "test", "tool", "navigate", "note"}
SUGGESTION_EVENT_TYPE = "suggestion"
DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS = 4 * 60 * 60
DEFAULT_CLAIM_TTL_SECONDS = 30 * 60
DEFAULT_DERIVED_REL_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
VALID_CLAIM_MODES = {"read", "edit", "review", "test", "exclusive"}
WRITE_CLAIM_MODES = ("edit", "exclusive")
WRITE_CLAIM_PLACEHOLDERS = ",".join("?" for _ in WRITE_CLAIM_MODES)
EVENT_CLAIM_MODES = {
    "read": "read",
    "navigate": "read",
    "edit": "edit",
    "test": "test",
}
EXCLUSIVE_CLAIM_MODES = {"edit", "exclusive"}
CLAIM_ROW_SELECT = """
        SELECT c.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
"""
CLAIM_ROW_ORDER = "c.updated_at DESC, c.claim_pk DESC"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_after(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    value = float(seconds)
    if value <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=value)).isoformat(
        timespec="milliseconds"
    )


def _active_cutoff_iso(max_age_seconds: float | None) -> str | None:
    if max_age_seconds is None:
        return None
    seconds = float(max_age_seconds)
    if seconds <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return cutoff.isoformat(timespec="milliseconds")


def _normal_path(path: str | None) -> str | None:
    if not path:
        return None
    out = path.replace("\\", "/").strip()
    while out.startswith("./"):
        out = out[2:]
    return out or None


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _public_claim_metadata(value: Any) -> Any:
    from code_index import lease_manager

    return lease_manager.redact_public_metadata(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _non_terminal_run_clause(alias: str = "r") -> str:
    return (
        f"LOWER(COALESCE({alias}.status, 'working')) "
        f"NOT IN ({STOPPED_STATUS_PLACEHOLDERS})"
    )


def _claim_check(ok: bool, reason: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": ok, "reason": reason, "message": message}
    out.update(extra)
    return out


def _row_to_run(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    run_pk = int(row["run_pk"])
    columns = set(row.keys())
    return {
        "run_pk": run_pk,
        "run_id": row["run_id"],
        "agent_name": row["agent_name"] or "Agent",
        "status": row["status"] or "working",
        "prompt": row["prompt"] or "",
        "selected_nodes": _json_loads(row["selected_nodes_json"], []),
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "ended_at": row["ended_at"],
        "archived_at": row["archived_at"] if "archived_at" in columns else None,
        "metadata": _json_loads(row["metadata_json"], {}),
        "active_files": _active_files_for_run(conn, run_pk),
        "blocked_by": _run_blockers_for_run(conn, run_pk),
        "blocks": _runs_blocked_by_run(conn, run_pk),
    }


def _is_orphan_graph_event_run(conn: sqlite3.Connection, run: dict[str, Any]) -> bool:
    """Return true for blank runs created by legacy anonymous tool output."""

    if (run.get("prompt") or "").strip():
        return False
    if run.get("selected_nodes") or run.get("active_files"):
        return False
    metadata = run.get("metadata") or {}
    if metadata.get("source") != "graph-server":
        return False
    row = conn.execute(
        """
        SELECT COUNT(*) AS event_count,
               SUM(CASE WHEN event_type NOT IN ('tool', 'status') THEN 1 ELSE 0 END) AS meaningful_count,
               SUM(CASE WHEN file_path IS NOT NULL AND file_path != '' THEN 1 ELSE 0 END) AS file_count
          FROM agent_events
         WHERE run_pk = ?
        """,
        (run["run_pk"],),
    ).fetchone()
    event_count = int(row["event_count"] or 0)
    meaningful_count = int(row["meaningful_count"] or 0)
    file_count = int(row["file_count"] or 0)
    return event_count > 0 and meaningful_count == 0 and file_count == 0


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_pk": int(row["event_pk"]),
        "run_id": row["run_id"],
        "agent_name": row["agent_name"] or "Agent",
        "run_status": row["run_status"] or "working",
        "timestamp": row["timestamp"],
        "event_type": row["event_type"],
        "file_path": row["file_path"],
        "symbol_path": row["symbol_path"],
        "message": row["message"] or "",
        "payload": _json_loads(row["payload_json"], {}),
    }


def _row_to_claim(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "claim_pk": int(row["claim_pk"]),
        "claim_id": row["claim_id"],
        "run_id": row["run_id"],
        "agent_name": row["agent_name"] or "Agent",
        "run_status": row["run_status"] or "working",
        "file_path": row["file_path"],
        "mode": row["mode"],
        "status": row["status"] or "active",
        "reason": row["reason"] or "",
        "fence_token": int(row["fence_token"] or 0) if "fence_token" in row.keys() else 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "metadata": _public_claim_metadata(_json_loads(row["metadata_json"], {})),
    }


def _claim_rows(
    conn: sqlite3.Connection,
    clauses: list[str],
    params: list[Any] | tuple[Any, ...],
    *,
    order_by: str | None = CLAIM_ROW_ORDER,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = CLAIM_ROW_SELECT + f"         WHERE {' AND '.join(clauses)}"
    query_params = list(params)
    if order_by:
        sql += f"\n         ORDER BY {order_by}"
    if limit is not None:
        sql += "\n         LIMIT ?"
        query_params.append(max(0, int(limit)))
    return conn.execute(sql, query_params).fetchall()


def _row_to_blocker_run(row: sqlite3.Row, prefix: str) -> dict[str, Any]:
    return {
        "blocker_pk": int(row["blocker_pk"]),
        "run_id": row[f"{prefix}_run_id"],
        "agent_name": row[f"{prefix}_agent_name"] or "Agent",
        "run_status": row[f"{prefix}_run_status"] or "working",
        "prompt": row[f"{prefix}_prompt"] or "",
        "status": row["status"] or "active",
        "reason": row["reason"] or "",
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "metadata": _json_loads(row["metadata_json"], {}),
    }


def _row_to_blocker(row: sqlite3.Row) -> dict[str, Any]:
    return _row_to_blocker_run(row, "blocker")


def _row_to_blocked_run(row: sqlite3.Row) -> dict[str, Any]:
    return _row_to_blocker_run(row, "blocked")


def _run_blockers_for_run(
    conn: sqlite3.Connection, run_pk: int
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT b.*,
                   blocker.run_id AS blocker_run_id,
                   blocker.agent_name AS blocker_agent_name,
                   blocker.status AS blocker_run_status,
                   blocker.prompt AS blocker_prompt
              FROM agent_run_blockers b
              JOIN agent_runs blocker ON blocker.run_pk = b.blocker_run_pk
             WHERE b.run_pk = ?
             ORDER BY
               CASE b.status WHEN 'active' THEN 0 ELSE 1 END,
               b.created_at ASC,
               b.blocker_pk ASC
            """,
            (run_pk,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row_to_blocker(row) for row in rows]


def _runs_blocked_by_run(
    conn: sqlite3.Connection, blocker_run_pk: int
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT b.*,
                   blocked.run_id AS blocked_run_id,
                   blocked.agent_name AS blocked_agent_name,
                   blocked.status AS blocked_run_status,
                   blocked.prompt AS blocked_prompt
              FROM agent_run_blockers b
              JOIN agent_runs blocked ON blocked.run_pk = b.run_pk
             WHERE b.blocker_run_pk = ?
             ORDER BY
               CASE b.status WHEN 'active' THEN 0 ELSE 1 END,
               b.created_at ASC,
               b.blocker_pk ASC
            """,
            (blocker_run_pk,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row_to_blocked_run(row) for row in rows]


def _run_file_claims(
    conn: sqlite3.Connection, run_pk: int
) -> list[dict[str, Any]]:
    """Return active file claims for a specific run."""
    rows = _claim_rows(
        conn,
        [
            "c.run_pk = ?",
            "c.status = 'active'",
            "(c.expires_at IS NULL OR c.expires_at >= ?)",
        ],
        [run_pk, _now_iso()],
        order_by=None,
        limit=None,
    )
    return [_row_to_claim(row) for row in rows]


def _active_files_for_run(
    conn: sqlite3.Connection, run_pk: int, *, limit: int = 5
) -> list[str]:
    files: list[str] = []
    # Active claims are the strongest signal of current work.
    claim_rows = conn.execute(
        """
        SELECT file_path
          FROM agent_file_claims
         WHERE run_pk = ?
           AND status = 'active'
           AND (expires_at IS NULL OR expires_at >= ?)
         ORDER BY updated_at DESC, claim_pk DESC
         LIMIT 30
        """,
        (run_pk, _now_iso()),
    ).fetchall()
    for row in claim_rows:
        path = row["file_path"]
        if path and path not in files:
            files.append(path)
        if len(files) >= limit:
            return files
    # Fallback to recent events for files without claims.
    rows = conn.execute(
        """
        SELECT file_path
          FROM agent_events
         WHERE run_pk = ?
           AND file_path IS NOT NULL
         ORDER BY timestamp DESC, event_pk DESC
         LIMIT 30
        """,
        (run_pk,),
    ).fetchall()
    for row in rows:
        path = row["file_path"]
        if path and path not in files:
            files.append(path)
        if len(files) >= limit:
            break
    return files


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM agent_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_run(conn, row)


def _runs_from_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    limit: int,
    include_orphan: bool = False,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    runs: list[dict[str, Any]] = []
    for row in rows:
        run = _row_to_run(conn, row)
        if not include_orphan and _is_orphan_graph_event_run(conn, run):
            continue
        runs.append(run)
        if len(runs) >= limit:
            break
    return runs


def latest_active_run(
    conn: sqlite3.Connection,
    *,
    agent_name: str | None = None,
    max_age_seconds: float | None = DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    params: list[Any] = list(STOPPED_STATUS_VALUES)
    agent_filter = ""
    age_filter = ""
    if agent_name:
        agent_filter = "AND agent_name = ?"
        params.append(agent_name)
    cutoff = _active_cutoff_iso(max_age_seconds)
    if cutoff:
        age_filter = "AND updated_at >= ?"
        params.append(cutoff)
    rows = conn.execute(
        f"""
        SELECT *
          FROM agent_runs
         WHERE LOWER(COALESCE(status, 'working')) NOT IN ({STOPPED_STATUS_PLACEHOLDERS})
           AND archived_at IS NULL
           {agent_filter}
           {age_filter}
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT 20
        """,
        params,
    ).fetchall()
    runs = _runs_from_rows(conn, rows, limit=1)
    return runs[0] if runs else None


def start_run(
    conn: sqlite3.Connection,
    *,
    agent_name: str = "Agent",
    prompt: str = "",
    selected_nodes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
    status: str = "working",
) -> dict[str, Any]:
    now = _now_iso()
    rid = run_id or uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO agent_runs(
            run_id, agent_name, status, prompt, selected_nodes_json,
            started_at, updated_at, ended_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            rid,
            agent_name or "Agent",
            status or "working",
            prompt or "",
            _json_dumps(selected_nodes or []),
            now,
            now,
            _json_dumps(metadata or {}),
        ),
    )
    run = get_run(conn, rid)
    assert run is not None
    return run


def _active_blocker_count(conn: sqlite3.Connection, run_pk: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
          FROM agent_run_blockers
         WHERE run_pk = ?
           AND status = 'active'
        """,
        (run_pk,),
    ).fetchone()
    return int(row[0] or 0)


def _sync_blocked_runs(conn: sqlite3.Connection) -> None:
    now = _now_iso()
    conn.execute(
        """
        UPDATE agent_run_blockers
           SET status = 'resolved',
               resolved_at = COALESCE(resolved_at, ?)
         WHERE status = 'active'
           AND blocker_run_pk IN (
               SELECT run_pk
                 FROM agent_runs
                WHERE LOWER(COALESCE(status, '')) = 'completed'
           )
        """,
        (now,),
    )
    conn.execute(
        """
        UPDATE agent_runs
           SET status = 'queued',
               updated_at = ?
         WHERE LOWER(COALESCE(status, '')) = 'blocked'
           AND archived_at IS NULL
           AND NOT EXISTS (
               SELECT 1
                 FROM agent_run_blockers b
                WHERE b.run_pk = agent_runs.run_pk
                  AND b.status = 'active'
           )
        """,
        (now,),
    )


def add_run_blockers(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    blocked_by_run_ids: list[str],
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    if str(run.get("status") or "").lower() in TERMINAL_STATUSES:
        raise ValueError(f"cannot block terminal run_id: {run_id}")
    blocker_ids = _string_list(blocked_by_run_ids)
    if not blocker_ids:
        return []
    if run_id in blocker_ids:
        raise ValueError("a run cannot block itself")

    now = _now_iso()
    rows: list[dict[str, Any]] = []
    for blocker_id in blocker_ids:
        blocker = get_run(conn, blocker_id)
        if blocker is None:
            raise ValueError(f"unknown blocker run_id: {blocker_id}")
        link_status = (
            "resolved"
            if str(blocker.get("status") or "").lower() == "completed"
            else "active"
        )
        resolved_at = now if link_status == "resolved" else None
        conn.execute(
            """
            INSERT INTO agent_run_blockers(
                run_pk, blocker_run_pk, status, reason, created_at,
                resolved_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_pk, blocker_run_pk) DO UPDATE SET
                status = excluded.status,
                reason = excluded.reason,
                resolved_at = excluded.resolved_at,
                metadata_json = excluded.metadata_json
            """,
            (
                run["run_pk"],
                blocker["run_pk"],
                link_status,
                reason or "",
                now,
                resolved_at,
                _json_dumps(metadata or {}),
            ),
        )
    if _active_blocker_count(conn, int(run["run_pk"])):
        conn.execute(
            """
            UPDATE agent_runs
               SET status = 'blocked',
                   updated_at = ?
             WHERE run_pk = ?
               AND LOWER(COALESCE(status, '')) NOT IN ({})
            """.format(STOPPED_STATUS_PLACEHOLDERS),
            (now, run["run_pk"], *STOPPED_STATUS_VALUES),
        )
    else:
        _sync_blocked_runs(conn)

    refreshed = get_run(conn, run_id)
    if refreshed is not None:
        rows = refreshed.get("blocked_by") or []
    return [
        row
        for row in rows
        if row.get("run_id") in blocker_ids
    ]


def active_run_blockers(
    conn: sqlite3.Connection, *, run_id: str
) -> list[dict[str, Any]]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    return [
        blocker
        for blocker in run.get("blocked_by", [])
        if str(blocker.get("status") or "").lower() == "active"
    ]


def blocking_runs(
    conn: sqlite3.Connection, *, run_ids: list[str]
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run_id in _string_list(run_ids):
        run = get_run(conn, run_id)
        if run is None:
            raise ValueError(f"unknown blocker run_id: {run_id}")
        if run_id in seen:
            continue
        seen.add(run_id)
        blockers.append(run)
    return [
        run
        for run in blockers
        if str(run.get("status") or "").lower() != "completed"
    ]


def _kanban_column_for_run(run: dict[str, Any]) -> str:
    status = str(run.get("status") or "working").lower()
    active_blockers = [
        blocker
        for blocker in run.get("blocked_by", [])
        if str(blocker.get("status") or "").lower() == "active"
    ]
    if active_blockers or status == "blocked":
        return "blocked"
    if status in {"queued", "ready", "planned"}:
        return "ready"
    if status in {"review", "needs_review", "needs-review"}:
        return "review"
    if status in TERMINAL_STATUSES:
        return "done"
    return "active"


def kanban_board(conn: sqlite3.Connection, *, limit: int = 25) -> dict[str, Any]:
    active = active_runs(conn, limit=max(0, int(limit)) * 3, max_age_seconds=None)
    recent = recent_runs(conn, limit=max(0, int(limit)) * 3)
    runs = unique_runs(active + recent)
    columns = {
        "blocked": {"title": "Blocked", "runs": []},
        "ready": {"title": "Ready", "runs": []},
        "active": {"title": "Active", "runs": []},
        "review": {"title": "Review", "runs": []},
        "done": {"title": "Done", "runs": []},
    }
    for run in runs:
        column = _kanban_column_for_run(run)
        if len(columns[column]["runs"]) < max(0, int(limit)):
            columns[column]["runs"].append(run)
    return {
        "kind": "code_index_agent_kanban",
        "columns": columns,
        "counts": {name: len(column["runs"]) for name, column in columns.items()},
    }


def unique_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in runs:
        run_id = str(run.get("run_id") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        out.append(run)
    return out


def claim_file(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    mode: str = "edit",
    reason: str | None = None,
    ttl_seconds: float | None = DEFAULT_CLAIM_TTL_SECONDS,
    metadata: dict[str, Any] | None = None,
    _record_lifecycle_event: bool = True,
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    path = _normal_path(file_path)
    if not path:
        raise ValueError("file_path is required")
    from code_index import lease_manager

    lease_manager.expire_stale_leases(conn)
    claim_mode = (mode or "edit").strip().lower()
    if claim_mode not in VALID_CLAIM_MODES:
        raise ValueError(f"unknown claim mode: {claim_mode}")
    _raise_on_claim_conflict(
        conn,
        run_pk=int(run["run_pk"]),
        file_path=path,
        mode=claim_mode,
    )
    now = _now_iso()
    claim_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{path}:{claim_mode}").hex
    fence_token = _next_fence_token(conn, path)
    conn.execute(
        """
        INSERT INTO agent_file_claims(
            claim_id, run_pk, file_path, mode, status, reason, fence_token, created_at,
            updated_at, heartbeat_at, expires_at, released_at, metadata_json
        )
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(run_pk, file_path, mode) DO UPDATE SET
            status = 'active',
            reason = excluded.reason,
            fence_token = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.fence_token
                ELSE excluded.fence_token
            END,
            lease_token_hash = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.lease_token_hash
                ELSE NULL
            END,
            lease_kind = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.lease_kind
                ELSE 'claim'
            END,
            owner_agent = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.owner_agent
                ELSE NULL
            END,
            heartbeat_interval_ms = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.heartbeat_interval_ms
                ELSE NULL
            END,
            conflict_policy = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.conflict_policy
                ELSE NULL
            END,
            last_conflict_json = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.last_conflict_json
                ELSE NULL
            END,
            updated_at = excluded.updated_at,
            heartbeat_at = excluded.heartbeat_at,
            expires_at = CASE
                WHEN agent_file_claims.lease_kind = 'lease'
                 AND agent_file_claims.status = 'active'
                 AND agent_file_claims.lease_token_hash IS NOT NULL
                 AND (
                     agent_file_claims.expires_at IS NULL
                     OR agent_file_claims.expires_at >= excluded.updated_at
                 )
                THEN agent_file_claims.expires_at
                ELSE excluded.expires_at
            END,
            released_at = NULL,
            metadata_json = excluded.metadata_json
        """,
        (
            claim_id,
            run["run_pk"],
            path,
            claim_mode,
            reason or "",
            fence_token,
            now,
            now,
            now,
            _iso_after(ttl_seconds),
            _json_dumps(metadata or {}),
        ),
    )
    rows = _claim_rows(
        conn,
        ["c.run_pk = ?", "c.file_path = ?", "c.mode = ?"],
        [run["run_pk"], path, claim_mode],
        order_by=None,
        limit=1,
    )
    assert rows
    claim = _row_to_claim(rows[0])
    row_keys = set(rows[0].keys())
    preserved_active_lease = (
        "lease_kind" in row_keys
        and "lease_token_hash" in row_keys
        and str(rows[0]["lease_kind"] or "") == "lease"
        and bool(rows[0]["lease_token_hash"])
    )
    if _record_lifecycle_event and not preserved_active_lease:
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


def _next_fence_token(conn: sqlite3.Connection, file_path: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(fence_token), 0) + 1
          FROM agent_file_claims
         WHERE file_path = ?
        """,
        (file_path,),
    ).fetchone()
    return int(row[0] or 1)


def _raise_on_claim_conflict(
    conn: sqlite3.Connection,
    *,
    run_pk: int,
    file_path: str,
    mode: str,
) -> None:
    now = _now_iso()
    rows = conn.execute(
        f"""
        SELECT c.claim_id, c.mode, c.file_path, c.fence_token,
               r.run_id, r.agent_name
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE c.file_path = ?
           AND c.status = 'active'
           AND c.run_pk != ?
           AND (c.expires_at IS NULL OR c.expires_at >= ?)
           AND r.archived_at IS NULL
           AND {_non_terminal_run_clause("r")}
        """,
        (file_path, run_pk, now, *STOPPED_STATUS_VALUES),
    ).fetchall()
    for row in rows:
        other_mode = str(row["mode"] or "")
        conflict = (
            mode == "exclusive"
            or other_mode == "exclusive"
            or (mode in EXCLUSIVE_CLAIM_MODES and other_mode in EXCLUSIVE_CLAIM_MODES)
        )
        if conflict:
            raise ValueError(
                "claim conflict: "
                f"{file_path} is held by {row['agent_name'] or 'Agent'} "
                f"({row['run_id']}, mode={other_mode}, fence={row['fence_token']})"
            )


def verify_claim_fence(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    fence_token: int,
    mode: str | None = None,
) -> bool:
    path = _normal_path(file_path)
    if not path:
        return False
    run = get_run(conn, run_id)
    if run is None:
        return False
    clauses = [
        "run_pk = ?",
        "file_path = ?",
        "status = 'active'",
        "(expires_at IS NULL OR expires_at >= ?)",
        "fence_token = ?",
    ]
    params: list[Any] = [run["run_pk"], path, _now_iso(), int(fence_token)]
    claim_mode = (mode or "").strip().lower()
    if claim_mode:
        clauses.append("mode = ?")
        params.append(claim_mode)
    row = conn.execute(
        f"""
        SELECT 1
          FROM agent_file_claims
         WHERE {" AND ".join(clauses)}
         LIMIT 1
        """,
        params,
    ).fetchone()
    return row is not None


def verify_write_claim(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    fence_token: int,
) -> dict[str, Any]:
    """Verify a supervised write has a current edit/exclusive claim.

    Read/review/test claims are intentionally ignored here: they should be
    visible coordination signals, not write blockers.
    """

    path = _normal_path(file_path)
    if not path:
        return _claim_check(False, "missing_claim", "missing claim: --file is required")
    try:
        fence = int(fence_token)
    except (TypeError, ValueError):
        return _claim_check(
            False,
            "stale_fence",
            f"stale fence: expected numeric fence token for {path}",
            file_path=path,
        )
    run = get_run(conn, run_id)
    if run is None:
        return _claim_check(
            False,
            "missing_claim",
            f"missing claim: unknown agent run_id {run_id}",
            run_id=run_id,
            file_path=path,
        )

    now = _now_iso()
    foreign_rows = _claim_rows(
        conn,
        [
            "c.file_path = ?",
            "c.run_pk != ?",
            "c.status = 'active'",
            f"c.mode IN ({WRITE_CLAIM_PLACEHOLDERS})",
            "(c.expires_at IS NULL OR c.expires_at >= ?)",
            "r.archived_at IS NULL",
            _non_terminal_run_clause("r"),
        ],
        [path, run["run_pk"], *WRITE_CLAIM_MODES, now, *STOPPED_STATUS_VALUES],
    )
    if foreign_rows:
        claim = _row_to_claim(foreign_rows[0])
        return _claim_check(
            False,
            "conflicting_claim",
            (
                "conflicting claim: "
                f"{path} is held by {claim['agent_name']} "
                f"({claim['run_id']}, mode={claim['mode']}, "
                f"fence={claim['fence_token']})"
            ),
            run_id=run_id,
            file_path=path,
            conflicting_claim=claim,
        )

    own_rows = _claim_rows(
        conn,
        [
            "c.file_path = ?",
            "c.run_pk = ?",
            "c.status = 'active'",
            f"c.mode IN ({WRITE_CLAIM_PLACEHOLDERS})",
        ],
        [path, run["run_pk"], *WRITE_CLAIM_MODES],
    )
    if not own_rows:
        return _claim_check(
            False,
            "missing_claim",
            (
                f"missing claim: {run_id} has no active edit/exclusive claim "
                f"for {path}"
            ),
            run_id=run_id,
            file_path=path,
        )

    expired_claims = [
        _row_to_claim(row)
        for row in own_rows
        if row["expires_at"] is not None and row["expires_at"] < now
    ]
    current_claims = [
        _row_to_claim(row)
        for row in own_rows
        if row["expires_at"] is None or row["expires_at"] >= now
    ]
    if not current_claims:
        claim = expired_claims[0]
        return _claim_check(
            False,
            "expired_claim",
            (
                f"expired claim: {path} claim for {run_id} expired at "
                f"{claim['expires_at']} (fence={claim['fence_token']})"
            ),
            run_id=run_id,
            file_path=path,
            claim=claim,
        )

    for claim in current_claims:
        if int(claim.get("fence_token") or 0) == fence:
            return _claim_check(
                True,
                "verified",
                (
                    f"claim verified: {path} is held by {run_id} "
                    f"(mode={claim['mode']}, fence={claim['fence_token']})"
                ),
                run_id=run_id,
                file_path=path,
                claim=claim,
            )

    fences = ", ".join(str(claim["fence_token"]) for claim in current_claims)
    return _claim_check(
        False,
        "stale_fence",
        (
            f"stale fence: {path} for {run_id} has current fence "
            f"{fences}; got {fence}"
        ),
        run_id=run_id,
        file_path=path,
        current_claims=current_claims,
    )


def claim_files(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_paths: list[str],
    mode: str = "edit",
    reason: str | None = None,
    ttl_seconds: float | None = DEFAULT_CLAIM_TTL_SECONDS,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for path in file_paths:
        if _normal_path(path):
            claims.append(
                claim_file(
                    conn,
                    run_id=run_id,
                    file_path=path,
                    mode=mode,
                    reason=reason,
                    ttl_seconds=ttl_seconds,
                    metadata=metadata,
                )
            )
    return claims


def heartbeat_claim(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    mode: str | None = None,
    ttl_seconds: float | None = DEFAULT_CLAIM_TTL_SECONDS,
) -> dict[str, Any]:
    """Refresh heartbeat and extend expiry for an active file claim."""
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    path = _normal_path(file_path)
    if not path:
        raise ValueError("file_path is required")
    now = _now_iso()
    clauses = [
        "run_pk = ?",
        "file_path = ?",
        "status = 'active'",
    ]
    params: list[Any] = [run["run_pk"], path]
    if mode:
        clauses.append("mode = ?")
        params.append(mode.strip().lower())
    conn.execute(
        f"""
        UPDATE agent_file_claims
           SET heartbeat_at = ?,
               updated_at = ?,
               expires_at = COALESCE(?, expires_at)
         WHERE {" AND ".join(clauses)}
        """,
        (now, now, _iso_after(ttl_seconds), *params),
    )
    rows = _claim_rows(
        conn,
        ["c.run_pk = ?", "c.file_path = ?"] + (["c.mode = ?"] if mode else []),
        [run["run_pk"], path] + ([mode.strip().lower()] if mode else []),
        order_by=None,
        limit=1,
    )
    if not rows:
        raise ValueError(f"no active claim for {path} on run {run_id}")
    return _row_to_claim(rows[0])


def release_claims(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str | None = None,
    mode: str | None = None,
    status: str = "released",
) -> list[dict[str, Any]]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    next_status = (status or "released").strip().lower()
    if next_status not in {"released", "expired"}:
        raise ValueError(f"unknown claim release status: {next_status}")
    from code_index import lease_manager

    with lease_manager._atomic(conn):
        lease_manager.expire_stale_leases(conn)
        select_clauses = ["c.run_pk = ?", "c.status = 'active'"]
        params: list[Any] = [run["run_pk"]]
        path = _normal_path(file_path)
        if path:
            select_clauses.append("c.file_path = ?")
            params.append(path)
        if mode:
            select_clauses.append("c.mode = ?")
            params.append(mode.strip().lower())
        select_clauses.append("COALESCE(c.lease_kind, 'claim') != 'lease'")
        rows = _claim_rows(conn, select_clauses, params)
        claim_pks = [int(row["claim_pk"]) for row in rows]
        if not claim_pks:
            return []
        now = _now_iso()
        placeholders = ",".join("?" for _ in claim_pks)
        conn.execute(
            f"""
            UPDATE agent_file_claims
                   SET status = ?,
                       lease_token_hash = NULL,
                       updated_at = ?,
                       released_at = ?
             WHERE claim_pk IN ({placeholders})
            """,
            [next_status, now, now, *claim_pks],
        )
        updated_rows = _claim_rows(conn, [f"c.claim_pk IN ({placeholders})"], claim_pks)
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


def active_file_claims(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    file_path: str | None = None,
) -> list[dict[str, Any]]:
    from code_index import lease_manager

    lease_manager.expire_stale_leases(conn)
    clauses = [
        "c.status = 'active'",
        "(c.expires_at IS NULL OR c.expires_at >= ?)",
        "r.archived_at IS NULL",
        _non_terminal_run_clause("r"),
    ]
    params: list[Any] = [_now_iso(), *STOPPED_STATUS_VALUES]
    path = _normal_path(file_path)
    if path:
        clauses.append("c.file_path = ?")
        params.append(path)
    rows = _claim_rows(
        conn,
        clauses,
        params,
        order_by=(
            "CASE c.mode "
            "WHEN 'edit' THEN 0 "
            "WHEN 'test' THEN 1 "
            "WHEN 'review' THEN 2 "
            "ELSE 3 END, "
            "c.updated_at DESC, c.claim_pk DESC"
        ),
        limit=limit,
    )
    return [_row_to_claim(row) for row in rows]


def record_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_type: str,
    file_path: str | None = None,
    symbol_path: str | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
    timestamp: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    event = (event_type or "").strip().lower()
    if not event:
        raise ValueError("event_type is required")
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    when = timestamp or _now_iso()
    payload_dict = payload or {}
    cursor = conn.execute(
        """
        INSERT INTO agent_events(
            run_pk, timestamp, event_type, file_path, symbol_path, message,
            payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run["run_pk"],
            when,
            event,
            _normal_path(file_path),
            symbol_path,
            message or "",
            _json_dumps(payload_dict),
        ),
    )
    event_pk = int(cursor.lastrowid)
    next_status = payload_dict.get("status") if event == "status" else None
    if not next_status and event in WORK_EVENT_TYPES:
        current = str(run.get("status") or "").lower()
        if current not in STOPPED_STATUSES:
            next_status = "working"
    next_status_text = str(next_status or "").lower()
    ended_at = when if next_status_text in STOPPED_STATUSES else None
    conn.execute(
        """
        UPDATE agent_runs
           SET updated_at = ?,
               status = COALESCE(?, status),
               ended_at = COALESCE(ended_at, ?)
         WHERE run_pk = ?
        """,
        (when, next_status, ended_at, run["run_pk"]),
    )
    row = conn.execute(
        """
        SELECT e.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_events e
          JOIN agent_runs r ON r.run_pk = e.run_pk
         WHERE e.event_pk = ?
        """,
        (event_pk,),
    ).fetchone()
    event_payload = _row_to_event(row)
    normalized_path = _normal_path(file_path)
    claim_mode = EVENT_CLAIM_MODES.get(event)
    if normalized_path and claim_mode:
        claim_file(
            conn,
            run_id=run_id,
            file_path=normalized_path,
            mode=claim_mode,
            reason=message or f"{event} event",
            metadata={"source": "agent_event", "event_type": event},
        )
    if next_status_text in STOPPED_STATUSES:
        release_claims(conn, run_id=run_id)
        if next_status_text == "completed":
            _sync_blocked_runs(conn)
    if root is not None:
        from code_index.agent_collaboration import append_event_jsonl

        append_event_jsonl(root, event_payload)
    return event_payload


def record_decision(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    decision: str,
    rationale: str | None = None,
    status: str | None = None,
    payload: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    text = (decision or "").strip()
    if not text:
        raise ValueError("decision is required")
    payload_dict = dict(payload or {})
    payload_dict["decision"] = text
    if rationale is not None:
        payload_dict["rationale"] = rationale
    if status:
        payload_dict["status"] = status
    return record_event(
        conn,
        run_id=run_id,
        event_type="decision",
        message=text,
        payload=payload_dict,
        timestamp=timestamp,
    )


def run_transcript(
    conn: sqlite3.Connection, run_id: str, *, limit: int = 200
) -> dict[str, Any] | None:
    run = get_run(conn, run_id)
    if run is None:
        return None
    event_limit = max(0, int(limit))
    total_event_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_pk = ?",
            (run["run_pk"],),
        ).fetchone()[0]
        or 0
    )
    decision_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
              FROM agent_events
             WHERE run_pk = ?
               AND event_type = 'decision'
            """,
            (run["run_pk"],),
        ).fetchone()[0]
        or 0
    )
    rows = conn.execute(
        """
        SELECT e.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_events e
          JOIN agent_runs r ON r.run_pk = e.run_pk
         WHERE e.run_pk = ?
         ORDER BY e.timestamp ASC, e.event_pk ASC
         LIMIT ?
        """,
        (run["run_pk"], event_limit),
    ).fetchall()
    events = [_row_to_event(row) for row in rows]
    decisions = [event for event in events if event["event_type"] == "decision"]
    edits = _transcript_edits(events)
    commands = _transcript_commands(events)
    event_types = Counter(event["event_type"] for event in events)
    files_touched: list[str] = []
    for event in events:
        path = event.get("file_path")
        if path and path not in files_touched:
            files_touched.append(path)
    changed_files = _transcript_changed_files(events)
    active_files: list[str] = []
    for path in run.get("active_files", []):
        if path and path not in active_files:
            active_files.append(path)
    metadata = run.get("metadata") or {}
    for path in metadata.get("selected_paths", []):
        if path and path not in active_files:
            active_files.append(path)
    summary = {
        "event_count": total_event_count,
        "included_event_count": len(events),
        "truncated": total_event_count > len(events),
        "decision_count": decision_count,
        "first_event_at": events[0]["timestamp"] if events else None,
        "last_event_at": events[-1]["timestamp"] if events else None,
        "event_types": dict(sorted(event_types.items())),
        "files_touched": files_touched,
        "changed_files": changed_files,
        "edit_count": len(edits),
        "command_count": len(commands),
    }
    return {
        "run": run,
        "events": events,
        "decisions": decisions,
        "edits": edits,
        "commands": commands,
        "changed_files": changed_files,
        "active_files": active_files,
        "suggestions": build_run_suggestions(conn, run_id),
        "summary": summary,
        "summaries": summary,
    }


def _payload_string_list(payload: dict[str, Any], *keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = [str(item) for item in value if item]
        else:
            continue
        for item in values:
            text = _normal_path(item)
            if text and text not in out:
                out.append(text)
    return out


def _event_command(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    raw = None
    for key in ("command", "cmd", "invocation"):
        if payload.get(key):
            raw = payload[key]
            break
    if isinstance(raw, list):
        command = " ".join(str(part) for part in raw if part)
    elif raw is not None:
        command = str(raw)
    elif event.get("event_type") in {"tool", "test"}:
        command = str(event.get("message") or "")
    else:
        command = ""
    command = command.strip()
    return command or None


def _transcript_commands(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        event_type = str(event.get("event_type") or "").lower()
        if event_type not in {"tool", "test", "status"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        command = _event_command(event)
        if not command:
            continue
        key = (event_type, str(event.get("timestamp") or ""), command)
        if key in seen:
            continue
        seen.add(key)
        commands.append(
            {
                "event_pk": event.get("event_pk"),
                "event_type": event_type,
                "timestamp": event.get("timestamp"),
                "file_path": event.get("file_path"),
                "command": command,
                "message": event.get("message") or "",
                "status": payload.get("status") or event.get("run_status"),
                "exit_code": payload.get("exit_code"),
            }
        )
    return commands


def _transcript_edits(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "edit" or not event.get("file_path"):
            continue
        edits.append(
            {
                "event_pk": event.get("event_pk"),
                "timestamp": event.get("timestamp"),
                "file_path": event.get("file_path"),
                "symbol_path": event.get("symbol_path"),
                "message": event.get("message") or "",
                "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            }
        )
    return edits


def _transcript_changed_files(events: list[dict[str, Any]]) -> list[str]:
    changed: list[str] = []

    def add(path: str | None) -> None:
        text = _normal_path(path)
        if text and text not in changed:
            changed.append(text)

    for event in events:
        if event.get("event_type") == "edit":
            add(event.get("file_path"))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        for path in _payload_string_list(payload, "changed_files", "changed_file"):
            add(path)
    return changed


def build_run_suggestions(
    conn: sqlite3.Connection, run_id: str, *, limit: int = 25
) -> dict[str, Any]:
    """Return post-run diagnostics and affected-test suggestions for a run."""

    run = get_run(conn, run_id)
    if run is None:
        return {
            "source": "post_run",
            "run_id": run_id,
            "changed_files": [],
            "diagnostics": [],
            "affected_tests": [],
            "runner": {"runner": "pytest", "invocation": ["pytest"], "node_ids": []},
            "suggestions": [],
        }

    files = _run_files(conn, run)
    diagnostics = _diagnostics_for_files(conn, files, limit=limit)
    affected_tests = _affected_tests_for_files(conn, files, limit=limit)
    try:
        from code_index.runners.pytest import build_pytest_invocation

        runner = build_pytest_invocation(affected_tests)
    except Exception:  # pragma: no cover - defensive; runner is tiny.
        runner = {"runner": "pytest", "invocation": ["pytest"], "node_ids": []}

    suggestions: list[dict[str, Any]] = []
    if diagnostics:
        highest = diagnostics[0].get("severity") or "diagnostic"
        suggestions.append(
            {
                "kind": "diagnostics",
                "severity": highest,
                "message": (
                    f"Review {len(diagnostics)} diagnostic(s) on files touched by this run."
                ),
                "files": sorted({d["file_path"] for d in diagnostics if d.get("file_path")}),
            }
        )
    node_ids = list(runner.get("node_ids") or [])
    if node_ids:
        suggestions.append(
            {
                "kind": "affected_tests",
                "message": f"Run {len(node_ids)} affected pytest node id(s).",
                "command": " ".join(str(part) for part in runner.get("invocation") or []),
                "node_ids": node_ids[:limit],
            }
        )
    elif files:
        suggestions.append(
            {
                "kind": "affected_tests",
                "message": (
                    "No affected-test edges were found for the touched files; run a broader test target or rebuild tests if coverage seems stale."
                ),
                "files": files,
            }
        )

    return {
        "source": "post_run",
        "run_id": run_id,
        "changed_files": files,
        "diagnostics": diagnostics,
        "affected_tests": affected_tests,
        "runner": runner,
        "suggestions": suggestions,
    }


def record_run_suggestions(
    conn: sqlite3.Connection, *, run_id: str, limit: int = 25
) -> dict[str, Any] | None:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    if str(run.get("status") or "").lower() not in {"completed", "failed"}:
        return None
    row = conn.execute(
        """
        SELECT 1
          FROM agent_events
         WHERE run_pk = ?
           AND event_type = ?
           AND payload_json LIKE '%"source":"post_run"%'
         LIMIT 1
        """,
        (run["run_pk"], SUGGESTION_EVENT_TYPE),
    ).fetchone()
    if row is not None:
        return None
    payload = build_run_suggestions(conn, run_id, limit=limit)
    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return None
    return record_event(
        conn,
        run_id=run_id,
        event_type=SUGGESTION_EVENT_TYPE,
        message=suggestions[0].get("message") or "Post-run suggestions available.",
        payload=payload,
    )


def archive_run(
    conn: sqlite3.Connection, *, run_id: str, archived_at: str | None = None
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    release_claims(conn, run_id=run_id)
    when = archived_at or _now_iso()
    conn.execute(
        """
        UPDATE agent_runs
           SET archived_at = ?,
               updated_at = ?
         WHERE run_id = ?
        """,
        (when, when, run_id),
    )
    updated = get_run(conn, run_id)
    assert updated is not None
    return updated


def _run_files(conn: sqlite3.Connection, run: dict[str, Any]) -> list[str]:
    files: list[str] = []

    def add(path: Any) -> None:
        if not path:
            return
        text = _normal_path(str(path))
        if text and text not in files:
            files.append(text)

    for path in run.get("active_files", []):
        add(path)
    metadata = run.get("metadata") or {}
    for path in metadata.get("selected_paths", []):
        add(path)
    rows = conn.execute(
        """
        SELECT file_path
          FROM agent_events
         WHERE run_pk = ?
           AND file_path IS NOT NULL
           AND event_type IN ('edit', 'test', 'read', 'navigate')
         ORDER BY
           CASE event_type
             WHEN 'edit' THEN 0
             WHEN 'test' THEN 1
             WHEN 'read' THEN 2
             ELSE 3
           END,
           timestamp DESC,
           event_pk DESC
        """,
        (run["run_pk"],),
    ).fetchall()
    for row in rows:
        add(row["file_path"])
    return files[:25]


def _diagnostics_for_files(
    conn: sqlite3.Connection, files: list[str], *, limit: int
) -> list[dict[str, Any]]:
    if not files:
        return []
    placeholders = ",".join("?" for _ in files)
    rows = conn.execute(
        f"""
        SELECT f.file_path, d.tool, d.code, d.severity,
               d.start_line, d.end_line, d.message, d.observed_at
          FROM diagnostics d
          JOIN files f ON f.file_pk = d.file_pk
         WHERE f.file_path IN ({placeholders})
           AND f.deleted_at IS NULL
         ORDER BY
           CASE COALESCE(d.severity, '')
             WHEN 'error' THEN 0
             WHEN 'warning' THEN 1
             ELSE 2
           END,
           f.file_path ASC,
           d.start_line ASC
         LIMIT ?
        """,
        (*files, max(0, int(limit))),
    ).fetchall()
    return [
        {
            "file_path": row["file_path"],
            "tool": row["tool"],
            "code": row["code"],
            "severity": row["severity"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "message": row["message"],
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]


def _affected_tests_for_files(
    conn: sqlite3.Connection, files: list[str], *, limit: int
) -> list[dict[str, Any]]:
    if not files:
        return []
    placeholders = ",".join("?" for _ in files)
    rows = conn.execute(
        f"""
        SELECT test.symbol_uid, test.canonical_name, test.kind,
               te.edge_type, te.depth, te.confidence, te.path_json,
               te.provenance,
               target.symbol_uid AS matched_symbol_uid,
               target.canonical_name AS matched_canonical_name,
               target.kind AS matched_kind,
               (SELECT f2.file_path FROM occurrences o2
                  JOIN files f2 ON f2.file_pk = o2.file_pk
                 WHERE o2.symbol_pk = test.symbol_pk
                   AND o2.role = 'definition'
                   AND f2.deleted_at IS NULL
                 ORDER BY o2.start_line ASC LIMIT 1) AS def_file,
               (SELECT o2.start_line FROM occurrences o2
                 WHERE o2.symbol_pk = test.symbol_pk
                   AND o2.role = 'definition'
                 ORDER BY o2.start_line ASC LIMIT 1) AS def_line,
               (SELECT c.context_json FROM chunks c
                 WHERE c.primary_symbol_pk = test.symbol_pk
                   AND c.deleted_at IS NULL
                 ORDER BY c.chunk_pk ASC LIMIT 1) AS context_json
          FROM test_edges te
          JOIN symbols test ON test.symbol_pk = te.test_symbol_pk
          JOIN symbols target ON target.symbol_pk = te.target_symbol_pk
          JOIN occurrences target_def ON target_def.symbol_pk = target.symbol_pk
          JOIN files target_file ON target_file.file_pk = target_def.file_pk
         WHERE target_def.role = 'definition'
           AND target_file.file_path IN ({placeholders})
           AND target_file.deleted_at IS NULL
           AND test.deleted_at IS NULL
           AND target.deleted_at IS NULL
         ORDER BY te.depth ASC, test.canonical_name ASC, target.canonical_name ASC
         LIMIT ?
        """,
        (*files, max(0, int(limit))),
    ).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["symbol_uid"] in seen:
            continue
        seen.add(row["symbol_uid"])
        try:
            path = json.loads(row["path_json"] or "[]")
        except json.JSONDecodeError:
            path = []
        try:
            context = json.loads(row["context_json"] or "{}")
        except json.JSONDecodeError:
            context = {}
        out.append(
            {
                "symbol_uid": row["symbol_uid"],
                "canonical_name": row["canonical_name"],
                "kind": row["kind"],
                "def_file": row["def_file"],
                "def_line": row["def_line"],
                "edge_type": row["edge_type"],
                "depth": row["depth"],
                "confidence": row["confidence"],
                "path": path,
                "rationale": " → ".join(path) if path else row["canonical_name"],
                "parametrize": context.get("parametrize"),
                "matched_target": {
                    "symbol_uid": row["matched_symbol_uid"],
                    "canonical_name": row["matched_canonical_name"],
                    "kind": row["matched_kind"],
                },
            }
        )
    return out


def end_run(
    conn: sqlite3.Connection, *, run_id: str, status: str = "completed"
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    release_claims(conn, run_id=run_id)
    now = _now_iso()
    conn.execute(
        """
        UPDATE agent_runs
           SET status = ?,
               updated_at = ?,
               ended_at = ?
         WHERE run_id = ?
        """,
        (status or "completed", now, now, run_id),
    )
    if str(status or "completed").lower() == "completed":
        _sync_blocked_runs(conn)
    updated = get_run(conn, run_id)
    assert updated is not None
    return updated


def active_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 5,
    max_age_seconds: float | None = DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    params: list[Any] = list(STOPPED_STATUS_VALUES)
    cutoff = _active_cutoff_iso(max_age_seconds)
    age_filter = ""
    if cutoff:
        age_filter = "AND updated_at >= ?"
        params.append(cutoff)
    requested_limit = max(0, int(limit))
    params.append(max(requested_limit, requested_limit * 4))
    rows = conn.execute(
        f"""
        SELECT *
          FROM agent_runs
         WHERE LOWER(COALESCE(status, 'working')) NOT IN ({STOPPED_STATUS_PLACEHOLDERS})
            AND archived_at IS NULL
            {age_filter}
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT ?
        """,
        params,
    ).fetchall()
    return _runs_from_rows(conn, rows, limit=requested_limit)


def recent_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 8,
    include_orphan: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    requested_limit = max(0, int(limit))
    archive_filter = "" if include_archived else "WHERE archived_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT *
          FROM agent_runs
         {archive_filter}
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT ?
        """,
        (max(requested_limit, requested_limit * 4),),
    ).fetchall()
    return _runs_from_rows(
        conn,
        rows,
        limit=requested_limit,
        include_orphan=include_orphan,
    )


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
    return [_row_to_run(conn, row) for row in rows]


def recent_events(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_events e
          JOIN agent_runs r ON r.run_pk = e.run_pk
         ORDER BY e.timestamp DESC, e.event_pk DESC
         LIMIT ?
        """,
        (max(0, int(limit)),),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def file_presence(
    conn: sqlite3.Connection, *, limit: int = 200
) -> dict[str, list[dict[str, Any]]]:
    """Return a mapping of file_path -> active agent presence indicators.

    Useful for live views that need to show exactly which agents are on
    which files without recomputing from raw runs and claims.
    """
    presence: dict[str, list[dict[str, Any]]] = {}
    runs = active_runs(conn, limit=50, max_age_seconds=None)
    for run in runs:
        run_id = run.get("run_id")
        agent_name = run.get("agent_name") or "Agent"
        status = run.get("status") or "working"
        for path in run.get("active_files", []):
            if not path:
                continue
            presence.setdefault(path, [])
            if not any(
                p.get("run_id") == run_id and p.get("presence_type") == "active_file"
                for p in presence[path]
            ):
                presence[path].append(
                    {
                        "run_id": run_id,
                        "agent_name": agent_name,
                        "status": status,
                        "presence_type": "active_file",
                    }
                )
        metadata = run.get("metadata") or {}
        for path in metadata.get("selected_paths", []):
            if not path:
                continue
            presence.setdefault(path, [])
            if not any(
                p.get("run_id") == run_id and p.get("presence_type") == "selected"
                for p in presence[path]
            ):
                presence[path].append(
                    {
                        "run_id": run_id,
                        "agent_name": agent_name,
                        "status": status,
                        "presence_type": "selected",
                    }
                )
    claims = active_file_claims(conn, limit=limit)
    for claim in claims:
        path = claim.get("file_path")
        if not path:
            continue
        presence.setdefault(path, [])
        if not any(
            p.get("claim_id") == claim.get("claim_id")
            for p in presence[path]
        ):
            presence[path].append(
                {
                    "claim_id": claim.get("claim_id"),
                    "run_id": claim.get("run_id"),
                    "agent_name": claim.get("agent_name") or "Agent",
                    "mode": claim.get("mode"),
                    "status": claim.get("status"),
                    "presence_type": "claim",
                }
            )
    return presence


def recent_file_activity(
    conn: sqlite3.Connection, *, limit: int = 8, event_limit: int = 200
) -> list[dict[str, Any]]:
    events = recent_events(conn, limit=max(limit * 8, event_limit))
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        path = event.get("file_path")
        if not path:
            continue
        if path not in grouped:
            grouped[path] = {
                "file_path": path,
                "last_edited_at": event["timestamp"],
                "event_source": f"agent:{event['agent_name']}",
                "edit_count": 0,
                "activity_count": 0,
                "change_types": Counter(),
                "symbols": [],
                "agents": [],
                "run_ids": [],
                "last_event_type": event["event_type"],
                "last_message": event["message"],
            }
            order.append(path)
        item = grouped[path]
        if event["event_type"] == "edit":
            item["edit_count"] += 1
        item["activity_count"] += 1
        item["change_types"][event["event_type"]] += 1
        if event["agent_name"] not in item["agents"]:
            item["agents"].append(event["agent_name"])
        if event["run_id"] not in item["run_ids"]:
            item["run_ids"].append(event["run_id"])
        symbol = event.get("symbol_path")
        if symbol and symbol not in item["symbols"] and len(item["symbols"]) < 6:
            item["symbols"].append(symbol)

    # Compute per-file overlapping runs
    all_run_ids: set[str] = set()
    for item in grouped.values():
        all_run_ids.update(item["run_ids"])
    run_agents: dict[str, str] = {}
    if all_run_ids:
        placeholders = ",".join("?" for _ in all_run_ids)
        rows = conn.execute(
            f"""
            SELECT run_id, agent_name
              FROM agent_runs
             WHERE run_id IN ({placeholders})
            """,
            tuple(all_run_ids),
        ).fetchall()
        run_agents = {row["run_id"]: row["agent_name"] or "Agent" for row in rows}

    for path in order:
        item = grouped[path]
        own_run_ids = set(item["run_ids"])
        overlapping: list[dict[str, Any]] = []
        for other_path, other_item in grouped.items():
            if other_path == path:
                continue
            shared_runs = own_run_ids & set(other_item["run_ids"])
            if shared_runs:
                for rid in shared_runs:
                    overlapping.append(
                        {
                            "file_path": other_path,
                            "run_id": rid,
                            "agent_name": run_agents.get(rid, "Agent"),
                        }
                    )
        # Deduplicate by file_path + run_id
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for o in overlapping:
            key = (o["file_path"], o["run_id"])
            if key not in seen:
                seen.add(key)
                deduped.append(o)
        item["overlapping_files"] = deduped[:10]

    out: list[dict[str, Any]] = []
    for rank, path in enumerate(order[:limit], start=1):
        item = dict(grouped[path])
        item["rank"] = rank
        item["change_types"] = dict(sorted(item["change_types"].items()))
        out.append(item)
    return out


def _overlapping_run_analysis(
    conn: sqlite3.Connection,
    *,
    max_age_seconds: float | None = DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    """Detect files actively touched by multiple runs and assess conflict risk."""
    runs = active_runs(conn, limit=100, max_age_seconds=max_age_seconds)
    if len(runs) < 2:
        return []

    # Batch-load active claims once
    all_claims = active_file_claims(conn, limit=1000)
    claims_by_run: dict[str, list[dict[str, Any]]] = {}
    for claim in all_claims:
        claims_by_run.setdefault(claim["run_id"], []).append(claim)

    # Batch-load recent edit events per run for files without active claims
    run_ids = [run["run_id"] for run in runs]
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        event_rows = conn.execute(
            f"""
            SELECT r.run_id, e.file_path, e.event_type
              FROM agent_events e
              JOIN agent_runs r ON r.run_pk = e.run_pk
             WHERE r.run_id IN ({placeholders})
               AND e.file_path IS NOT NULL
               AND e.event_type IN ('edit', 'test')
            """,
            tuple(run_ids),
        ).fetchall()
    else:
        event_rows = []
    edit_files_by_run: dict[str, set[str]] = {}
    for row in event_rows:
        edit_files_by_run.setdefault(row["run_id"], set()).add(row["file_path"])

    run_files: dict[str, set[str]] = {}
    for run in runs:
        rid = run["run_id"]
        files: set[str] = set(run.get("active_files") or [])
        for claim in claims_by_run.get(rid, []):
            files.add(claim["file_path"])
        run_files[rid] = files

    overlaps: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, run_a in enumerate(runs):
        rid_a = run_a["run_id"]
        files_a = run_files[rid_a]
        for run_b in runs[i + 1 :]:
            rid_b = run_b["run_id"]
            shared = files_a & run_files[rid_b]
            if not shared:
                continue
            pair = tuple(sorted([rid_a, rid_b]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            severity = "low"
            for path in shared:
                modes_a = {
                    c["mode"]
                    for c in claims_by_run.get(rid_a, [])
                    if c["file_path"] == path
                }
                modes_b = {
                    c["mode"]
                    for c in claims_by_run.get(rid_b, [])
                    if c["file_path"] == path
                }
                has_write_a = bool(modes_a & EXCLUSIVE_CLAIM_MODES)
                has_write_b = bool(modes_b & EXCLUSIVE_CLAIM_MODES)
                edited_a = path in edit_files_by_run.get(rid_a, set())
                edited_b = path in edit_files_by_run.get(rid_b, set())
                if (has_write_a and has_write_b) or (edited_a and edited_b):
                    severity = "high"
                    break
                if has_write_a or has_write_b or edited_a or edited_b:
                    severity = "medium"

            overlaps.append(
                {
                    "run_id_a": rid_a,
                    "agent_name_a": run_a["agent_name"],
                    "run_id_b": rid_b,
                    "agent_name_b": run_b["agent_name"],
                    "shared_files": sorted(shared),
                    "severity": severity,
                    "message": (
                        f"{run_a['agent_name']} ({rid_a[:8]}) and "
                        f"{run_b['agent_name']} ({rid_b[:8]}) both touch "
                        f"{len(shared)} file(s)."
                    ),
                }
            )
    return overlaps


def agent_derived_file_relationships(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    min_observations: int = 1,
    max_age_seconds: float | None = DEFAULT_DERIVED_REL_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    """Infer file-to-file relationships from agent navigation patterns.

    When agents read or edit file A and then file B within the same run,
    that suggests a dynamic relationship that the static AST index may miss.
    """
    cutoff = _active_cutoff_iso(max_age_seconds)
    age_clause = ""
    params: list[Any] = []
    if cutoff:
        age_clause = "AND timestamp >= ?"
        params.append(cutoff)
    rows = conn.execute(
        f"""
        SELECT run_pk, file_path, timestamp, event_pk
          FROM agent_events
         WHERE file_path IS NOT NULL
           AND event_type IN ('read', 'edit', 'test', 'navigate')
           {age_clause}
         ORDER BY run_pk, timestamp ASC, event_pk ASC
        """,
        params,
    ).fetchall()

    run_sequences: dict[int, list[str]] = {}
    for row in rows:
        run_pk = int(row["run_pk"])
        path = row["file_path"]
        seq = run_sequences.setdefault(run_pk, [])
        if not seq or seq[-1] != path:
            seq.append(path)

    transition_counts: dict[tuple[str, str], int] = {}
    for sequence in run_sequences.values():
        deduped: list[str] = []
        for path in sequence:
            if not deduped or deduped[-1] != path:
                deduped.append(path)
        for i in range(len(deduped) - 1):
            a, b = deduped[i], deduped[i + 1]
            key = tuple(sorted([a, b]))
            transition_counts[key] = transition_counts.get(key, 0) + 1

    if not transition_counts:
        return []

    max_count = max(transition_counts.values())
    relationships: list[dict[str, Any]] = []
    for (a, b), count in sorted(
        transition_counts.items(), key=lambda x: -x[1]
    )[:limit]:
        if count < min_observations:
            continue
        confidence = count / max_count
        relationships.append(
            {
                "source": a,
                "target": b,
                "kind": "agent_derived",
                "confidence": round(confidence, 3),
                "observations": count,
                "rationale": (
                    f"Agents navigated between these files {count} time(s)."
                ),
            }
        )
    return relationships


def run_trajectory(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    event_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the ordered sequence of files an agent visited within a run.

    Useful for predicting where an agent is likely to go next and for
    detecting divergent navigation patterns across agentic teams.
    """
    run = get_run(conn, run_id)
    if run is None:
        return []
    types = event_types or {"read", "edit", "test", "navigate"}
    placeholders = ",".join("?" for _ in types)
    rows = conn.execute(
        f"""
        SELECT file_path, symbol_path, event_type, timestamp, message
          FROM agent_events
         WHERE run_pk = ?
           AND file_path IS NOT NULL
           AND event_type IN ({placeholders})
         ORDER BY timestamp ASC, event_pk ASC
        """,
        (run["run_pk"], *types),
    ).fetchall()
    trajectory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        path = row["file_path"]
        if not path:
            continue
        key = f"{path}:{row['event_type']}"
        if key in seen:
            continue
        seen.add(key)
        trajectory.append(
            {
                "file_path": path,
                "symbol_path": row["symbol_path"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "message": row["message"],
            }
        )
    return trajectory


def predict_next_files(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Predict which files an agent is likely to touch next.

    Combines the run's current trajectory with historically derived agent
    navigation patterns to surface likely next destinations. This helps
    other agents anticipate coordination needs before claims are created.
    """
    trajectory = run_trajectory(conn, run_id)
    if not trajectory:
        return []
    current_files = [t["file_path"] for t in trajectory]
    current_set = set(current_files)
    last_file = current_files[-1]

    # Load globally derived relationships (cached in-memory would be ideal,
    # but for now we compute a targeted subset).
    derived = agent_derived_file_relationships(conn, limit=200)
    candidates: dict[str, float] = {}
    for rel in derived:
        a, b = rel["source"], rel["target"]
        if a == last_file and b not in current_set:
            candidates[b] = max(candidates.get(b, 0.0), rel["confidence"])
        elif b == last_file and a not in current_set:
            candidates[a] = max(candidates.get(a, 0.0), rel["confidence"])

    # Also boost files that appear in the same run's existing trajectory
    # but haven't been visited recently (back-and-forth patterns).
    for t in trajectory[:-1]:
        path = t["file_path"]
        if path != last_file and path not in current_set:
            candidates[path] = candidates.get(path, 0.0) + 0.1

    # Boost files that are structurally related (imports) to current files.
    if current_files:
        placeholders = ",".join("?" for _ in current_files)
        rows = conn.execute(
            f"""
            SELECT DISTINCT df.file_path
              FROM relations r
              JOIN occurrences so ON so.symbol_pk = r.src_symbol_pk
                                   AND so.role = 'definition'
              JOIN files sf ON sf.file_pk = so.file_pk
              JOIN occurrences do ON do.symbol_pk = r.dst_symbol_pk
                                   AND do.role = 'definition'
              JOIN files df ON df.file_pk = do.file_pk
             WHERE sf.file_path IN ({placeholders})
               AND r.relation_kind IN ('calls', 'imports')
               AND sf.deleted_at IS NULL
               AND df.deleted_at IS NULL
            """,
            tuple(current_files),
        ).fetchall()
        for row in rows:
            path = row["file_path"]
            if path and path not in current_set:
                candidates[path] = candidates.get(path, 0.0) + 0.25

    sorted_candidates = sorted(
        candidates.items(), key=lambda x: -x[1]
    )[:limit]
    return [
        {"file_path": path, "confidence": round(score, 3), "reason": "trajectory_prediction"}
        for path, score in sorted_candidates
    ]


def dependency_claim_warnings(
    conn: sqlite3.Connection,
    selected_paths: list[str],
    *,
    run_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find active claims on files that selected_paths depend on.

    This surfaces transitive coordination risks: even if no other agent
    has claimed the exact same file, someone may be editing a dependency
    that could break your work.
    """
    paths = [_normal_path(p) for p in selected_paths if _normal_path(p)]
    if not paths:
        return []
    placeholders = ",".join("?" for _ in paths)
    # Find files that selected_paths call or import (one hop).
    rows = conn.execute(
        f"""
        SELECT DISTINCT df.file_path
          FROM relations r
          JOIN occurrences so ON so.symbol_pk = r.src_symbol_pk
                               AND so.role = 'definition'
          JOIN files sf ON sf.file_pk = so.file_pk
          JOIN occurrences do ON do.symbol_pk = r.dst_symbol_pk
                               AND do.role = 'definition'
          JOIN files df ON df.file_pk = do.file_pk
         WHERE sf.file_path IN ({placeholders})
           AND r.relation_kind IN ('calls', 'imports', 'inherits', 'implements')
           AND sf.deleted_at IS NULL
           AND df.deleted_at IS NULL
        """,
        tuple(paths),
    ).fetchall()
    dependency_files = {row["file_path"] for row in rows if row["file_path"]}
    if not dependency_files:
        return []

    # Find active claims on those dependency files from OTHER runs.
    now = _now_iso()
    dep_placeholders = ",".join("?" for _ in dependency_files)
    clauses = [
        "c.status = 'active'",
        f"c.file_path IN ({dep_placeholders})",
        "(c.expires_at IS NULL OR c.expires_at >= ?)",
        "r.archived_at IS NULL",
        _non_terminal_run_clause("r"),
    ]
    params: list[Any] = list(dependency_files)
    params.append(now)
    params.extend(STOPPED_STATUS_VALUES)
    if run_id:
        clauses.append("r.run_id != ?")
        params.append(run_id)

    claim_rows = conn.execute(
        f"""
        SELECT c.file_path, c.mode, c.reason,
               r.run_id, r.agent_name, r.status AS run_status
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE {" AND ".join(clauses)}
         ORDER BY c.mode = 'edit' DESC, c.mode = 'exclusive' DESC, c.updated_at DESC
         LIMIT ?
        """,
        (*params, max(0, int(limit))),
    ).fetchall()

    warnings: list[dict[str, Any]] = []
    for row in claim_rows:
        mode = str(row["mode"] or "")
        severity = "high" if mode in EXCLUSIVE_CLAIM_MODES else "medium"
        warnings.append(
            {
                "file_path": row["file_path"],
                "claimed_by_run_id": row["run_id"],
                "claimed_by_agent": row["agent_name"] or "Agent",
                "claim_mode": mode,
                "claim_reason": row["reason"] or "",
                "severity": severity,
                "message": (
                    f"{row['agent_name'] or 'Agent'} has an active {mode} claim on "
                    f"{row['file_path']}, which your selected files depend on."
                ),
            }
        )
    return warnings


def activity_snapshot(
    conn: sqlite3.Connection,
    *,
    event_limit: int = 100,
    file_limit: int = 8,
    active_run_max_age_seconds: float | None = DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    return {
        "active_runs": active_runs(
            conn, limit=5, max_age_seconds=active_run_max_age_seconds
        ),
        "recent_runs": recent_runs(conn, limit=8),
        "recent_events": recent_events(conn, limit=event_limit),
        "recent_files": recent_file_activity(
            conn, limit=file_limit, event_limit=max(event_limit, file_limit * 8)
        ),
        "active_claims": active_file_claims(conn, limit=100),
        "kanban": kanban_board(conn, limit=10),
        "overlapping_runs": _overlapping_run_analysis(
            conn, max_age_seconds=active_run_max_age_seconds
        ),
        "derived_relationships": agent_derived_file_relationships(conn, limit=50),
        "file_presence": file_presence(conn, limit=200),
    }
