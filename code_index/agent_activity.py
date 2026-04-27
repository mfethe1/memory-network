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
from datetime import datetime, timezone
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}
WORK_EVENT_TYPES = {"read", "edit", "test", "tool", "navigate", "note"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
        "metadata": _json_loads(row["metadata_json"], {}),
        "active_files": _active_files_for_run(conn, run_pk),
    }


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
    conn: sqlite3.Connection, *, agent_name: str | None = None
) -> dict[str, Any] | None:
    params: list[Any] = sorted(TERMINAL_STATUSES)
    agent_filter = ""
    if agent_name:
        agent_filter = "AND agent_name = ?"
        params.append(agent_name)
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    row = conn.execute(
        f"""
        SELECT *
          FROM agent_runs
         WHERE LOWER(COALESCE(status, 'working')) NOT IN ({placeholders})
           {agent_filter}
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    return _row_to_run(conn, row)


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
    conn.execute(
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
         WHERE e.event_pk = last_insert_rowid()
        """
    ).fetchone()
    return _row_to_event(row)


def end_run(
    conn: sqlite3.Connection, *, run_id: str, status: str = "completed"
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run_id: {run_id}")
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


def active_runs(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        f"""
        SELECT *
          FROM agent_runs
         WHERE LOWER(COALESCE(status, 'working')) NOT IN ({placeholders})
         ORDER BY updated_at DESC, started_at DESC, run_pk DESC
         LIMIT ?
        """,
        (*sorted(TERMINAL_STATUSES), max(0, int(limit))),
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
    conn: sqlite3.Connection, *, event_limit: int = 100, file_limit: int = 8
) -> dict[str, Any]:
    return {
        "active_runs": active_runs(conn, limit=5),
        "recent_events": recent_events(conn, limit=event_limit),
        "recent_files": recent_file_activity(
            conn, limit=file_limit, event_limit=max(event_limit, file_limit * 8)
        ),
    }
