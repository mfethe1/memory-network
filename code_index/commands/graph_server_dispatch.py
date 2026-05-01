"""Agent dispatch backends for the live graph server."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from typing import Any
from urllib import error as urllib_error
from urllib.request import Request, urlopen

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_lifecycle
from code_index.agent_collaboration import (
    append_event_jsonl,
    build_collaboration_packet,
)
from code_index.commands.graph_model import build_graph
from code_index.locking import writer_lock


_LOCAL_AGENT_CANCEL_EVENTS: dict[str, threading.Event] = {}
_LOCAL_AGENT_PROCESS_PIDS: dict[str, int] = {}
_LOCAL_AGENT_CANCEL_LOCK = threading.Lock()
GRAPH_CONTEXT_WHY_INCLUDED = (
    "selected_node",
    "selected_path",
    "supplied_node",
    "adjacent_relation",
    "metric_neighbor",
)


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
            _LOCAL_AGENT_PROCESS_PIDS.pop(run_id, None)


def _cancel_local_agent_task(run_id: str) -> bool:
    with _LOCAL_AGENT_CANCEL_LOCK:
        cancel_event = _LOCAL_AGENT_CANCEL_EVENTS.get(run_id)
        pid = _LOCAL_AGENT_PROCESS_PIDS.get(run_id)
    if cancel_event is None:
        return False
    cancel_event.set()
    if pid is None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with _LOCAL_AGENT_CANCEL_LOCK:
                pid = _LOCAL_AGENT_PROCESS_PIDS.get(run_id)
            if pid is not None:
                break
            time.sleep(0.05)
    if pid is not None:
        _terminate_process_tree(pid)
    return True


def _terminate_process_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            import ctypes

            process_terminate = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(
                process_terminate,
                False,
                int(pid),
            )
            if handle:
                try:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


def _run_local_agent_task(
    config: cfg_mod.Config,
    task: dict[str, Any],
    command: str,
    provider: str,
    cancel_event: threading.Event,
) -> None:
    from code_index.commands import agent_adapter_cmd

    process_id: str | None = None
    process_started = False

    def register_started_process(pid: int, command_label: str) -> None:
        nonlocal process_id, process_started
        run_id = str(task.get("run_id") or "")
        if not run_id:
            return
        with _LOCAL_AGENT_CANCEL_LOCK:
            if run_id in _LOCAL_AGENT_CANCEL_EVENTS:
                _LOCAL_AGENT_PROCESS_PIDS[run_id] = int(pid)
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                process = run_lifecycle.register_process(
                    conn,
                    run_id=run_id,
                    transport="local-command",
                    provider=provider or None,
                    pid=pid,
                    command_label=command_label,
                    metadata={"adapter": "command"},
                )
                process_id = str(process["process_id"])
                process_started = True
            finally:
                db_mod.close(conn)

    def finish_registered_process(status: str, exit_code: int | None) -> None:
        if not process_id:
            return
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                run_lifecycle.finish_process(
                    conn,
                    process_id=process_id,
                    status=status,
                    exit_code=exit_code,
                )
            finally:
                db_mod.close(conn)

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
            process_started_callback=register_started_process,
        )
        result_status = str(result.get("status") or "").strip().lower()
        process_status = (
            result_status
            if result_status in run_lifecycle.TERMINAL_PROCESS_STATUSES
            else ("completed" if exit_code == 0 else "failed")
        )
        process_exit_code = result.get("process_exit_code")
        finish_registered_process(
            process_status,
            int(process_exit_code) if process_exit_code is not None else exit_code,
        )
        if exit_code == 2:
            _record_local_adapter_failure(
                config, task, RuntimeError(str(result.get("error") or "adapter error"))
            )
    except BaseException as exc:  # pragma: no cover - defensive background guard.
        if process_started:
            finish_registered_process(
                "cancelled" if cancel_event.is_set() else "failed",
                None,
            )
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


def _json_byte_cost(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _with_byte_cost(payload: dict[str, Any]) -> dict[str, Any]:
    item = dict(payload)
    item["byte_cost"] = 0
    while True:
        byte_cost = _json_byte_cost(item)
        if item["byte_cost"] == byte_cost:
            return item
        item["byte_cost"] = byte_cost


def _node_risk(node: dict[str, Any]) -> dict[str, Any]:
    importance = node.get("importance") or {}
    return {
        "level": node.get("care_level") or "unknown",
        "score": importance.get("score"),
    }


def _compact_graph_node(
    node: dict[str, Any],
    *,
    layer: str,
    distance: int,
    relation_path: list[dict[str, Any]],
    why_included: str,
) -> dict[str, Any]:
    metrics = node.get("metrics") or {}
    importance = node.get("importance") or {}
    stable_id = node.get("id")
    item = {
        "id": node.get("id"),
        "stable_id": stable_id,
        "path": node.get("path"),
        "kind": node.get("kind"),
        "language": node.get("language"),
        "role": node.get("role"),
        "layer": layer,
        "distance": distance,
        "relation_path": relation_path,
        "risk": _node_risk(node),
        "why_included": (
            why_included
            if why_included in GRAPH_CONTEXT_WHY_INCLUDED
            else "metric_neighbor"
        ),
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
    return _with_byte_cost(item)


def _edge_relation_path(edge: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "edge_id": edge.get("id"),
            "source": edge.get("source"),
            "target": edge.get("target"),
            "kind": edge.get("kind"),
        }
    ]


def _metric_relation_path(source_id: str, target_id: str) -> list[dict[str, Any]]:
    return [
        {
            "source": source_id,
            "target": target_id,
            "kind": "metric_neighbor",
        }
    ]


def _append_budgeted_node(
    out: list[dict[str, Any]],
    item: dict[str, Any],
    *,
    max_nodes: int,
    max_bytes: int,
    budget: dict[str, Any],
) -> bool:
    if len(out) >= max_nodes:
        budget["truncated_nodes"] = True
        return False
    next_bytes = int(budget["used_bytes"]) + int(item.get("byte_cost") or 0)
    if next_bytes > max_bytes:
        budget["truncated_bytes"] = True
        return False
    out.append(item)
    budget["used_nodes"] = len(out)
    budget["used_bytes"] = next_bytes
    return True


def _build_task_graph_context(
    config: cfg_mod.Config,
    *,
    agent_name: str,
    selected_nodes: list[str],
    selected_paths: list[str],
    node: dict[str, Any] | None = None,
    max_nodes: int = 24,
    max_bytes: int = 24_000,
) -> dict[str, Any]:
    """Build a compact graph packet for a dispatched coding-agent task."""

    try:
        max_nodes = max(0, int(max_nodes))
        max_bytes = max(0, int(max_bytes))
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
        relation_path_by_related_id: dict[str, list[dict[str, Any]]] = {}
        for edge in payload.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source in selected_ids or target in selected_ids:
                compact_edge = {
                    "id": edge.get("id"),
                    "source": source,
                    "target": target,
                    "kind": edge.get("kind"),
                    "label": edge.get("label"),
                    "weight": edge.get("weight"),
                }
                adjacent_edges.append(compact_edge)
                if source and source not in selected_ids:
                    related_ids.add(source)
                    relation_path_by_related_id.setdefault(
                        source, _edge_relation_path(compact_edge)
                    )
                if target and target not in selected_ids:
                    related_ids.add(target)
                    relation_path_by_related_id.setdefault(
                        target, _edge_relation_path(compact_edge)
                    )

        for selected_node in selected_graph_nodes:
            metrics = selected_node.get("metrics") or {}
            selected_id = str(selected_node.get("id") or "")
            for path in list(metrics.get("incoming_files") or []) + list(
                metrics.get("outgoing_files") or []
            ):
                related = node_by_path.get(_normal_task_path(path))
                if related and related.get("id") not in selected_ids:
                    related_id = str(related.get("id"))
                    related_ids.add(related_id)
                    relation_path_by_related_id.setdefault(
                        related_id, _metric_relation_path(selected_id, related_id)
                    )

        selected_why_by_id: dict[str, str] = {}
        for item in selected_graph_nodes:
            node_id = str(item.get("id") or "")
            path = _normal_task_path(item.get("path"))
            if node_id in selected_node_ids:
                selected_why_by_id[node_id] = "selected_node"
            elif path and path in selected_path_set:
                selected_why_by_id[node_id] = "selected_path"
            else:
                selected_why_by_id[node_id] = "supplied_node"

        budget = {
            "max_nodes": max_nodes,
            "max_bytes": max_bytes,
            "max_edges": 40,
            "used_nodes": 0,
            "used_edges": 0,
            "used_bytes": 0,
            "truncated_nodes": False,
            "truncated_bytes": False,
            "truncated_edges": False,
        }
        selected_compact: list[dict[str, Any]] = []
        all_compact: list[dict[str, Any]] = []
        for item in selected_graph_nodes:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or "")
            compact = _compact_graph_node(
                item,
                layer="selected",
                distance=0,
                relation_path=[],
                why_included=selected_why_by_id.get(node_id, "supplied_node"),
            )
            if _append_budgeted_node(
                all_compact,
                compact,
                max_nodes=max_nodes,
                max_bytes=max_bytes,
                budget=budget,
            ):
                selected_compact.append(compact)

        related_compact: list[dict[str, Any]] = []
        for node_id in sorted(related_ids):
            related = node_by_id.get(node_id)
            if not related:
                continue
            why = (
                "adjacent_relation"
                if node_id in relation_path_by_related_id
                and any(
                    step.get("kind") != "metric_neighbor"
                    for step in relation_path_by_related_id[node_id]
                )
                else "metric_neighbor"
            )
            compact = _compact_graph_node(
                related,
                layer="related",
                distance=1,
                relation_path=relation_path_by_related_id.get(node_id, []),
                why_included=why,
            )
            if _append_budgeted_node(
                all_compact,
                compact,
                max_nodes=max_nodes,
                max_bytes=max_bytes,
                budget=budget,
            ):
                related_compact.append(compact)

        included_ids = {str(item.get("id")) for item in all_compact if item.get("id")}
        budgeted_edges: list[dict[str, Any]] = []
        for edge in adjacent_edges:
            if len(budgeted_edges) >= int(budget["max_edges"]):
                budget["truncated_edges"] = True
                break
            if edge["source"] not in included_ids or edge["target"] not in included_ids:
                continue
            budgeted_edge = _with_byte_cost(edge)
            edge_cost = int(budgeted_edge.get("byte_cost") or 0)
            if int(budget["used_bytes"]) + edge_cost > max_bytes:
                budget["truncated_bytes"] = True
                continue
            budget["used_bytes"] = int(budget["used_bytes"]) + edge_cost
            budgeted_edges.append(budgeted_edge)
            budget["used_edges"] = len(budgeted_edges)

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
            "budget": budget,
            "why_included_values": list(GRAPH_CONTEXT_WHY_INCLUDED),
            "layers": [
                {"layer": "selected", "distance": 0, "nodes": selected_compact},
                {"layer": "related", "distance": 1, "nodes": related_compact},
            ],
            "nodes": all_compact,
            "selected_nodes": selected_compact,
            "related_nodes": related_compact,
            "edges": budgeted_edges,
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
