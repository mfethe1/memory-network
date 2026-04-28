"""Agent dispatch backends for the live graph server."""

from __future__ import annotations

import json
import os
import threading
from typing import Any
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.agent_collaboration import (
    append_event_jsonl,
    build_collaboration_packet,
)
from code_index.commands.graph_model import build_graph
from code_index.locking import writer_lock


_LOCAL_AGENT_CANCEL_EVENTS: dict[str, threading.Event] = {}
_LOCAL_AGENT_CANCEL_LOCK = threading.Lock()


def _env_float(name: str) -> float | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _record_local_adapter_failure(
    config: cfg_mod.Config, task: dict[str, Any], exc: BaseException
) -> None:
    run_id = task.get("run_id")
    if not run_id:
        return
    with writer_lock(config):
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            event = agent_activity.record_event(
                conn,
                run_id=str(run_id),
                event_type="status",
                message=f"Local command adapter failed: {exc}",
                payload={
                    "status": "failed",
                    "adapter": "command",
                    "error": str(exc),
                },
            )
            append_event_jsonl(config.root, event)
        finally:
            db_mod.close(conn)


def _register_local_agent_task(run_id: str) -> threading.Event:
    cancel_event = threading.Event()
    with _LOCAL_AGENT_CANCEL_LOCK:
        _LOCAL_AGENT_CANCEL_EVENTS[run_id] = cancel_event
    return cancel_event


def _unregister_local_agent_task(run_id: str, cancel_event: threading.Event) -> None:
    with _LOCAL_AGENT_CANCEL_LOCK:
        if _LOCAL_AGENT_CANCEL_EVENTS.get(run_id) is cancel_event:
            _LOCAL_AGENT_CANCEL_EVENTS.pop(run_id, None)


def _cancel_local_agent_task(run_id: str) -> bool:
    with _LOCAL_AGENT_CANCEL_LOCK:
        cancel_event = _LOCAL_AGENT_CANCEL_EVENTS.get(run_id)
    if cancel_event is None:
        return False
    cancel_event.set()
    return True


def _run_local_agent_task(
    config: cfg_mod.Config,
    task: dict[str, Any],
    command: str,
    provider: str,
    cancel_event: threading.Event,
) -> None:
    from code_index.commands import agent_adapter_cmd

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
        if exit_code == 2:
            _record_local_adapter_failure(
                config, task, RuntimeError(str(result.get("error") or "adapter error"))
            )
    except BaseException as exc:  # pragma: no cover - defensive background guard.
        _record_local_adapter_failure(config, task, exc)
    finally:
        run_id = str(task.get("run_id") or "")
        if run_id:
            _unregister_local_agent_task(run_id, cancel_event)


def _dispatch_agent_task(
    config: cfg_mod.Config, task: dict[str, Any]
) -> dict[str, Any]:
    webhook_url = os.environ.get("CODE_INDEX_AGENT_WEBHOOK_URL")
    if webhook_url:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("CODE_INDEX_AGENT_WEBHOOK_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(task).encode("utf-8")
        request = Request(webhook_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=5) as response:  # noqa: S310 - local opt-in webhook.
                response.read()
                return {
                    "configured": True,
                    "status": "sent",
                    "transport": "webhook",
                    "http_status": response.status,
                }
        except urllib_error.HTTPError as exc:
            return {
                "configured": True,
                "status": "failed",
                "transport": "webhook",
                "http_status": exc.code,
                "error": str(exc),
            }
        except OSError as exc:
            return {
                "configured": True,
                "status": "failed",
                "transport": "webhook",
                "error": str(exc),
            }

    from code_index.commands.agent_adapter_cmd import resolve_agent_command

    try:
        provider_hint = str(task.get("provider") or "").strip().lower() or None
        command, provider = resolve_agent_command(provider=provider_hint)
    except ValueError as exc:
        return {
            "configured": True,
            "status": "failed",
            "transport": "local-command",
            "error": str(exc),
        }
    if command:
        cancel_event = _register_local_agent_task(str(task["run_id"]))
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                event = agent_activity.record_event(
                    conn,
                    run_id=str(task["run_id"]),
                    event_type="status",
                    message="Task dispatched to local command adapter.",
                    payload={
                        "status": "working",
                        "dispatch": {
                            "configured": True,
                            "status": "started",
                            "transport": "local-command",
                            "adapter": "command",
                            "provider": provider,
                        },
                    },
                )
                append_event_jsonl(config.root, event)
            finally:
                db_mod.close(conn)
        thread = threading.Thread(
            target=_run_local_agent_task,
            args=(config, task, command, provider, cancel_event),
            daemon=True,
            name=f"code-index-agent-{task.get('run_id') or 'task'}",
        )
        thread.start()
        return {
            "configured": True,
            "status": "started",
            "transport": "local-command",
            "adapter": "command",
            "provider": provider,
        }
    return {"configured": False, "status": "not_configured"}


def _normal_task_path(path: Any) -> str | None:
    if not path:
        return None
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text or None


def _compact_graph_node(node: dict[str, Any]) -> dict[str, Any]:
    metrics = node.get("metrics") or {}
    importance = node.get("importance") or {}
    return {
        "id": node.get("id"),
        "path": node.get("path"),
        "kind": node.get("kind"),
        "language": node.get("language"),
        "role": node.get("role"),
        "care_level": node.get("care_level"),
        "active_work": bool(node.get("active_work")),
        "importance": {
            "score": importance.get("score"),
            "rank": importance.get("rank"),
            "reasons": list(importance.get("reasons") or [])[:5],
        },
        "summary": node.get("summary"),
        "incoming_files": list(metrics.get("incoming_files") or [])[:12],
        "outgoing_files": list(metrics.get("outgoing_files") or [])[:12],
        "recent_edits": list(node.get("recent_edits") or [])[:6],
    }


def _build_task_graph_context(
    config: cfg_mod.Config,
    *,
    agent_name: str,
    selected_nodes: list[str],
    selected_paths: list[str],
    node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact graph packet for a dispatched coding-agent task."""

    try:
        selected_node_ids = {str(item) for item in selected_nodes if item}
        selected_path_set = {
            path for path in (_normal_task_path(item) for item in selected_paths) if path
        }
        supplied_node = node or {}
        supplied_node_id = supplied_node.get("id")
        supplied_node_path = _normal_task_path(supplied_node.get("path"))
        if supplied_node_id:
            selected_node_ids.add(str(supplied_node_id))
        if supplied_node_path:
            selected_path_set.add(supplied_node_path)

        conn = db_mod.connect(config.db_path)
        try:
            db_mod.ensure_schema(conn, config)
            payload = build_graph(
                conn,
                config.root,
                include_code=False,
                max_code_bytes=0,
                focus_paths=sorted(selected_path_set),
                agent_name=agent_name,
            )
        finally:
            db_mod.close(conn)

        graph_nodes = payload.get("nodes") or []
        node_by_id = {
            str(item.get("id")): item
            for item in graph_nodes
            if isinstance(item, dict) and item.get("id")
        }
        node_by_path = {
            _normal_task_path(item.get("path")): item
            for item in graph_nodes
            if isinstance(item, dict) and item.get("path")
        }
        selected_graph_nodes = [
            node_by_id[node_id]
            for node_id in sorted(selected_node_ids)
            if node_id in node_by_id
        ]
        for path in sorted(selected_path_set):
            graph_node = node_by_path.get(path)
            if graph_node and graph_node not in selected_graph_nodes:
                selected_graph_nodes.append(graph_node)
        if not selected_graph_nodes and supplied_node:
            selected_graph_nodes.append(supplied_node)

        selected_ids = {
            str(item.get("id"))
            for item in selected_graph_nodes
            if isinstance(item, dict) and item.get("id")
        }
        related_ids: set[str] = set()
        adjacent_edges: list[dict[str, Any]] = []
        for edge in payload.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source in selected_ids or target in selected_ids:
                adjacent_edges.append(
                    {
                        "source": source,
                        "target": target,
                        "kind": edge.get("kind"),
                        "label": edge.get("label"),
                        "weight": edge.get("weight"),
                    }
                )
                if source and source not in selected_ids:
                    related_ids.add(source)
                if target and target not in selected_ids:
                    related_ids.add(target)

        for selected_node in selected_graph_nodes:
            metrics = selected_node.get("metrics") or {}
            for path in list(metrics.get("incoming_files") or []) + list(
                metrics.get("outgoing_files") or []
            ):
                related = node_by_path.get(_normal_task_path(path))
                if related and related.get("id") not in selected_ids:
                    related_ids.add(str(related.get("id")))

        summary = payload.get("summary") or {}
        return {
            "kind": "code_index_graph_context",
            "schema_version": payload.get("schema_version"),
            "generated_at": payload.get("generated_at"),
            "repo_graph_url": "/repo-graph.json",
            "summary": {
                "file_count": summary.get("file_count"),
                "directory_count": summary.get("directory_count"),
                "relation_edge_count": summary.get("relation_edge_count"),
                "top_files": list(summary.get("top_files") or [])[:8],
                "recent_files": list(summary.get("recent_files") or [])[:8],
            },
            "selected_nodes": [
                _compact_graph_node(item)
                for item in selected_graph_nodes
                if isinstance(item, dict)
            ],
            "related_nodes": [
                _compact_graph_node(node_by_id[node_id])
                for node_id in sorted(related_ids)
                if node_id in node_by_id
            ][:16],
            "edges": adjacent_edges[:40],
        }
    except Exception as exc:  # pragma: no cover - graph context is best-effort.
        return {
            "kind": "code_index_graph_context_error",
            "error": str(exc),
        }


def _build_task_context_packet(
    config: cfg_mod.Config,
    *,
    message: str,
    selected_nodes: list[str],
    selected_paths: list[str],
) -> dict[str, Any] | None:
    try:
        from code_index.commands.context_cmd import build_context_packet

        return build_context_packet(
            config,
            message,
            budget_tokens=_env_int("CODE_INDEX_AGENT_CONTEXT_BUDGET", 1600),
            selected_nodes=selected_nodes,
            selected_paths=selected_paths,
            limit=8,
        )
    except Exception as exc:  # pragma: no cover - context is best-effort.
        return {
            "kind": "code_index_context_packet_error",
            "task": message,
            "error": str(exc),
        }


def _build_task_collaboration_packet(
    config: cfg_mod.Config,
    *,
    run_id: str,
    agent_name: str,
    selected_nodes: list[str],
    selected_paths: list[str],
    node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.ensure_schema(conn, config)
            return build_collaboration_packet(
                conn,
                config.root,
                run_id=run_id,
                agent_name=agent_name,
                selected_nodes=selected_nodes,
                selected_paths=selected_paths,
                node=node,
            )
        finally:
            db_mod.close(conn)
    except Exception as exc:  # pragma: no cover - collaboration is best-effort.
        return {
            "kind": "code_index_agent_collaboration_error",
            "run_id": run_id,
            "error": str(exc),
        }
