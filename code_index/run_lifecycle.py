"""Durable process lifecycle registry for Agent Runs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from code_index import agent_activity


ACTIVE_PROCESS_STATUSES = {"started"}
TERMINAL_PROCESS_STATUSES = {"completed", "failed", "cancelled", "orphaned"}
PROCESS_STATUSES = ACTIVE_PROCESS_STATUSES | TERMINAL_PROCESS_STATUSES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _row_to_process(row: sqlite3.Row) -> dict[str, Any]:
    return {
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
        "metadata": _json_loads(row["metadata_json"]) or {},
    }


def _get_process(conn: sqlite3.Connection, process_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT p.*, r.run_id
          FROM agent_run_processes p
          JOIN agent_runs r ON r.run_pk = p.run_pk
         WHERE p.process_id = ?
        """,
        (process_id,),
    ).fetchone()
    return _row_to_process(row) if row is not None else None


def register_process(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    transport: str,
    provider: str | None = None,
    pid: int | None = None,
    command_label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = agent_activity.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown agent run: {run_id}")
    process_id = uuid.uuid4().hex
    now = _now_iso()
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
            str(transport),
            provider,
            pid,
            command_label,
            now,
            now,
            _json_dumps(metadata or {}),
        ),
    )
    process = _get_process(conn, process_id)
    assert process is not None
    return process


def heartbeat_process(
    conn: sqlite3.Connection,
    *,
    process_id: str,
) -> dict[str, Any] | None:
    now = _now_iso()
    conn.execute(
        """
        UPDATE agent_run_processes
           SET heartbeat_at = ?
         WHERE process_id = ?
           AND status = 'started'
        """,
        (now, process_id),
    )
    return _get_process(conn, process_id)


def finish_process(
    conn: sqlite3.Connection,
    *,
    process_id: str,
    status: str,
    exit_code: int | None,
) -> dict[str, Any]:
    normalized = str(status or "").strip().lower().replace("-", "_")
    if normalized not in TERMINAL_PROCESS_STATUSES:
        raise ValueError(f"process status must be terminal: {status}")
    now = _now_iso()
    conn.execute(
        """
        UPDATE agent_run_processes
           SET status = ?,
               ended_at = ?,
               heartbeat_at = ?,
               exit_code = ?
         WHERE process_id = ?
        """,
        (normalized, now, now, exit_code, process_id),
    )
    process = _get_process(conn, process_id)
    if process is None:
        raise ValueError(f"unknown agent run process: {process_id}")
    return process


def process_liveness_by_run(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT run_id, status
          FROM (
                SELECT r.run_id,
                       p.status,
                       ROW_NUMBER() OVER (
                           PARTITION BY r.run_pk
                           ORDER BY COALESCE(p.heartbeat_at, p.ended_at, p.started_at) DESC,
                                    p.process_pk DESC
                       ) AS rn
                  FROM agent_run_processes p
                  JOIN agent_runs r ON r.run_pk = p.run_pk
               )
         WHERE rn = 1
        """
    ).fetchall()
    liveness: dict[str, str] = {}
    for row in rows:
        if row["status"] in ACTIVE_PROCESS_STATUSES:
            liveness[row["run_id"]] = "alive"
        elif row["status"] in TERMINAL_PROCESS_STATUSES:
            liveness[row["run_id"]] = "dead"
    return liveness
