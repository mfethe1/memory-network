"""HTTP handler factory for the live graph server."""

from __future__ import annotations

import argparse
import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.agent_collaboration import append_event_jsonl
from code_index.commands.graph_html import render_html
from code_index.commands.graph_notes import graph_notes_block, upsert_note
from code_index.commands.graph_server_dispatch import (
    _build_task_collaboration_packet,
    _build_task_context_packet,
    _build_task_graph_context,
    _cancel_local_agent_task,
    _dispatch_agent_task,
)
from code_index.commands.graph_server_state import (
    _agent_stream_payload,
    _build_debug_payload,
    _build_payload,
    _record_user_note_event,
    _state_signature,
)
from code_index.commands.graph_server_utils import (
    GRAPH_TOKEN_ENV_VAR,
    _json_bytes,
    _string_list,
    _validate_bearer,
    _validate_token,
)
from code_index.locking import writer_lock
from code_index.search import fts


def _task_request_from_payload(
    payload: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    provider = str(payload.get("provider") or "").strip().lower()
    agent_name = str(
        payload.get("agent_name")
        or (provider.title() if provider else "")
        or args.agent_name
        or "Codex"
    )
    selected_nodes = _string_list(payload.get("selected_nodes"))
    if payload.get("node_id"):
        selected_nodes.extend(
            node
            for node in _string_list(payload.get("node_id"))
            if node not in selected_nodes
        )
    selected_paths = _string_list(payload.get("selected_paths"))
    if payload.get("path"):
        selected_paths.extend(
            path
            for path in _string_list(payload.get("path"))
            if path not in selected_paths
        )
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    node_path = str(node.get("path") or "").strip()
    if node_path and node.get("kind") == "file" and node_path not in selected_paths:
        selected_paths.append(node_path)
    parent_run_id = str(payload.get("parent_run_id") or "").strip()
    run_context = (
        payload.get("run_context")
        if isinstance(payload.get("run_context"), dict)
        else None
    )
    context = {
        "selected_paths": selected_paths,
        "node": node,
        "source": "graph-server",
    }
    if provider:
        context["provider"] = provider
    if parent_run_id:
        context["parent_run_id"] = parent_run_id
    if run_context is not None:
        context["run_context"] = run_context
    return {
        "message": str(payload.get("message") or payload.get("prompt") or "").strip(),
        "provider": provider,
        "agent_name": agent_name,
        "selected_nodes": selected_nodes,
        "selected_paths": selected_paths,
        "node": node,
        "parent_run_id": parent_run_id,
        "run_context": run_context,
        "metadata": context,
        "preflight_confirmed": bool(payload.get("preflight_confirmed")),
    }


def _build_task_draft(
    config: cfg_mod.Config,
    request: dict[str, Any],
    *,
    callback_base_url: str,
) -> dict[str, Any]:
    try:
        context_budget = int(os.environ.get("CODE_INDEX_AGENT_CONTEXT_BUDGET") or 1600)
    except ValueError:
        context_budget = 1600
    task: dict[str, Any] = {
        "kind": "code_index_agent_task_draft",
        "root": str(config.root),
        "agent_name": request["agent_name"],
        "message": request["message"],
        "selected_nodes": request["selected_nodes"],
        "selected_paths": request["selected_paths"],
        "node": request["node"],
        "callback": {
            "agent_events_url": f"{callback_base_url}/api/agent-events",
        },
        "context_policy": {
            "initial_budget_tokens": context_budget,
            "runtime_retrieval": True,
            "retrieval_handles": {
                "selected_nodes": request["selected_nodes"],
                "selected_paths": request["selected_paths"],
            },
        },
    }
    if request["provider"]:
        task["provider"] = request["provider"]
    if request["parent_run_id"]:
        task["parent_run_id"] = request["parent_run_id"]
    if request["run_context"] is not None:
        task["run_context"] = request["run_context"]
    task["graph_context"] = _build_task_graph_context(
        config,
        agent_name=request["agent_name"],
        selected_nodes=request["selected_nodes"],
        selected_paths=request["selected_paths"],
        node=request["node"],
    )
    return task


def _claim_overlaps(
    claims: list[dict[str, Any]], selected_paths: list[str], *, parent_run_id: str
) -> list[dict[str, Any]]:
    selected = {path for path in selected_paths if path}
    overlaps: list[dict[str, Any]] = []
    for claim in claims:
        path = claim.get("file_path")
        if not path or path not in selected:
            continue
        item = dict(claim)
        item["same_parent_run"] = bool(parent_run_id and claim.get("run_id") == parent_run_id)
        overlaps.append(item)
    return overlaps


def _care_levels_from_graph_context(graph_context: dict[str, Any]) -> list[str]:
    levels: list[str] = []
    for node in graph_context.get("selected_nodes") or []:
        if isinstance(node, dict) and node.get("care_level"):
            level = str(node["care_level"])
            if level not in levels:
                levels.append(level)
    return levels


def _preflight_from_draft(
    *,
    request: dict[str, Any],
    draft: dict[str, Any],
    active_claims: list[dict[str, Any]],
) -> dict[str, Any]:
    graph_context = draft.get("graph_context") if isinstance(draft.get("graph_context"), dict) else {}
    care_levels = _care_levels_from_graph_context(graph_context)
    overlaps = _claim_overlaps(
        active_claims,
        list(request.get("selected_paths") or []),
        parent_run_id=str(request.get("parent_run_id") or ""),
    )
    foreign_overlaps = [
        claim for claim in overlaps if not claim.get("same_parent_run")
    ]
    warnings: list[dict[str, Any]] = []
    if not request.get("selected_paths"):
        warnings.append(
            {
                "kind": "no_selected_files",
                "severity": "warning",
                "message": "No concrete selected files were resolved for this task.",
            }
        )
    if "critical" in care_levels:
        warnings.append(
            {
                "kind": "critical_care",
                "severity": "warning",
                "message": "Selected context includes critical-care code.",
            }
        )
    elif "high" in care_levels:
        warnings.append(
            {
                "kind": "high_care",
                "severity": "info",
                "message": "Selected context includes high-care shared code.",
            }
        )
    if foreign_overlaps:
        warnings.append(
            {
                "kind": "overlapping_claims",
                "severity": "warning",
                "message": (
                    f"{len(foreign_overlaps)} active file claim(s) overlap this task."
                ),
                "claims": foreign_overlaps,
            }
        )
    requires_confirmation = any(
        warning["kind"] in {"critical_care", "overlapping_claims"}
        for warning in warnings
    )
    return {
        "status": "needs_confirmation" if requires_confirmation else "clear",
        "can_dispatch": True,
        "requires_confirmation": requires_confirmation,
        "warnings": warnings,
        "active_claims": active_claims,
        "overlapping_claims": overlaps,
        "care_levels": care_levels,
        "selected_path_count": len(request.get("selected_paths") or []),
        "selected_node_count": len(request.get("selected_nodes") or []),
    }


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _file_path_search(conn, query: str, *, limit: int) -> list[dict[str, Any]]:
    pattern = _like_pattern(query)
    rows = conn.execute(
        """
        SELECT file_path, language, parse_status
          FROM files
         WHERE deleted_at IS NULL
           AND file_path LIKE ? ESCAPE '\\'
         ORDER BY
           CASE WHEN file_path = ? THEN 0
                WHEN file_path LIKE ? ESCAPE '\\' THEN 1
                ELSE 2 END,
           file_path ASC
         LIMIT ?
        """,
        (pattern, query, f"{query}%", int(limit)),
    ).fetchall()
    return [
        {
            "kind": "file_path",
            "file_path": row["file_path"],
            "language": row["language"],
            "parse_status": row["parse_status"],
            "score": 0,
            "snippet": row["file_path"],
        }
        for row in rows
    ]


def _file_content_search(conn, query: str, *, limit: int) -> list[dict[str, Any]]:
    results = fts.search(conn, query, limit=limit)
    return [
        {
            "kind": "file_content",
            "file_path": row.get("file_path"),
            "language": row.get("language"),
            "chunk_type": row.get("chunk_type"),
            "symbol_name": row.get("symbol_name"),
            "symbol_path": row.get("symbol_path"),
            "signature": row.get("signature"),
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "score": row.get("score"),
            "snippet": row.get("snippet") or "",
        }
        for row in results
    ]


def _transcript_search(conn, query: str, *, limit: int) -> list[dict[str, Any]]:
    pattern = _like_pattern(query)
    event_rows = conn.execute(
        """
        SELECT e.event_pk, r.run_id, r.agent_name, r.status, r.prompt,
               e.timestamp, e.event_type, e.file_path, e.symbol_path,
               e.message
          FROM agent_events e
          JOIN agent_runs r ON r.run_pk = e.run_pk
         WHERE COALESCE(e.message, '') LIKE ? ESCAPE '\\'
            OR COALESCE(e.file_path, '') LIKE ? ESCAPE '\\'
            OR COALESCE(e.symbol_path, '') LIKE ? ESCAPE '\\'
            OR COALESCE(e.payload_json, '') LIKE ? ESCAPE '\\'
            OR COALESCE(r.prompt, '') LIKE ? ESCAPE '\\'
         ORDER BY e.event_pk DESC
         LIMIT ?
        """,
        (pattern, pattern, pattern, pattern, pattern, int(limit)),
    ).fetchall()
    return [
        {
            "kind": "transcript_event",
            "event_pk": row["event_pk"],
            "run_id": row["run_id"],
            "agent_name": row["agent_name"],
            "status": row["status"],
            "prompt": row["prompt"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "file_path": row["file_path"],
            "symbol_path": row["symbol_path"],
            "message": row["message"] or "",
            "snippet": row["message"] or row["prompt"] or "",
        }
        for row in event_rows
    ]


def _build_search_payload(
    config: cfg_mod.Config,
    *,
    query: str,
    scope: str,
    limit: int,
) -> dict[str, Any]:
    normalized_scope = scope if scope in {"all", "files", "transcripts"} else "all"
    safe_limit = max(1, min(50, int(limit or 12)))
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        file_results: list[dict[str, Any]] = []
        transcript_results: list[dict[str, Any]] = []
        if normalized_scope in {"all", "files"}:
            seen_paths: set[str] = set()
            for item in _file_path_search(conn, query, limit=safe_limit):
                path = str(item.get("file_path") or "")
                if path:
                    seen_paths.add(path)
                file_results.append(item)
            for item in _file_content_search(conn, query, limit=safe_limit):
                key = (
                    str(item.get("file_path") or ""),
                    str(item.get("start_line") or ""),
                    str(item.get("symbol_path") or ""),
                )
                if item.get("kind") == "file_content" and key[0] in seen_paths:
                    item["path_match_also"] = True
                file_results.append(item)
            file_results = file_results[:safe_limit]
        if normalized_scope in {"all", "transcripts"}:
            transcript_results = _transcript_search(conn, query, limit=safe_limit)
    finally:
        db_mod.close(conn)
    return {
        "ok": True,
        "kind": "code_index_graph_search",
        "query": query,
        "scope": normalized_scope,
        "limit": safe_limit,
        "files": file_results,
        "transcripts": transcript_results,
        "counts": {
            "files": len(file_results),
            "transcripts": len(transcript_results),
        },
    }


def _make_handler(config: cfg_mod.Config, args: argparse.Namespace):
    class GraphHandler(BaseHTTPRequestHandler):
        server_version = "code_index-graph/1"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            if getattr(self.server, "quiet", False):
                return
            super().log_message(format, *args)

        def _send_bytes(
            self, status: int, body: bytes, content_type: str = "application/json"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_unauthorized(self) -> None:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", "Bearer")
            body = _json_bytes({"error": "unauthorized"})
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, query: str = "") -> bool:
            token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
            if not token:
                return True
            if _validate_bearer(self.headers.get("Authorization"), token):
                return True
            params = parse_qs(query, keep_blank_values=False)
            for name in ("token", "graph_token", "access_token"):
                if any(_validate_token(value, token) for value in params.get(name, [])):
                    return True
            self._send_unauthorized()
            return False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            if not self._authorized(parsed.query):
                return
            if route in {"/", "/repo-graph.html"}:
                payload = _build_payload(config, args)
                self._send_bytes(
                    HTTPStatus.OK,
                    render_html(payload).encode("utf-8"),
                    "text/html",
                )
                return
            if route == "/repo-graph.json":
                self._send_bytes(HTTPStatus.OK, _json_bytes(_build_payload(config, args)))
                return
            if route == "/api/debug":
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(_build_debug_payload(config, args)),
                )
                return
            if route == "/api/file-claims":
                self._send_file_claims()
                return
            if route == "/api/search":
                self._send_search(parsed.query)
                return
            if route == "/notes.json":
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(graph_notes_block(config.root)),
                )
                return
            if route.startswith("/api/agent-runs/"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 3:
                    self._send_agent_run(parts[2])
                    return
            if route == "/events":
                self._stream_events()
                return
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found", "path": route}),
            )

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            if not self._authorized(parsed.query):
                return
            length = int(self.headers.get("Content-Length") or "0")
            try:
                body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(body or "{}")
            except json.JSONDecodeError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid JSON body"}),
                )
                return
            if route == "/api/notes":
                try:
                    saved = upsert_note(config.root, payload)
                    _record_user_note_event(config, payload, saved)
                except ValueError as exc:
                    self._send_bytes(
                        HTTPStatus.BAD_REQUEST,
                        _json_bytes({"error": str(exc)}),
                    )
                    return
                self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "note": saved}))
                return
            if route == "/api/agent-runs":
                self._start_agent_run(payload)
                return
            if route == "/api/agent-task-preflight":
                self._preflight_agent_task(payload)
                return
            if route.startswith("/api/agent-runs/") and route.endswith("/cancel"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 4:
                    self._cancel_agent_run(parts[2])
                    return
            if route.startswith("/api/agent-runs/") and route.endswith("/archive"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 4:
                    self._archive_agent_run(parts[2])
                    return
            if route == "/api/agent-events":
                self._record_agent_event(payload)
                return
            if route == "/api/file-claims":
                self._manage_file_claims(payload)
                return
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found", "path": route}),
            )

        def _send_file_claims(self) -> None:
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                claims = agent_activity.active_file_claims(conn, limit=200)
            finally:
                db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "kind": "code_index_agent_file_claims",
                        "active_claims": claims,
                        "count": len(claims),
                    }
                ),
            )

        def _send_search(self, query_string: str) -> None:
            params = parse_qs(query_string, keep_blank_values=False)
            query = str((params.get("q") or params.get("query") or [""])[0]).strip()
            if not query:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "q is required"}),
                )
                return
            scope = str((params.get("scope") or ["all"])[0]).strip().lower()
            try:
                limit = int((params.get("limit") or ["12"])[0])
            except ValueError:
                limit = 12
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    _build_search_payload(
                        config,
                        query=query,
                        scope=scope,
                        limit=limit,
                    )
                ),
            )

        def _manage_file_claims(self, payload: dict[str, Any]) -> None:
            run_id = str(payload.get("run_id") or "").strip()
            if not run_id:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "run_id is required"}),
                )
                return
            file_paths = _string_list(
                payload.get("file_paths") or payload.get("file_path") or payload.get("path")
            )
            action = str(payload.get("action") or "claim").strip().lower()
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    if action == "claim":
                        claims = agent_activity.claim_files(
                            conn,
                            run_id=run_id,
                            file_paths=file_paths,
                            mode=str(payload.get("mode") or "edit"),
                            reason=payload.get("reason"),
                            ttl_seconds=payload.get(
                                "ttl_seconds",
                                agent_activity.DEFAULT_CLAIM_TTL_SECONDS,
                            ),
                            metadata={"source": "graph-server-api"},
                        )
                    elif action == "release":
                        if file_paths:
                            claims = []
                            for path in file_paths:
                                claims.extend(
                                    agent_activity.release_claims(
                                        conn,
                                        run_id=run_id,
                                        file_path=path,
                                        mode=payload.get("mode"),
                                    )
                                )
                        else:
                            claims = agent_activity.release_claims(conn, run_id=run_id)
                    else:
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": f"unknown claim action: {action}"}),
                        )
                        return
                    active_claims = agent_activity.active_file_claims(conn, limit=200)
                except ValueError as exc:
                    self._send_bytes(
                        HTTPStatus.BAD_REQUEST,
                        _json_bytes({"error": str(exc)}),
                    )
                    return
                finally:
                    db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "action": action,
                        "claims": claims,
                        "active_claims": active_claims,
                    }
                ),
            )

        def _preflight_agent_task(self, payload: dict[str, Any]) -> None:
            request = _task_request_from_payload(payload, args)
            if not request["message"]:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "message is required"}),
                )
                return
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                active_claims = agent_activity.active_file_claims(conn, limit=200)
            finally:
                db_mod.close(conn)
            draft = _build_task_draft(
                config,
                request,
                callback_base_url=self._callback_base_url(),
            )
            preflight = _preflight_from_draft(
                request=request,
                draft=draft,
                active_claims=active_claims,
            )
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "kind": "code_index_graph_agent_task_preflight",
                        "draft": draft,
                        "preflight": preflight,
                        "dispatch_path": "/api/agent-runs",
                    }
                ),
            )

        def _send_agent_run(self, run_id: str) -> None:
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                transcript = agent_activity.run_transcript(conn, run_id)
            finally:
                db_mod.close(conn)
            if transcript is None:
                self._send_bytes(
                    HTTPStatus.NOT_FOUND,
                    _json_bytes({"error": f"unknown run_id: {run_id}"}),
                )
                return
            self._send_bytes(HTTPStatus.OK, _json_bytes(transcript))

        def _cancel_agent_run(self, run_id: str) -> None:
            local_cancel_requested = _cancel_local_agent_task(run_id)
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    run = agent_activity.get_run(conn, run_id)
                    if run is None:
                        self._send_bytes(
                            HTTPStatus.NOT_FOUND,
                            _json_bytes({"error": f"unknown run_id: {run_id}"}),
                        )
                        return
                    status = str(run.get("status") or "").lower()
                    if status in agent_activity.TERMINAL_STATUSES:
                        self._send_bytes(
                            HTTPStatus.CONFLICT,
                            _json_bytes(
                                {
                                    "error": "run already terminal",
                                    "run": run,
                                }
                            ),
                        )
                        return
                    event = agent_activity.record_event(
                        conn,
                        run_id=run_id,
                        event_type="status",
                        message=(
                            "Run cancelled from graph UI; local command adapter signalled."
                            if local_cancel_requested
                            else "Run cancelled from graph UI."
                        ),
                        payload={
                            "status": "cancelled",
                            "local_cancel_requested": local_cancel_requested,
                        },
                    )
                    updated = agent_activity.get_run(conn, run_id)
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": updated,
                        "event": event,
                        "local_cancel_requested": local_cancel_requested,
                    }
                ),
            )

        def _archive_agent_run(self, run_id: str) -> None:
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    run = agent_activity.get_run(conn, run_id)
                    if run is None:
                        self._send_bytes(
                            HTTPStatus.NOT_FOUND,
                            _json_bytes({"error": f"unknown run_id: {run_id}"}),
                        )
                        return
                    event = agent_activity.record_event(
                        conn,
                        run_id=run_id,
                        event_type="status",
                        message="Run archived from graph UI.",
                        payload={"archived": True},
                    )
                    updated = agent_activity.archive_run(conn, run_id=run_id)
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes({"ok": True, "run": updated, "event": event}),
            )

        def _callback_base_url(self) -> str:
            host = self.headers.get("Host")
            if not host:
                address = self.server.server_address  # type: ignore[attr-defined]
                host = f"{address[0]}:{address[1]}"
            return f"http://{host}"

        def _start_agent_run(self, payload: dict[str, Any]) -> None:
            request = _task_request_from_payload(payload, args)
            if not request["message"]:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "message is required"}),
                )
                return
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    run = agent_activity.start_run(
                        conn,
                        agent_name=request["agent_name"],
                        prompt=request["message"],
                        selected_nodes=request["selected_nodes"],
                        metadata=request["metadata"],
                        status="queued",
                    )
                    event = agent_activity.record_event(
                        conn,
                        run_id=run["run_id"],
                        event_type="task",
                        file_path=(
                            request["selected_paths"][0]
                            if request["selected_paths"]
                            else request["node"].get("path")
                        ),
                        message=request["message"],
                        payload={
                            "status": "queued",
                            "provider": request["provider"] or None,
                            "selected_nodes": request["selected_nodes"],
                            "selected_paths": request["selected_paths"],
                            "node": request["node"],
                            "preflight_confirmed": request["preflight_confirmed"],
                        },
                    )
                    if request["selected_paths"]:
                        agent_activity.claim_files(
                            conn,
                            run_id=run["run_id"],
                            file_paths=request["selected_paths"],
                            mode="review",
                            reason="Graph task selected file.",
                            metadata={"source": "graph-task"},
                        )
                    run = agent_activity.get_run(conn, run["run_id"])
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            task = _build_task_draft(
                config,
                request,
                callback_base_url=self._callback_base_url(),
            )
            task["kind"] = "code_index_agent_task"
            task["run_id"] = run["run_id"] if run else event["run_id"]
            if isinstance(payload.get("preflight"), dict):
                task["preflight"] = payload["preflight"]
            task["context_packet"] = _build_task_context_packet(
                config,
                message=request["message"],
                selected_nodes=request["selected_nodes"],
                selected_paths=request["selected_paths"],
            )
            graph_context = task["graph_context"]
            if isinstance(task.get("context_packet"), dict):
                task["context_packet"]["graph_context"] = graph_context
            collaboration = _build_task_collaboration_packet(
                config,
                run_id=str(task["run_id"]),
                agent_name=request["agent_name"],
                selected_nodes=request["selected_nodes"],
                selected_paths=request["selected_paths"],
                node=request["node"],
            )
            task["collaboration"] = collaboration
            if isinstance(task.get("context_packet"), dict):
                task["context_packet"]["collaboration"] = collaboration
            dispatch = _dispatch_agent_task(config, task)
            if dispatch.get("configured") and dispatch.get("status") == "sent":
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        dispatch_event = agent_activity.record_event(
                            conn,
                            run_id=task["run_id"],
                            event_type="status",
                            message=(
                                "Task dispatched to local command adapter."
                                if dispatch.get("transport") == "local-command"
                                else "Task dispatched to agent webhook."
                            ),
                            payload={"status": "working", "dispatch": dispatch},
                        )
                        run = agent_activity.get_run(conn, task["run_id"])
                    finally:
                        db_mod.close(conn)
                append_event_jsonl(config.root, dispatch_event)
            elif (
                dispatch.get("configured")
                and dispatch.get("transport") == "local-command"
            ):
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        run = agent_activity.get_run(conn, task["run_id"])
                    finally:
                        db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": run,
                        "event": event,
                        "dispatch": dispatch,
                        "task": task,
                    }
                ),
            )

        def _record_agent_event(self, payload: dict[str, Any]) -> None:
            event_type = str(payload.get("event_type") or payload.get("type") or "")
            if not event_type:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "event_type is required"}),
                )
                return
            agent_name = str(payload.get("agent_name") or "Agent")
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    event_payload = payload.get("payload") or {}
                    if not isinstance(event_payload, dict):
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": "payload must be a JSON object"}),
                        )
                        return
                    run_id = payload.get("run_id") or event_payload.get("run_id")
                    run = (
                        agent_activity.get_run(conn, str(run_id))
                        if run_id
                        else agent_activity.latest_active_run(conn, agent_name=agent_name)
                    )
                    if run is None:
                        prompt = str(payload.get("prompt") or "").strip()
                        if run_id:
                            self._send_bytes(
                                HTTPStatus.NOT_FOUND,
                                _json_bytes({"error": f"unknown run_id: {run_id}"}),
                            )
                            return
                        if not prompt:
                            self._send_bytes(
                                HTTPStatus.OK,
                                _json_bytes(
                                    {
                                        "ok": True,
                                        "ignored": True,
                                        "reason": (
                                            "run_id is required when no active run can "
                                            "receive this event"
                                        ),
                                        "run": None,
                                        "event": None,
                                    }
                                ),
                            )
                            return
                        run = agent_activity.start_run(
                            conn,
                            agent_name=agent_name,
                            prompt=prompt,
                            metadata={"source": "graph-server"},
                        )
                    if payload.get("status"):
                        event_payload["status"] = str(payload.get("status"))
                    event = agent_activity.record_event(
                        conn,
                        run_id=run["run_id"],
                        event_type=event_type,
                        file_path=payload.get("file_path") or payload.get("file"),
                        symbol_path=payload.get("symbol_path"),
                        message=payload.get("message"),
                        payload=event_payload,
                    )
                    updated_run = agent_activity.get_run(conn, event["run_id"])
                    suggestions_event = None
                    suggestions = None
                    if str((updated_run or {}).get("status") or "").lower() in {
                        "completed",
                        "failed",
                    }:
                        suggestions_event = agent_activity.record_run_suggestions(
                            conn, run_id=event["run_id"]
                        )
                        suggestions = agent_activity.build_run_suggestions(
                            conn, event["run_id"]
                        )
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            append_event_jsonl(config.root, suggestions_event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": updated_run,
                        "event": event,
                        "suggestions_event": suggestions_event,
                        "suggestions": suggestions,
                    }
                ),
            )

        def _stream_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_event_pk: int | None = None
            last_notes_mtime: int | None = None
            interval = max(0.25, float(getattr(args, "event_interval", 1.0) or 1.0))
            while True:
                try:
                    signature = _state_signature(config)
                    sent = False
                    if signature["event_pk"] != last_event_pk:
                        last_event_pk = signature["event_pk"]
                        data = json.dumps(_agent_stream_payload(config))
                        self.wfile.write(f"event: agent\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                        sent = True
                    if signature["notes_mtime"] != last_notes_mtime:
                        last_notes_mtime = signature["notes_mtime"]
                        data = json.dumps({"type": "graph", **signature})
                        self.wfile.write(f"event: graph\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                        sent = True
                    if not sent:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    time.sleep(interval)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break

    return GraphHandler
