"""Graph payload and activity state for the live graph server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index import agent_providers
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_orchestrator
from code_index import scopes
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


def _agent_activity_signature(conn: sqlite3.Connection, event_pk: int) -> str:
    try:
        run_row = conn.execute(
            """
            SELECT COUNT(*) AS run_count,
                   COALESCE(MAX(updated_at), '') AS updated_at
              FROM agent_runs
             WHERE archived_at IS NULL
            """
        ).fetchone()
        claim_row = conn.execute(
            """
            SELECT COUNT(*) AS claim_count,
                   COALESCE(MAX(updated_at), '') AS updated_at
              FROM agent_file_claims
             WHERE status = 'active'
               AND (expires_at IS NULL OR expires_at >= ?)
            """,
            (datetime.now(timezone.utc).isoformat(timespec="milliseconds"),),
        ).fetchone()
        process_row = conn.execute(
            """
            SELECT COUNT(*) AS process_count,
                   COALESCE(MAX(heartbeat_at), '') AS heartbeat_at,
                   COALESCE(MAX(ended_at), '') AS ended_at
              FROM agent_run_processes
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return str(event_pk)
    return ":".join(
        [
            str(event_pk),
            str(run_row["run_count"] if run_row else 0),
            str(run_row["updated_at"] if run_row else ""),
            str(claim_row["claim_count"] if claim_row else 0),
            str(claim_row["updated_at"] if claim_row else ""),
            str(process_row["process_count"] if process_row else 0),
            str(process_row["heartbeat_at"] if process_row else ""),
            str(process_row["ended_at"] if process_row else ""),
        ]
    )


def _notes_mtime(root: Path) -> int:
    path = notes_path(root)
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _dynamic_edge_signature(
    config: cfg_mod.Config,
    *,
    conn=None,
    return_relationships: bool = False,
) -> str | tuple[str, list[dict[str, Any]]]:
    """Return a hashable signature of current agent-derived file relationships.

    When *conn* is provided, the connection is reused instead of opened/closed.
    When *return_relationships* is True, also return the raw relationships list.
    """
    close_conn = conn is None
    if close_conn:
        conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        relationships = agent_activity.agent_derived_file_relationships(
            conn, limit=200, min_observations=1
        )
    finally:
        if close_conn:
            db_mod.close(conn)
    if not relationships:
        if return_relationships:
            return "", []
        return ""
    parts = []
    for rel in relationships:
        parts.append(
            f"{rel.get('source')}:{rel.get('target')}:{rel.get('observations')}"
        )
    signature = hashlib.sha256(
        "|".join(parts).encode("utf-8")
    ).hexdigest()[:32]
    if return_relationships:
        return signature, relationships
    return signature


def _state_signature(config: cfg_mod.Config, *, conn=None) -> dict[str, Any]:
    close_conn = conn is None
    if close_conn:
        conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        event_pk = _latest_event_pk(conn)
        agent_signature = _agent_activity_signature(conn, event_pk)
    finally:
        if close_conn:
            db_mod.close(conn)
    return {
        "event_pk": event_pk,
        "agent_signature": agent_signature,
        "notes_mtime": _notes_mtime(config.root),
    }


def _agent_stream_payload(config: cfg_mod.Config) -> dict[str, Any]:
    _reconcile_agent_runs(config)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        event_pk = _latest_event_pk(conn)
        snapshot = run_orchestrator.annotate_activity_snapshot(
            agent_activity.activity_snapshot(conn, event_limit=80, file_limit=8)
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
            "kanban": snapshot.get("kanban"),
            "orchestrator": snapshot.get("orchestrator"),
            "status": "working" if snapshot["active_runs"] else "idle",
        },
        "activity": {
            "agent_events": snapshot["recent_events"],
            "agent_recent_files": snapshot["recent_files"],
            "active_claims": snapshot.get("active_claims", []),
            "file_presence": snapshot.get("file_presence", {}),
            "overlapping_runs": snapshot.get("overlapping_runs", []),
        },
    }


def _agent_runtime_payload() -> dict[str, Any]:
    provider = os.environ.get("CODE_INDEX_AGENT_PROVIDER", "").strip().lower()
    command = os.environ.get("CODE_INDEX_AGENT_COMMAND", "").strip()
    webhook = os.environ.get("CODE_INDEX_AGENT_WEBHOOK_URL", "").strip()
    providers = agent_providers.provider_registry_payload()
    provider_presets = [
        str(provider["id"])
        for provider in providers
        if isinstance(provider, dict) and provider.get("command_preset")
    ]
    return {
        "kind": "code_index_agent_runtime",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "dispatch": {
            "webhook_configured": bool(webhook),
            "local_command_configured": bool(command or provider),
            "provider": provider or None,
            "custom_command_configured": bool(command),
            "provider_presets": provider_presets,
        },
    }


def _reconcile_agent_runs(config: cfg_mod.Config) -> dict[str, Any]:
    """Apply deterministic Agent Run lifecycle updates before live snapshots."""

    with writer_lock(config, timeout_s=5.0):
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            return run_orchestrator.apply(conn)
        finally:
            db_mod.close(conn)


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
    _reconcile_agent_runs(config)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        scope_selection = scopes.resolve_scope_from_args(config.root, args)
        focus_paths = list(args.focus or [])
        if scope_selection.explicit:
            focus_paths.extend(
                path
                for path in scopes.indexed_file_paths_for_scope(
                    conn,
                    scope_selection,
                    limit=200,
                )
                if path not in focus_paths
            )
        payload = build_graph(
            conn,
            config.root,
            include_code=not args.no_code,
            max_code_bytes=max(0, int(args.max_code_bytes)),
            focus_paths=focus_paths,
            agent_name=args.agent_name,
        )
        payload["scope"] = scope_selection.to_dict()
        payload["live"] = {
            "server": True,
            "events_path": "/events",
            "notes_path": "/api/notes",
            "search_path": "/api/search",
            "agent_preflight_path": "/api/agent-task-preflight",
            "agent_runs_path": "/api/agent-runs",
            "agent_events_path": "/api/agent-events",
            "agent_board_path": "/api/agent-board",
            "agent_providers_path": "/api/agent-providers",
            "agent_providers": agent_providers.provider_registry_payload(),
            "agent_runtime": _agent_runtime_payload(),
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


def _parse_debug_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _sanitize_claim_for_debug(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": claim.get("claim_id"),
        "run_id": claim.get("run_id"),
        "agent_name": claim.get("agent_name"),
        "run_status": claim.get("run_status"),
        "file_path": claim.get("file_path"),
        "mode": claim.get("mode"),
        "status": claim.get("status"),
        "created_at": claim.get("created_at"),
        "updated_at": claim.get("updated_at"),
        "heartbeat_at": claim.get("heartbeat_at"),
        "expires_at": claim.get("expires_at"),
    }


def _sanitize_run_for_debug(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "agent_name": run.get("agent_name"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at"),
        "active_files": run.get("active_files") or [],
    }


def _debug_active_runs_all(config: cfg_mod.Config) -> list[dict[str, Any]]:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        return agent_activity.active_runs(conn, limit=100, max_age_seconds=None)
    finally:
        db_mod.close(conn)


def _debug_ops_snapshot(
    *,
    agent: dict[str, Any],
    perf: dict[str, Any],
) -> dict[str, Any]:
    counters = perf.get("counters") if isinstance(perf.get("counters"), dict) else {}
    try:
        stale_after_seconds = float(
            os.environ.get("CODE_INDEX_GRAPH_STALE_RUN_SECONDS")
            or agent_activity.DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS
        )
    except ValueError:
        stale_after_seconds = float(agent_activity.DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    stale_runs: list[dict[str, Any]] = []
    for run in agent.get("active_runs_all") or agent.get("active_runs") or []:
        updated_at = _parse_debug_iso(run.get("updated_at"))
        if updated_at and updated_at < stale_cutoff:
            stale_runs.append(_sanitize_run_for_debug(run))
    search_latency = counters.get("search_latency_ms")
    if not isinstance(search_latency, dict):
        search_latency = {"count": 0, "last": None, "max": None, "avg": None}
    retrieval_budget = counters.get("retrieval_budget")
    if not isinstance(retrieval_budget, dict):
        retrieval_budget = {
            "broker_configured": False,
            "requests": 0,
            "budget_rejections": 0,
        }
    broker_configured = bool(retrieval_budget.get("broker_configured"))
    active_claims = [
        _sanitize_claim_for_debug(claim)
        for claim in agent.get("active_claims") or []
        if isinstance(claim, dict)
    ]
    return {
        "preflight": {
            "rejections": counters.get("preflight_rejections") or {},
        },
        "auth": {
            "failures": counters.get("auth_failures") or {},
        },
        "claims": {
            "active_count": len(active_claims),
            "conflict_count": int(counters.get("claim_conflicts") or 0),
            "active": active_claims[:20],
        },
        "sse": {
            "dropped_events": int(counters.get("sse_dropped_events") or 0),
        },
        "runs": {
            "stale_after_seconds": stale_after_seconds,
            "stale_count": len(stale_runs),
            "stale": stale_runs[:20],
        },
        "search": {
            "latency_ms": search_latency,
        },
        "retrieval_budget": {
            "broker_configured": broker_configured,
            "requests": int(retrieval_budget.get("requests") or 0),
            "budget_rejections": int(retrieval_budget.get("budget_rejections") or 0),
            "placeholder": not broker_configured,
            "note": (
                "Graph search uses the shared retrieval broker."
                if broker_configured
                else "Runtime retrieval budget broker is not wired into graph-server yet."
            ),
        },
    }


def _debug_secret_values() -> list[str]:
    needles: list[str] = []
    sensitive_markers = (
        "TOKEN",
        "SECRET",
        "COOKIE",
        "PASSWORD",
        "WEBHOOK",
        "COMMAND",
        "KEY",
    )
    for name, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if any(marker in name.upper() for marker in sensitive_markers):
            needles.append(value)
    return sorted(set(needles), key=len, reverse=True)


def _sanitize_debug_payload(value: Any, secret_values: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_debug_payload(item, secret_values)
            for key, item in value.items()
            if key not in {"fence_token", "lease_token", "bearer_token", "session_cookie"}
        }
    if isinstance(value, list):
        return [_sanitize_debug_payload(item, secret_values) for item in value]
    if isinstance(value, str):
        sanitized = value
        for secret in secret_values:
            sanitized = sanitized.replace(secret, "[redacted]")
        return sanitized
    return value


def _build_debug_payload(
    config: cfg_mod.Config,
    args: argparse.Namespace,
    perf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact debug snapshot for humans, agents, and health checks."""

    started = time.perf_counter()
    payload = _build_payload(config, args)
    build_ms = round((time.perf_counter() - started) * 1000, 2)
    payload_size = _json_payload_bytes(payload)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
    agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
    agent_for_ops = dict(agent)
    agent_for_ops["active_runs_all"] = _debug_active_runs_all(config)
    notes = payload.get("notes") if isinstance(payload.get("notes"), dict) else {}
    code_metrics = _embedded_code_metrics(payload)

    runtime = _agent_runtime_payload()
    dispatch = runtime["dispatch"]
    perf_payload = perf or {
        "kind": "code_index_graph_debug_perf",
        "counters": {
            "preflight_rejections": {},
            "auth_failures": {},
            "sse_dropped_events": 0,
            "claim_conflicts": 0,
            "stale_runs": 0,
            "retrieval_budget": {
                "broker_configured": True,
                "requests": 0,
                "budget_rejections": 0,
            },
            "search_latency_ms": {
                "count": 0,
                "last": None,
                "max": None,
                "avg": None,
                "by_scope": {},
            },
        },
    }
    ops_payload = _debug_ops_snapshot(agent=agent_for_ops, perf=perf_payload)
    if isinstance(perf_payload.get("counters"), dict):
        perf_payload["counters"]["stale_runs"] = ops_payload["runs"]["stale_count"]
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
            "agent_dispatch": dispatch,
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
        "perf": perf_payload,
        "ops": ops_payload,
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
    return _sanitize_debug_payload(debug, _debug_secret_values())
