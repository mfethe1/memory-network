"""Graph payload and activity state for the live graph server."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.agent_collaboration import append_event_jsonl
from code_index.commands.graph_model import build_graph
from code_index.commands.graph_notes import notes_path
from code_index.commands.graph_server_utils import GRAPH_TOKEN_ENV_VAR
from code_index.locking import writer_lock


def _latest_event_pk(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COALESCE(MAX(event_pk), 0) FROM agent_events").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def _notes_mtime(root: Path) -> int:
    path = notes_path(root)
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _state_signature(config: cfg_mod.Config) -> dict[str, Any]:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        event_pk = _latest_event_pk(conn)
    finally:
        db_mod.close(conn)
    return {
        "event_pk": event_pk,
        "notes_mtime": _notes_mtime(config.root),
    }


def _agent_stream_payload(config: cfg_mod.Config) -> dict[str, Any]:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        event_pk = _latest_event_pk(conn)
        snapshot = agent_activity.activity_snapshot(
            conn, event_limit=80, file_limit=8
        )
    finally:
        db_mod.close(conn)
    active_files: list[str] = []
    for run in snapshot["active_runs"]:
        for path in run.get("active_files", []):
            if path and path not in active_files:
                active_files.append(path)
        metadata = run.get("metadata") or {}
        for path in metadata.get("selected_paths", []):
            if path and path not in active_files:
                active_files.append(path)
    for claim in snapshot.get("active_claims", []):
        path = claim.get("file_path")
        if path and path not in active_files:
            active_files.append(path)
    active_agents = sorted(
        {
            run.get("agent_name") or "Agent"
            for run in snapshot["active_runs"]
        }
    )
    return {
        "type": "agent",
        "event_pk": event_pk,
        "agent": {
            "active_agents": active_agents,
            "active_files": active_files,
            "active_runs": snapshot["active_runs"],
            "recent_runs": snapshot.get("recent_runs", []),
            "active_claims": snapshot.get("active_claims", []),
            "status": "working" if snapshot["active_runs"] else "idle",
        },
        "activity": {
            "agent_events": snapshot["recent_events"],
            "agent_recent_files": snapshot["recent_files"],
            "active_claims": snapshot.get("active_claims", []),
        },
    }


def _record_user_note_event(
    config: cfg_mod.Config, note: dict[str, Any], saved: dict[str, Any]
) -> None:
    file_path = saved.get("path")
    message = saved.get("note") or "Cleared graph note."
    with writer_lock(config):
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            run = agent_activity.latest_active_run(conn, agent_name="User")
            if run is None:
                run = agent_activity.start_run(
                    conn,
                    agent_name="User",
                    prompt="Graph notes",
                    metadata={"source": "graph-server"},
                )
            event = agent_activity.record_event(
                conn,
                run_id=run["run_id"],
                event_type="note",
                file_path=file_path,
                message=message,
                payload={
                    "node_id": note.get("node_id"),
                    "care_level": saved.get("care_level"),
                },
            )
            append_event_jsonl(config.root, event)
        finally:
            db_mod.close(conn)


def _build_payload(config: cfg_mod.Config, args: argparse.Namespace) -> dict[str, Any]:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        payload = build_graph(
            conn,
            config.root,
            include_code=not args.no_code,
            max_code_bytes=max(0, int(args.max_code_bytes)),
            focus_paths=args.focus or [],
            agent_name=args.agent_name,
        )
        payload["live"] = {
            "server": True,
            "events_path": "/events",
            "notes_path": "/api/notes",
            "search_path": "/api/search",
            "agent_preflight_path": "/api/agent-task-preflight",
            "agent_runs_path": "/api/agent-runs",
            "agent_events_path": "/api/agent-events",
        }
        return payload
    finally:
        db_mod.close(conn)


def _json_payload_bytes(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    )


def _embedded_code_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    embedded_files = 0
    embedded_bytes = 0
    omitted_files = 0
    omitted_reasons: dict[str, int] = {}
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != "file":
            continue
        code = node.get("code") if isinstance(node.get("code"), dict) else {}
        if code.get("included"):
            embedded_files += 1
            embedded_bytes += len(str(code.get("content") or "").encode("utf-8"))
        else:
            omitted_files += 1
            reason = str(code.get("reason") or "unknown")
            omitted_reasons[reason] = omitted_reasons.get(reason, 0) + 1
    return {
        "embedded_files": embedded_files,
        "embedded_bytes": embedded_bytes,
        "omitted_files": omitted_files,
        "omitted_reasons": dict(sorted(omitted_reasons.items())),
    }


def _path_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _debug_recommendations(
    *, payload_size: int, summary: dict[str, Any], code: dict[str, Any]
) -> list[str]:
    recommendations: list[str] = []
    if payload_size >= 5_000_000:
        recommendations.append(
            "Graph payload is large; start graph-server with --no-code or fetch code lazily."
        )
    if int(summary.get("node_count") or 0) >= 750:
        recommendations.append(
            "Node count is high; use layered views and render only the visible subgraph."
        )
    if int(summary.get("edge_count") or 0) >= 2_000:
        recommendations.append(
            "Edge count is high; collapse directory/package layers before showing relation edges."
        )
    if int(code.get("embedded_bytes") or 0) >= 2_000_000:
        recommendations.append(
            "Embedded source dominates payload size; prefer selected-file source retrieval."
        )
    return recommendations


def _build_debug_payload(
    config: cfg_mod.Config, args: argparse.Namespace
) -> dict[str, Any]:
    """Build a compact debug snapshot for humans, agents, and health checks."""

    started = time.perf_counter()
    payload = _build_payload(config, args)
    build_ms = round((time.perf_counter() - started) * 1000, 2)
    payload_size = _json_payload_bytes(payload)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
    agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
    notes = payload.get("notes") if isinstance(payload.get("notes"), dict) else {}
    code_metrics = _embedded_code_metrics(payload)

    provider = os.environ.get("CODE_INDEX_AGENT_PROVIDER", "").strip()
    command = os.environ.get("CODE_INDEX_AGENT_COMMAND", "").strip()
    webhook = os.environ.get("CODE_INDEX_AGENT_WEBHOOK_URL", "").strip()
    debug = {
        "kind": "code_index_graph_debug",
        "schema_version": 1,
        "root": str(config.root),
        "generated_at": payload.get("generated_at"),
        "graph": {
            "build_ms": build_ms,
            "payload_bytes": payload_size,
            "node_count": summary.get("node_count"),
            "file_count": summary.get("file_count"),
            "directory_count": summary.get("directory_count"),
            "edge_count": summary.get("edge_count"),
            "relation_edge_count": summary.get("relation_edge_count"),
            "care_counts": summary.get("care_counts") or {},
            "language_counts": summary.get("language_counts") or {},
            "role_counts": summary.get("role_counts") or {},
            "embedded_code": code_metrics,
        },
        "index": {
            "db": _path_stat(config.db_path),
            "index_dir": _path_stat(config.index_dir),
            "notes": _path_stat(notes_path(config.root)),
            "root": str(config.root),
        },
        "server": {
            "live": True,
            "auth_enabled": bool(os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()),
            "event_interval_seconds": float(
                getattr(args, "event_interval", 1.0) or 1.0
            ),
            "include_code": not bool(getattr(args, "no_code", False)),
            "max_code_bytes": int(getattr(args, "max_code_bytes", 200_000) or 0),
            "agent_dispatch": {
                "webhook_configured": bool(webhook),
                "local_command_configured": bool(command or provider),
                "provider": provider or None,
                "custom_command_configured": bool(command),
            },
        },
        "activity": {
            "active_run_count": len(agent.get("active_runs") or []),
            "active_claim_count": len(agent.get("active_claims") or []),
            "recent_run_count": len(agent.get("recent_runs") or []),
            "active_file_count": len(agent.get("active_files") or []),
            "recent_event_count": len(activity.get("agent_events") or []),
            "recent_file_count": len(activity.get("agent_recent_files") or []),
            "active_agents": agent.get("active_agents") or [],
        },
        "notes": {
            "count": notes.get("count") or 0,
            "updated_at": notes.get("updated_at"),
            "path": notes.get("path"),
        },
    }
    debug["recommendations"] = _debug_recommendations(
        payload_size=payload_size,
        summary=summary,
        code=code_metrics,
    )
    return debug
