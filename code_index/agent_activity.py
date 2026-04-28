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
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}
WORK_EVENT_TYPES = {"read", "edit", "test", "tool", "navigate", "note"}
SUGGESTION_EVENT_TYPE = "suggestion"
DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS = 4 * 60 * 60
DEFAULT_CLAIM_TTL_SECONDS = 30 * 60
EVENT_CLAIM_MODES = {
    "read": "read",
    "navigate": "read",
    "edit": "edit",
    "test": "test",
}


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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "metadata": _json_loads(row["metadata_json"], {}),
    }


def _active_files_for_run(
    conn: sqlite3.Connection, run_pk: int, *, limit: int = 5
) -> list[str]:
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
    files: list[str] = []
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


def latest_active_run(
    conn: sqlite3.Connection,
    *,
    agent_name: str | None = None,
    max_age_seconds: float | None = DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    params: list[Any] = sorted(TERMINAL_STATUSES)
    agent_filter = ""
    age_filter = ""
    if agent_name:
        agent_filter = "AND agent_name = ?"
        params.append(agent_name)
    cutoff = _active_cutoff_iso(max_age_seconds)
    if cutoff:
        age_filter = "AND updated_at >= ?"
        params.append(cutoff)
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        f"""
        SELECT *
          FROM agent_runs
         WHERE LOWER(COALESCE(status, 'working')) NOT IN ({placeholders})
           AND archived_at IS NULL
           {agent_filter}
           {age_filter}
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT 20
        """,
        params,
    ).fetchall()
    for row in rows:
        run = _row_to_run(conn, row)
        if not _is_orphan_graph_event_run(conn, run):
            return run
    return None


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


def claim_file(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    mode: str = "edit",
    reason: str | None = None,
    ttl_seconds: float | None = DEFAULT_CLAIM_TTL_SECONDS,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
    path = _normal_path(file_path)
    if not path:
        raise ValueError("file_path is required")
    claim_mode = (mode or "edit").strip().lower()
    if claim_mode not in {"read", "edit", "review", "test"}:
        raise ValueError(f"unknown claim mode: {claim_mode}")
    now = _now_iso()
    claim_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{path}:{claim_mode}").hex
    conn.execute(
        """
        INSERT INTO agent_file_claims(
            claim_id, run_pk, file_path, mode, status, reason, created_at,
            updated_at, heartbeat_at, expires_at, released_at, metadata_json
        )
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(run_pk, file_path, mode) DO UPDATE SET
            status = 'active',
            reason = excluded.reason,
            updated_at = excluded.updated_at,
            heartbeat_at = excluded.heartbeat_at,
            expires_at = excluded.expires_at,
            released_at = NULL,
            metadata_json = excluded.metadata_json
        """,
        (
            claim_id,
            run["run_pk"],
            path,
            claim_mode,
            reason or "",
            now,
            now,
            now,
            _iso_after(ttl_seconds),
            _json_dumps(metadata or {}),
        ),
    )
    row = conn.execute(
        """
        SELECT c.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE c.run_pk = ?
           AND c.file_path = ?
           AND c.mode = ?
        """,
        (run["run_pk"], path, claim_mode),
    ).fetchone()
    return _row_to_claim(row)


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
    select_clauses = ["c.run_pk = ?", "c.status = 'active'"]
    update_clauses = ["run_pk = ?", "status = 'active'"]
    params: list[Any] = [run["run_pk"]]
    path = _normal_path(file_path)
    if path:
        select_clauses.append("c.file_path = ?")
        update_clauses.append("file_path = ?")
        params.append(path)
    if mode:
        select_clauses.append("c.mode = ?")
        update_clauses.append("mode = ?")
        params.append(mode.strip().lower())
    rows = conn.execute(
        f"""
        SELECT c.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
         FROM agent_file_claims c
         JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE {" AND ".join(select_clauses)}
         ORDER BY c.updated_at DESC, c.claim_pk DESC
        """,
        params,
    ).fetchall()
    claim_pks = [int(row["claim_pk"]) for row in rows]
    now = _now_iso()
    conn.execute(
        f"""
        UPDATE agent_file_claims
               SET status = ?,
                   updated_at = ?,
                   released_at = ?
         WHERE {" AND ".join(update_clauses)}
        """,
        [next_status, now, now, *params],
    )
    if not claim_pks:
        return []
    placeholders = ",".join("?" for _ in claim_pks)
    updated_rows = conn.execute(
        f"""
        SELECT c.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE c.claim_pk IN ({placeholders})
         ORDER BY c.updated_at DESC, c.claim_pk DESC
        """,
        claim_pks,
    ).fetchall()
    return [_row_to_claim(row) for row in updated_rows]


def active_file_claims(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    file_path: str | None = None,
) -> list[dict[str, Any]]:
    clauses = [
        "c.status = 'active'",
        "(c.expires_at IS NULL OR c.expires_at >= ?)",
        "r.archived_at IS NULL",
        f"LOWER(COALESCE(r.status, 'working')) NOT IN ({','.join('?' for _ in TERMINAL_STATUSES)})",
    ]
    params: list[Any] = [_now_iso(), *sorted(TERMINAL_STATUSES)]
    path = _normal_path(file_path)
    if path:
        clauses.append("c.file_path = ?")
        params.append(path)
    params.append(max(0, int(limit)))
    rows = conn.execute(
        f"""
        SELECT c.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE {" AND ".join(clauses)}
         ORDER BY
           CASE c.mode
             WHEN 'edit' THEN 0
             WHEN 'test' THEN 1
             WHEN 'review' THEN 2
             ELSE 3
           END,
           c.updated_at DESC,
           c.claim_pk DESC
         LIMIT ?
        """,
        params,
    ).fetchall()
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
        if current not in TERMINAL_STATUSES:
            next_status = "working"
    conn.execute(
        """
        UPDATE agent_runs
           SET updated_at = ?,
               status = COALESCE(?, status)
         WHERE run_pk = ?
        """,
        (when, next_status, run["run_pk"]),
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
    if next_status and str(next_status).lower() in TERMINAL_STATUSES:
        release_claims(conn, run_id=run_id)
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
    event_types = Counter(event["event_type"] for event in events)
    files_touched: list[str] = []
    for event in events:
        path = event.get("file_path")
        if path and path not in files_touched:
            files_touched.append(path)
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
    }
    return {
        "run": run,
        "events": events,
        "decisions": decisions,
        "active_files": active_files,
        "suggestions": build_run_suggestions(conn, run_id),
        "summary": summary,
        "summaries": summary,
    }


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
    updated = get_run(conn, run_id)
    assert updated is not None
    return updated


def active_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 5,
    max_age_seconds: float | None = DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    params: list[Any] = sorted(TERMINAL_STATUSES)
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
         WHERE LOWER(COALESCE(status, 'working')) NOT IN ({placeholders})
           AND archived_at IS NULL
           {age_filter}
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT ?
        """,
        params,
    ).fetchall()
    runs: list[dict[str, Any]] = []
    for row in rows:
        run = _row_to_run(conn, row)
        if _is_orphan_graph_event_run(conn, run):
            continue
        runs.append(run)
        if len(runs) >= requested_limit:
            break
    return runs


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
    runs: list[dict[str, Any]] = []
    for row in rows:
        run = _row_to_run(conn, row)
        if not include_orphan and _is_orphan_graph_event_run(conn, run):
            continue
        runs.append(run)
        if len(runs) >= requested_limit:
            break
    return runs


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
    out: list[dict[str, Any]] = []
    for rank, path in enumerate(order[:limit], start=1):
        item = dict(grouped[path])
        item["rank"] = rank
        item["change_types"] = dict(sorted(item["change_types"].items()))
        out.append(item)
    return out


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
    }
