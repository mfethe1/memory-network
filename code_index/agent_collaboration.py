"""Shared agent coordination packets and append-only JSONL feeds."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from code_index import agent_activity


_WRITE_LOCK = threading.Lock()
_MAX_EVENT_MESSAGE_CHARS = 1200


def _safe_run_id(value: Any) -> str:
    text = str(value or "run")
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


def _normal_path(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text or None


def _rel_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def agent_runs_dir(root: Path) -> Path:
    return root / ".code_index" / "agent-runs"


def global_events_jsonl_path(root: Path) -> Path:
    return agent_runs_dir(root) / "events.jsonl"


def run_events_jsonl_path(root: Path, run_id: str) -> Path:
    return agent_runs_dir(root) / _safe_run_id(run_id) / "events.jsonl"


def _compact_message(message: Any) -> str:
    text = str(message or "")
    if len(text) <= _MAX_EVENT_MESSAGE_CHARS:
        return text
    return text[:_MAX_EVENT_MESSAGE_CHARS].rstrip() + "\n...[truncated]"


def _event_jsonl_record(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "kind": "code_index_agent_event",
        "schema_version": 1,
        "event_pk": event.get("event_pk"),
        "run_id": event.get("run_id"),
        "agent_name": event.get("agent_name") or "Agent",
        "run_status": event.get("run_status") or "working",
        "timestamp": event.get("timestamp"),
        "event_type": event.get("event_type"),
        "file_path": event.get("file_path"),
        "symbol_path": event.get("symbol_path"),
        "message": _compact_message(event.get("message")),
        "payload": payload,
    }


def append_event_jsonl(root: Path, event: dict[str, Any] | None) -> None:
    """Append an event to the repo-wide and per-run coordination feeds."""

    if not event or not event.get("run_id"):
        return
    record = _event_jsonl_record(event)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    paths = [
        global_events_jsonl_path(root),
        run_events_jsonl_path(root, str(event["run_id"])),
    ]
    with _WRITE_LOCK:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def _run_paths(run: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def add(value: Any) -> None:
        path = _normal_path(value)
        if path and path not in paths:
            paths.append(path)

    for path in run.get("active_files") or []:
        add(path)
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    for path in metadata.get("selected_paths") or []:
        add(path)
    return paths


def _event_preview(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_pk": event.get("event_pk"),
        "run_id": event.get("run_id"),
        "agent_name": event.get("agent_name") or "Agent",
        "run_status": event.get("run_status") or "working",
        "timestamp": event.get("timestamp"),
        "event_type": event.get("event_type"),
        "file_path": event.get("file_path"),
        "symbol_path": event.get("symbol_path"),
        "message": _compact_message(event.get("message")),
    }


def _run_preview(run: dict[str, Any], selected_paths: set[str]) -> dict[str, Any]:
    files = _run_paths(run)
    overlap = sorted(path for path in files if path in selected_paths)
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    return {
        "run_id": run.get("run_id"),
        "agent_name": run.get("agent_name") or "Agent",
        "status": run.get("status") or "working",
        "prompt": run.get("prompt") or "",
        "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at"),
        "selected_nodes": list(run.get("selected_nodes") or [])[:12],
        "selected_paths": list(metadata.get("selected_paths") or [])[:12],
        "active_files": files[:12],
        "overlap_files": overlap,
        "transcript_api_path": f"/api/agent-runs/{run.get('run_id')}",
    }


def build_collaboration_packet(
    conn: Any,
    root: Path,
    *,
    run_id: str,
    agent_name: str,
    selected_nodes: list[str],
    selected_paths: list[str],
    node: dict[str, Any] | None = None,
    event_limit: int = 80,
) -> dict[str, Any]:
    """Build a compact peer-awareness packet for a dispatched agent task."""

    selected: set[str] = {
        path for path in (_normal_path(path) for path in selected_paths) if path
    }
    if node:
        node_path = _normal_path(node.get("path"))
        if node_path:
            selected.add(node_path)
    snapshot = agent_activity.activity_snapshot(
        conn,
        event_limit=max(20, int(event_limit)),
        file_limit=8,
    )
    active_peer_runs = [
        _run_preview(run, selected)
        for run in snapshot.get("active_runs", [])
        if run.get("run_id") != run_id
    ]
    active_peer_runs.sort(
        key=lambda run: (
            0 if run.get("overlap_files") else 1,
            str(run.get("updated_at") or ""),
        )
    )

    recent_peer_events = [
        _event_preview(event)
        for event in snapshot.get("recent_events", [])
        if event.get("run_id") != run_id
    ][:25]
    overlapping_file_events = [
        _event_preview(event)
        for event in snapshot.get("recent_events", [])
        if event.get("run_id") != run_id
        and event.get("file_path")
        and event.get("file_path") in selected
    ][:25]
    active_claims = [
        {
            "claim_id": claim.get("claim_id"),
            "run_id": claim.get("run_id"),
            "agent_name": claim.get("agent_name") or "Agent",
            "file_path": claim.get("file_path"),
            "mode": claim.get("mode"),
            "reason": claim.get("reason") or "",
            "updated_at": claim.get("updated_at"),
            "expires_at": claim.get("expires_at"),
            "overlaps_selected": claim.get("file_path") in selected,
        }
        for claim in snapshot.get("active_claims", [])
        if claim.get("run_id") != run_id
    ][:40]
    overlapping_claims = [
        claim for claim in active_claims if claim.get("overlaps_selected")
    ][:20]

    # Transitively warn about claims on dependency files.
    dependency_warnings = agent_activity.dependency_claim_warnings(
        conn, list(selected), run_id=run_id, limit=20
    )

    # Predict where peer runs are likely heading next.
    trajectory_hints: list[dict[str, Any]] = []
    for peer_run in active_peer_runs[:8]:
        peer_id = peer_run.get("run_id")
        if not peer_id:
            continue
        predictions = agent_activity.predict_next_files(conn, peer_id, limit=3)
        if predictions:
            trajectory_hints.append(
                {
                    "run_id": peer_id,
                    "agent_name": peer_run.get("agent_name") or "Agent",
                    "current_files": peer_run.get("active_files", []),
                    "likely_next_files": predictions,
                }
            )

    global_path = global_events_jsonl_path(root)
    run_path = run_events_jsonl_path(root, run_id)
    return {
        "kind": "code_index_agent_collaboration",
        "schema_version": 1,
        "run_id": run_id,
        "agent_name": agent_name,
        "selected_nodes": list(selected_nodes)[:20],
        "selected_paths": sorted(selected),
        "mailbox": {
            "global_events_jsonl": _rel_path(root, global_path),
            "run_events_jsonl": _rel_path(root, run_path),
            "run_dir": _rel_path(root, run_path.parent),
            "checkin_event_contract": {
                "event_type": "decision",
                "message": "Short check-in describing current phase, files, and any coordination risk.",
                "payload": {
                    "checkin": True,
                    "phase": "planning|reading|editing|testing|blocked|done",
                    "files": sorted(selected),
                },
            },
        },
        "active_peer_runs": active_peer_runs[:8],
        "active_file_claims": active_claims,
        "overlapping_file_claims": overlapping_claims,
        "dependency_claim_warnings": dependency_warnings,
        "trajectory_hints": trajectory_hints,
        "recent_peer_events": recent_peer_events,
        "overlapping_file_events": overlapping_file_events,
        "guidance": [
            "Read active_peer_runs and overlapping_file_events before editing.",
            "Treat overlapping_file_claims as live coordination warnings before editing.",
            "Check dependency_claim_warnings: another agent may be editing a file your selection depends on.",
            "Review trajectory_hints to anticipate where peer agents are heading next.",
            "If another active run overlaps a selected file, emit a decision check-in before changing that file.",
            "Use the global JSONL feed for a lightweight view of other agents' recent work.",
        ],
    }
