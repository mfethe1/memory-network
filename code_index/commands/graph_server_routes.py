"""Route handler implementations for the live graph server.

This module extracts the heavy business logic from graph_server_http.py
so the HTTP handler file stays focused on HTTP plumbing and routing.
The factory function returns a mixin class that captures config/args/perf
state through closure variables, keeping method bodies unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs

from code_index import agent_activity
from code_index import agent_providers
from code_index import agent_swarm
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import lease_manager
from code_index import run_orchestrator
from code_index import scopes
from code_index.agent_collaboration import append_event_jsonl
from code_index.commands.graph_html import render_html
from code_index.commands.graph_notes import graph_notes_block, upsert_note
from code_index.commands.graph_server_dispatch import (
    _build_task_collaboration_packet,
    _build_task_context_packet,
    _cancel_local_agent_task,
    _dispatch_agent_task,
)
from code_index.commands.graph_server_perf import (
    _inc_counter,
    _observe_latency,
    _observe_retrieval_budget,
    _perf_snapshot,
    _perf_tick_payload,
)
from code_index.commands.graph_server_preflight import (
    _build_preflight_record,
    _build_task_draft,
    _consume_preflight,
    _preflight_from_draft,
    _store_preflight,
    _task_request_from_payload,
)
from code_index.commands.graph_server_search import _build_search_payload
from code_index.commands.graph_server_state import (
    GraphServerProjectState,
    _agent_stream_payload,
    _build_debug_payload,
    _build_payload,
    _dynamic_edge_signature,
    _record_user_note_event,
    _reconcile_agent_runs,
    _state_signature,
)
from code_index.commands.graph_server_utils import _json_bytes, _now_iso, _string_list
from code_index.locking import writer_lock


def _make_routes_class(
    config: cfg_mod.Config,
    args: argparse.Namespace,
    preflight_secret: str,
    perf_state: dict[str, Any],
    project_state: GraphServerProjectState,
):
    """Return a mixin class containing all heavy route handlers.

    The mixin is meant to be combined with BaseHTTPRequestHandler so the
    methods can use ``self._send_bytes``, ``self.wfile``, etc.
    """

    class RoutesBase:

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

        def _send_agent_board(self) -> None:
            _reconcile_agent_runs(config)
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                activity = run_orchestrator.snapshot(conn, limit=80)
                board = activity.get("kanban") or agent_activity.kanban_board(
                    conn, limit=25
                )
                board["orchestrator"] = activity.get("orchestrator")
            finally:
                db_mod.close(conn)
            self._send_bytes(HTTPStatus.OK, _json_bytes(board))

        def _send_search(self, query_string: str) -> None:
            started = time.perf_counter()
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
            limit = max(1, min(100, limit))
            path_scope = scopes.resolve_scope_from_args(config.root, args)
            result = _build_search_payload(
                config,
                query=query,
                scope=scope,
                limit=limit,
                path_scope=path_scope,
            )
            _observe_retrieval_budget(perf_state, result)
            _observe_latency(
                perf_state,
                "search_latency_ms",
                (time.perf_counter() - started) * 1000,
                str(result.get("scope") or scope or "all"),
            )
            self._send_bytes(HTTPStatus.OK, _json_bytes(result))

        def _send_symbols(self, query_string: str) -> None:
            params = parse_qs(query_string, keep_blank_values=False)
            query = str((params.get("q") or params.get("query") or [""])[0]).strip()
            if not query:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "q parameter is required"}),
                )
                return
            kind = str((params.get("kind") or [""])[0]).strip() or None
            try:
                limit = int((params.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(100, limit))
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                from code_index.search import symbol_search

                raw_results = symbol_search.lookup(
                    conn,
                    query,
                    kind=kind,
                    limit=limit,
                )
            finally:
                db_mod.close(conn)
            results = [
                {
                    "kind": "symbol_definition",
                    "symbol_uid": row.get("symbol_uid", ""),
                    "canonical_name": row.get("canonical_name", ""),
                    "display_name": row.get("display_name", ""),
                    "symbol_kind": row.get("kind", ""),
                    "def_file": row.get("def_file", ""),
                    "def_line": row.get("def_line"),
                    "signature": row.get("signature_norm", ""),
                    "confidence": row.get("confidence"),
                }
                for row in raw_results
            ]
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "kind": "symbol_results",
                        "query": query,
                        "results": results,
                    }
                ),
            )

        def _send_dirs(self, query_string: str) -> None:
            params = parse_qs(query_string, keep_blank_values=False)
            path = str((params.get("path") or [""])[0]).strip() or None
            payload = project_state.directory_listing(path)
            status = (
                HTTPStatus.BAD_REQUEST
                if payload.get("error") == "not a directory"
                else HTTPStatus.OK
            )
            self._send_bytes(status, _json_bytes(payload))

        def _send_init_status(self, query_string: str) -> None:
            params = parse_qs(query_string, keep_blank_values=False)
            path = str((params.get("path") or [""])[0]).strip()
            result = project_state.init_status(path)
            status = HTTPStatus.BAD_REQUEST if result.get("error") else HTTPStatus.OK
            self._send_bytes(status, _json_bytes(result))

        def _switch_project(self, payload: dict[str, Any]) -> None:
            result = project_state.switch_or_initialize_project(
                str(payload.get("path") or ""),
                initialize=bool(payload.get("initialize")),
            )
            if result.get("ok"):
                setattr(args, "scope", None)
                if hasattr(args, "_resolved_scope"):
                    delattr(args, "_resolved_scope")
            status = HTTPStatus.BAD_REQUEST if result.get("error") else HTTPStatus.OK
            self._send_bytes(status, _json_bytes(result))

        def _send_events(self, query_string: str) -> None:
            params = parse_qs(query_string, keep_blank_values=False)
            run_id = str((params.get("run_id") or [""])[0]).strip()
            event_type = str((params.get("event_type") or [""])[0]).strip().lower()
            file_path = str((params.get("file_path") or [""])[0]).strip()
            since = str((params.get("since") or [""])[0]).strip()
            try:
                limit = int((params.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            safe_limit = max(1, min(500, limit))
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                clauses: list[str] = ["1=1"]
                sql_params: list[Any] = []
                if run_id:
                    clauses.append("r.run_id = ?")
                    sql_params.append(run_id)
                if event_type:
                    clauses.append("e.event_type = ?")
                    sql_params.append(event_type)
                if file_path:
                    clauses.append("e.file_path = ?")
                    sql_params.append(file_path)
                if since:
                    clauses.append("e.timestamp >= ?")
                    sql_params.append(since)
                sql = (
                    "SELECT e.*, r.run_id, r.agent_name, r.status AS run_status "
                    "FROM agent_events e JOIN agent_runs r ON r.run_pk = e.run_pk "
                    f"WHERE {' AND '.join(clauses)} "
                    "ORDER BY e.timestamp DESC, e.event_pk DESC LIMIT ?"
                )
                sql_params.append(safe_limit)
                rows = conn.execute(sql, sql_params).fetchall()
                events = [agent_activity._row_to_event(row) for row in rows]
            finally:
                db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "kind": "code_index_agent_events",
                        "count": len(events),
                        "events": events,
                    }
                ),
            )

        def _send_events_summary(self) -> None:
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                day_ago = (
                    datetime.now(timezone.utc) - timedelta(hours=24)
                ).isoformat(timespec="milliseconds")
                rows = conn.execute(
                    """
                    SELECT event_type, COUNT(*) AS count
                      FROM agent_events
                     WHERE timestamp >= ?
                     GROUP BY event_type
                     ORDER BY count DESC
                    """,
                    (day_ago,),
                ).fetchall()
                event_types = {row["event_type"]: int(row["count"]) for row in rows}
                agent_rows = conn.execute(
                    """
                    SELECT agent_name, COUNT(*) AS run_count,
                           MAX(updated_at) AS last_active
                      FROM agent_runs
                     WHERE archived_at IS NULL
                     GROUP BY agent_name
                     ORDER BY run_count DESC
                    """,
                ).fetchall()
                agents = [
                    {
                        "agent_name": row["agent_name"] or "Agent",
                        "run_count": int(row["run_count"]),
                        "last_active": row["last_active"],
                    }
                    for row in agent_rows
                ]
                derived = agent_activity.agent_derived_file_relationships(
                    conn, limit=20
                )
            finally:
                db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "kind": "code_index_agent_events_summary",
                        "event_types_24h": event_types,
                        "agents": agents,
                        "derived_relationships": derived,
                    }
                ),
            )

        def _renew_file_claim(self, claim_id: str, payload: dict[str, Any]) -> None:
            from code_index import lease_manager

            try:
                ttl_raw = payload.get("ttl_seconds", 1800)
                ttl_seconds = None if ttl_raw is None else float(ttl_raw)
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        claim = lease_manager.renew_lease(
                            conn,
                            claim_id=claim_id,
                            lease_token=str(payload.get("lease_token") or ""),
                            fence_token=payload.get("fence_token"),
                            ttl_seconds=ttl_seconds,
                        )
                    finally:
                        db_mod.close(conn)
            except (TypeError, ValueError) as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "claim": claim}))

        def _release_file_claim(self, claim_id: str, payload: dict[str, Any]) -> None:
            from code_index import lease_manager

            try:
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        claim = lease_manager.release_lease(
                            conn,
                            claim_id=claim_id,
                            lease_token=str(payload.get("lease_token") or ""),
                            status=str(payload.get("status") or "released"),
                        )
                    finally:
                        db_mod.close(conn)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "claim": claim}))

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
                    message = str(exc)
                    if "claim conflict" in message:
                        _inc_counter(perf_state, "claim_conflicts")
                        status = HTTPStatus.CONFLICT
                    else:
                        status = HTTPStatus.BAD_REQUEST
                    self._send_bytes(
                        status,
                        _json_bytes({"error": message}),
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
            try:
                request = _task_request_from_payload(payload, args)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
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
                    request = scopes.apply_scope_to_request(
                        conn,
                        config.root,
                        request,
                        args,
                    )
                    active_claims = agent_activity.active_file_claims(conn, limit=200)
                    draft = _build_task_draft(
                        config,
                        request,
                        callback_base_url=self._callback_base_url(),
                    )
                    try:
                        blocking_runs = agent_activity.blocking_runs(
                            conn,
                            run_ids=request.get("blocked_by_run_ids") or [],
                        )
                    except ValueError as exc:
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": str(exc)}),
                        )
                        return
                    preflight = _preflight_from_draft(
                        request=request,
                        draft=draft,
                        active_claims=active_claims,
                        blocking_runs=blocking_runs,
                    )
                    from code_index.commands.graph_server_preflight import _build_preflight_record

                    record = _build_preflight_record(
                        secret=preflight_secret,
                        request=request,
                        draft=draft,
                        preflight=preflight,
                    )
                    preflight.update(record)
                    _store_preflight(
                        conn,
                        record=record,
                        request=request,
                        draft=draft,
                        preflight=preflight,
                    )
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
                        "kind": "code_index_graph_agent_task_preflight",
                        "draft": draft,
                        "preflight": preflight,
                        "dispatch_path": "/api/agent-runs",
                    }
                ),
            )

        def _send_agent_run(self, run_id: str) -> None:
            _reconcile_agent_runs(config)
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

        def _send_agent_run_message(self, run_id: str, payload: dict[str, Any]) -> None:
            try:
                request = _task_request_from_payload(payload, args)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
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
                    request = scopes.apply_scope_to_request(
                        conn,
                        config.root,
                        request,
                        args,
                    )
                    run = agent_activity.get_run(conn, run_id)
                    if run is None:
                        self._send_bytes(
                            HTTPStatus.NOT_FOUND,
                            _json_bytes({"error": f"unknown run_id: {run_id}"}),
                        )
                        return
                    if run.get("archived_at"):
                        self._send_bytes(
                            HTTPStatus.CONFLICT,
                            _json_bytes(
                                {
                                    "error": "run is archived",
                                    "run": run,
                                }
                            ),
                        )
                        return
                    preflight_draft = _build_task_draft(
                        config,
                        request,
                        callback_base_url=self._callback_base_url(),
                    )
                    active_claims = agent_activity.active_file_claims(conn, limit=200)
                    try:
                        blocking_runs = agent_activity.blocking_runs(
                            conn,
                            run_ids=request.get("blocked_by_run_ids") or [],
                        )
                    except ValueError as exc:
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": str(exc)}),
                        )
                        return
                    current_preflight = _preflight_from_draft(
                        request=request,
                        draft=preflight_draft,
                        active_claims=active_claims,
                        blocking_runs=blocking_runs,
                    )
                    rejection = _consume_preflight(
                        conn,
                        payload=payload,
                        request=request,
                        draft=preflight_draft,
                        preflight=current_preflight,
                        run_id=run_id,
                    )
                    if rejection is not None:
                        status, body = rejection
                        _inc_counter(
                            perf_state,
                            "preflight_rejections",
                            str(body.get("error") or "unknown"),
                        )
                        self._send_bytes(status, _json_bytes(body))
                        return
                    run_metadata = (
                        run.get("metadata")
                        if isinstance(run.get("metadata"), dict)
                        else {}
                    )
                    request = dict(request)
                    if not payload.get("agent_name"):
                        request["agent_name"] = run.get("agent_name") or request["agent_name"]
                    if not request.get("provider") and run_metadata.get("provider"):
                        request["provider"] = str(run_metadata["provider"]).strip().lower()
                    selected_nodes = []
                    for node_id in (request.get("selected_nodes") or []) + (
                        run.get("selected_nodes") or []
                    ):
                        if node_id and node_id not in selected_nodes:
                            selected_nodes.append(node_id)
                    selected_paths = []
                    for path in (
                        (request.get("selected_paths") or [])
                        + (run.get("active_files") or [])
                        + (run_metadata.get("selected_paths") or [])
                    ):
                        if path and path not in selected_paths:
                            selected_paths.append(path)
                    request["selected_nodes"] = selected_nodes
                    request["selected_paths"] = selected_paths
                    if not request.get("node") and selected_paths:
                        request["node"] = {
                            "id": f"file:{selected_paths[0]}",
                            "path": selected_paths[0],
                            "kind": "file",
                        }
                    request["parent_run_id"] = request.get("parent_run_id") or run_id
                    request["run_context"] = request.get("run_context") or {
                        "run_id": run_id,
                        "agent_name": run.get("agent_name") or request["agent_name"],
                        "status": run.get("status") or "working",
                    }
                    request_metadata = dict(request.get("metadata") or {})
                    if request.get("provider"):
                        request_metadata["provider"] = request["provider"]
                    request_metadata["parent_run_id"] = request["parent_run_id"]
                    request_metadata["run_context"] = request["run_context"]
                    request_metadata["same_run_message"] = True
                    request["metadata"] = request_metadata
                    status_text = str(run.get("status") or "").lower()
                    if status_text in agent_activity.STOPPED_STATUSES:
                        conn.execute(
                            """
                            UPDATE agent_runs
                               SET status = 'working',
                                   ended_at = NULL,
                                   updated_at = ?
                             WHERE run_id = ?
                            """,
                            (_now_iso(), run_id),
                        )
                    event = agent_activity.record_event(
                        conn,
                        run_id=run_id,
                        event_type="status",
                        file_path=(
                            request["selected_paths"][0]
                            if request["selected_paths"]
                            else request["node"].get("path")
                        ),
                        message=request["message"],
                        payload={
                            "status": (
                                "blocked"
                                if blocking_runs
                                else "working"
                            ),
                            "provider": request["provider"] or None,
                            "same_run_message": True,
                            "selected_nodes": request["selected_nodes"],
                            "selected_paths": request["selected_paths"],
                            "node": request["node"],
                            "blocked_by_run_ids": request.get("blocked_by_run_ids")
                            or [],
                        },
                    )
                    if request["selected_paths"]:
                        agent_activity.claim_files(
                            conn,
                            run_id=run_id,
                            file_paths=request["selected_paths"],
                            mode="review",
                            reason="Graph message selected file.",
                            metadata={"source": "graph-run-message"},
                        )
                    run = agent_activity.get_run(conn, run_id)
                except ValueError as exc:
                    http_status = (
                        HTTPStatus.CONFLICT
                        if str(exc).startswith("claim conflict:")
                        else HTTPStatus.BAD_REQUEST
                    )
                    self._send_bytes(
                        http_status,
                        _json_bytes({"error": str(exc)}),
                    )
                    return
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            task = _build_task_draft(
                config,
                request,
                callback_base_url=self._callback_base_url(),
            )
            task["kind"] = "code_index_agent_run_message"
            task["run_id"] = run_id
            task["same_run_message"] = True
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
                run_id=run_id,
                agent_name=request["agent_name"],
                selected_nodes=request["selected_nodes"],
                selected_paths=request["selected_paths"],
                node=request["node"],
            )
            task["collaboration"] = collaboration
            if isinstance(task.get("context_packet"), dict):
                task["context_packet"]["collaboration"] = collaboration
            if blocking_runs:
                dispatch = {
                    "configured": False,
                    "status": "blocked",
                    "reason": "blocked_by_run_ids are not complete",
                    "blocked_by_run_ids": request.get("blocked_by_run_ids") or [],
                }
            else:
                dispatch = _dispatch_agent_task(config, task)
            if dispatch.get("configured") and dispatch.get("status") == "sent":
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        dispatch_event = agent_activity.record_event(
                            conn,
                            run_id=run_id,
                            event_type="status",
                            message=(
                                "Message dispatched to local command adapter."
                                if dispatch.get("transport") == "local-command"
                                else "Message dispatched to agent webhook."
                            ),
                            payload={"status": "working", "dispatch": dispatch},
                        )
                        append_event_jsonl(config.root, dispatch_event)
                    finally:
                        db_mod.close(conn)
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                run = agent_activity.get_run(conn, run_id)
                transcript = agent_activity.run_transcript(conn, run_id)
                activity = run_orchestrator.snapshot(conn, limit=80)
                board = activity.get("kanban") or agent_activity.kanban_board(
                    conn, limit=25
                )
                board["orchestrator"] = activity.get("orchestrator")
            finally:
                db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "same_run": True,
                        "run": run,
                        "event": event,
                        "dispatch": dispatch,
                        "task": task,
                        "board": board,
                        "transcript": transcript,
                    }
                ),
            )

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
                    if status in agent_activity.STOPPED_STATUSES:
                        if status == "cancelled" and local_cancel_requested:
                            self._send_bytes(
                                HTTPStatus.OK,
                                _json_bytes(
                                    {
                                        "ok": True,
                                        "run": run,
                                        "event": None,
                                        "local_cancel_requested": True,
                                    }
                                ),
                            )
                            return
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

        def _accept_agent_run_review(
            self, run_id: str, payload: dict[str, Any]
        ) -> None:
            decision = str(
                payload.get("decision") or "Accepted reviewed agent work."
            ).strip()
            if not decision:
                decision = "Accepted reviewed agent work."
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
                    if run.get("archived_at"):
                        self._send_bytes(
                            HTTPStatus.CONFLICT,
                            _json_bytes({"error": "run is archived", "run": run}),
                        )
                        return
                    status = str(run.get("status") or "").lower()
                    if status not in agent_activity.REVIEW_STATUSES:
                        self._send_bytes(
                            HTTPStatus.CONFLICT,
                            _json_bytes(
                                {
                                    "error": "run is not awaiting review",
                                    "run": run,
                                }
                            ),
                        )
                        return
                    decision_payload = dict(payload.get("payload") or {})
                    decision_payload["review_action"] = "accepted"
                    event = agent_activity.record_decision(
                        conn,
                        run_id=run_id,
                        decision=decision,
                        status="accepted",
                        payload=decision_payload,
                    )
                    updated = agent_activity.end_run(
                        conn, run_id=run_id, status="completed"
                    )
                    suggestions_event = agent_activity.record_run_suggestions(
                        conn, run_id=run_id
                    )
                    suggestions = agent_activity.build_run_suggestions(conn, run_id)
                    activity = run_orchestrator.snapshot(conn, limit=80)
                    board = activity.get("kanban") or agent_activity.kanban_board(
                        conn, limit=25
                    )
                    board["orchestrator"] = activity.get("orchestrator")
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            append_event_jsonl(config.root, suggestions_event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": updated,
                        "event": event,
                        "suggestions_event": suggestions_event,
                        "suggestions": suggestions,
                        "board": board,
                    }
                ),
            )

        def _start_agent_run(self, payload: dict[str, Any]) -> None:
            try:
                request = _task_request_from_payload(payload, args)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            if not request["message"]:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "message is required"}),
                )
                return
            planned_run_id = secrets.token_hex(16)
            swarm_config = (
                request.get("swarm") if isinstance(request.get("swarm"), dict) else {}
            )
            swarm_enabled = bool(swarm_config.get("enabled"))
            swarm_child_runs: list[dict[str, Any]] = []
            swarm_child_events: list[dict[str, Any]] = []
            swarm_child_specs: list[dict[str, Any]] = []
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    request = scopes.apply_scope_to_request(
                        conn,
                        config.root,
                        request,
                        args,
                    )
                    preflight_draft = _build_task_draft(
                        config,
                        request,
                        callback_base_url=self._callback_base_url(),
                    )
                    try:
                        with db_mod.transaction(conn):
                            active_claims = agent_activity.active_file_claims(
                                conn,
                                limit=200,
                            )
                            try:
                                blocking_runs = agent_activity.blocking_runs(
                                    conn,
                                    run_ids=request.get("blocked_by_run_ids") or [],
                                )
                            except ValueError as exc:
                                self._send_bytes(
                                    HTTPStatus.BAD_REQUEST,
                                    _json_bytes({"error": str(exc)}),
                                )
                                return
                            current_preflight = _preflight_from_draft(
                                request=request,
                                draft=preflight_draft,
                                active_claims=active_claims,
                                blocking_runs=blocking_runs,
                            )
                            rejection = _consume_preflight(
                                conn,
                                payload=payload,
                                request=request,
                                draft=preflight_draft,
                                preflight=current_preflight,
                                run_id=planned_run_id,
                            )
                            if rejection is not None:
                                status, body = rejection
                                _inc_counter(
                                    perf_state,
                                    "preflight_rejections",
                                    str(body.get("error") or "unknown"),
                                )
                                self._send_bytes(status, _json_bytes(body))
                                return
                            initial_status = (
                                "blocked"
                                if blocking_runs
                                else ("working" if swarm_enabled else "queued")
                            )
                            run_metadata = dict(request["metadata"])
                            if swarm_enabled:
                                run_metadata = agent_swarm.swarm_parent_metadata(
                                    run_metadata,
                                    swarm_config,
                                )
                                request = dict(request)
                                request["metadata"] = run_metadata
                            run = agent_activity.start_run(
                                conn,
                                run_id=planned_run_id,
                                agent_name=request["agent_name"],
                                prompt=request["message"],
                                selected_nodes=request["selected_nodes"],
                                metadata=run_metadata,
                                status=initial_status,
                            )
                            if request.get("blocked_by_run_ids"):
                                agent_activity.add_run_blockers(
                                    conn,
                                    run_id=run["run_id"],
                                    blocked_by_run_ids=request["blocked_by_run_ids"],
                                    reason=(
                                        "Task waits for blocker run(s) before dispatch."
                                    ),
                                    metadata={"source": "graph-task"},
                                )
                                run = agent_activity.get_run(conn, run["run_id"]) or run
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
                                    "status": run.get("status") or "queued",
                                    "provider": request["provider"] or None,
                                    "execution_strategy": request.get(
                                        "execution_strategy"
                                    ),
                                    "swarm": agent_swarm.public_swarm_config(
                                        swarm_config
                                    ),
                                    "selected_nodes": request["selected_nodes"],
                                    "selected_paths": request["selected_paths"],
                                    "node": request["node"],
                                    "preflight_confirmed": request[
                                        "preflight_confirmed"
                                    ],
                                    "blocked_by_run_ids": request.get(
                                        "blocked_by_run_ids"
                                    )
                                    or [],
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
                            if swarm_enabled and not blocking_runs and run:
                                swarm_child_specs = agent_swarm.child_run_specs(
                                    request,
                                    parent_run_id=run["run_id"],
                                    swarm=swarm_config,
                                )
                                for spec in swarm_child_specs:
                                    child_run = agent_activity.start_run(
                                        conn,
                                        run_id=secrets.token_hex(16),
                                        agent_name=str(spec["agent_name"]),
                                        prompt=str(spec["prompt"]),
                                        selected_nodes=request["selected_nodes"],
                                        metadata=spec.get("metadata") or {},
                                        status="queued",
                                    )
                                    child_role = spec.get("role") or {}
                                    child_event = agent_activity.record_event(
                                        conn,
                                        run_id=child_run["run_id"],
                                        event_type="task",
                                        file_path=(
                                            request["selected_paths"][0]
                                            if request["selected_paths"]
                                            else request["node"].get("path")
                                        ),
                                        message=str(spec["prompt"]),
                                        payload={
                                            "status": "queued",
                                            "provider": spec.get("provider"),
                                            "execution_strategy": "swarm_child",
                                            "durable_execution_strategy": "swarm",
                                            "parent_run_id": run["run_id"],
                                            "swarm": (
                                                spec.get("metadata") or {}
                                            ).get("swarm"),
                                            "swarm_role": child_role,
                                            "selected_nodes": request["selected_nodes"],
                                            "selected_paths": request["selected_paths"],
                                        },
                                    )
                                    if request["selected_paths"]:
                                        agent_activity.claim_files(
                                            conn,
                                            run_id=child_run["run_id"],
                                            file_paths=request["selected_paths"],
                                            mode=str(
                                                spec.get("claim_mode") or "review"
                                            ),
                                            reason=(
                                                "Agent Swarm child selected file."
                                            ),
                                            metadata={
                                                "source": "agent-swarm",
                                                "parent_run_id": run["run_id"],
                                                "role": child_role.get("role"),
                                            },
                                        )
                                    swarm_child_runs.append(
                                        agent_activity.get_run(conn, child_run["run_id"])
                                        or child_run
                                    )
                                    swarm_child_events.append(child_event)
                    except ValueError as exc:
                        http_status = (
                            HTTPStatus.CONFLICT
                            if str(exc).startswith("claim conflict:")
                            else HTTPStatus.BAD_REQUEST
                        )
                        self._send_bytes(
                            http_status,
                            _json_bytes({"error": str(exc)}),
                        )
                        return
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            for child_event in swarm_child_events:
                append_event_jsonl(config.root, child_event)
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
            if blocking_runs:
                dispatch = {
                    "configured": False,
                    "status": "blocked",
                    "reason": "blocked_by_run_ids are not complete",
                    "blocked_by_run_ids": request.get("blocked_by_run_ids") or [],
                }
            elif swarm_enabled:
                swarm_tasks: list[dict[str, Any]] = []
                child_dispatches: list[dict[str, Any]] = []
                for child_run, spec in zip(swarm_child_runs, swarm_child_specs):
                    child_request = dict(request)
                    child_request.update(
                        {
                            "agent_name": spec["agent_name"],
                            "provider": spec["provider"],
                            "message": spec["prompt"],
                            "parent_run_id": task["run_id"],
                            "metadata": spec.get("metadata") or {},
                            "execution_strategy": "single",
                            "swarm": {"enabled": False},
                        }
                    )
                    child_task = _build_task_draft(
                        config,
                        child_request,
                        callback_base_url=self._callback_base_url(),
                    )
                    child_task["kind"] = "code_index_agent_task"
                    child_task["run_id"] = child_run["run_id"]
                    child_task["parent_run_id"] = task["run_id"]
                    child_task["execution_strategy"] = "swarm_child"
                    child_task["swarm"] = {
                        "parent_run_id": task["run_id"],
                        "role": spec.get("role"),
                        "provider": spec.get("provider"),
                    }
                    child_task["context_packet"] = _build_task_context_packet(
                        config,
                        message=str(spec["prompt"]),
                        selected_nodes=request["selected_nodes"],
                        selected_paths=request["selected_paths"],
                    )
                    if isinstance(child_task.get("context_packet"), dict):
                        child_task["context_packet"]["graph_context"] = child_task[
                            "graph_context"
                        ]
                    child_collaboration = _build_task_collaboration_packet(
                        config,
                        run_id=str(child_task["run_id"]),
                        agent_name=str(spec["agent_name"]),
                        selected_nodes=request["selected_nodes"],
                        selected_paths=request["selected_paths"],
                        node=request["node"],
                    )
                    child_task["collaboration"] = child_collaboration
                    if isinstance(child_task.get("context_packet"), dict):
                        child_task["context_packet"][
                            "collaboration"
                        ] = child_collaboration
                    child_dispatch = _dispatch_agent_task(config, child_task)
                    if (
                        child_dispatch.get("configured")
                        and child_dispatch.get("status") == "sent"
                    ):
                        with writer_lock(config):
                            conn = db_mod.connect(config.db_path)
                            try:
                                db_mod.apply_schema(conn)
                                dispatch_event = agent_activity.record_event(
                                    conn,
                                    run_id=str(child_task["run_id"]),
                                    event_type="status",
                                    message="Swarm child dispatched to agent webhook.",
                                    payload={
                                        "status": "working",
                                        "dispatch": child_dispatch,
                                    },
                                )
                                append_event_jsonl(config.root, dispatch_event)
                            finally:
                                db_mod.close(conn)
                    swarm_tasks.append(child_task)
                    child_dispatches.append(
                        {
                            "run_id": child_task["run_id"],
                            "role": (spec.get("role") or {}).get("role"),
                            "dispatch": child_dispatch,
                        }
                    )
                if swarm_child_runs:
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.ensure_schema(conn, config)
                        swarm_child_runs = [
                            agent_activity.get_run(conn, str(child["run_id"])) or child
                            for child in swarm_child_runs
                        ]
                    finally:
                        db_mod.close(conn)
                task["swarm_children"] = [
                    {
                        "run_id": child.get("run_id"),
                        "agent_name": child.get("agent_name"),
                        "status": child.get("status"),
                        "role": (spec.get("role") or {}).get("role"),
                        "provider": spec.get("provider"),
                    }
                    for child, spec in zip(swarm_child_runs, swarm_child_specs)
                ]
                any_configured = any(
                    bool(item["dispatch"].get("configured"))
                    for item in child_dispatches
                )
                dispatch = {
                    "configured": any_configured,
                    "status": "swarm_started" if any_configured else "not_configured",
                    "transport": "swarm",
                    "children": child_dispatches,
                    "task_count": len(swarm_tasks),
                }
            else:
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
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                activity = run_orchestrator.snapshot(conn, limit=80)
                board = activity.get("kanban") or agent_activity.kanban_board(
                    conn, limit=25
                )
                board["orchestrator"] = activity.get("orchestrator")
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
                        "board": board,
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
                    swarm_reconciliation = None
                    if str((updated_run or {}).get("status") or "").lower() in (
                        agent_activity.STOPPED_STATUSES
                    ):
                        metadata = (
                            updated_run.get("metadata")
                            if isinstance((updated_run or {}).get("metadata"), dict)
                            else {}
                        )
                        swarm_metadata = (
                            metadata.get("swarm")
                            if isinstance(metadata.get("swarm"), dict)
                            else {}
                        )
                        parent_run_id = metadata.get(
                            "parent_run_id"
                        ) or swarm_metadata.get("parent_run_id")
                        if parent_run_id:
                            try:
                                swarm_reconciliation = (
                                    agent_swarm.reconcile_swarm_parent(
                                        conn,
                                        parent_run_id=str(parent_run_id),
                                    )
                                )
                            except ValueError as exc:
                                swarm_reconciliation = {"error": str(exc)}
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
                        "swarm_reconciliation": swarm_reconciliation,
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
            last_agent_signature: str | None = None
            last_notes_mtime: int | None = None
            last_edge_signature: str | None = None
            base_interval = max(0.05, float(getattr(args, "event_interval", 1.0) or 1.0))
            env_interval = os.environ.get("CODE_INDEX_EVENT_INTERVAL")
            if env_interval:
                try:
                    base_interval = max(0.05, float(env_interval))
                except ValueError:
                    pass
            fast_interval = min(base_interval, 0.05)
            burst_ticks = 0
            stream_conn = db_mod.connect(config.db_path)
            try:
                while True:
                    try:
                        sent_something = False
                        signature = _state_signature(config, conn=stream_conn)
                        if signature["agent_signature"] != last_agent_signature:
                            last_agent_signature = signature["agent_signature"]
                            data = json.dumps(_agent_stream_payload(config))
                            self.wfile.write(f"event: agent\ndata: {data}\n\n".encode())
                            self.wfile.flush()
                            sent_something = True
                        if signature["notes_mtime"] != last_notes_mtime:
                            last_notes_mtime = signature["notes_mtime"]
                            data = json.dumps({"type": "graph", **signature})
                            self.wfile.write(f"event: graph\ndata: {data}\n\n".encode())
                            self.wfile.flush()
                            sent_something = True
                        edge_result = _dynamic_edge_signature(
                            config, conn=stream_conn, return_relationships=True
                        )
                        edge_signature, derived = edge_result  # type: ignore[misc]
                        if edge_signature != last_edge_signature:
                            last_edge_signature = edge_signature
                            data = json.dumps(
                                {
                                    "type": "connection:discovered",
                                    "signature": edge_signature,
                                    "derived_relationships": derived,
                                }
                            )
                            self.wfile.write(
                                f"event: connection\ndata: {data}\n\n".encode()
                            )
                            self.wfile.flush()
                            sent_something = True
                        if sent_something:
                            burst_ticks = 5
                            data = json.dumps(_perf_tick_payload(perf_state))
                            self.wfile.write(f"event: perf:tick\ndata: {data}\n\n".encode())
                            self.wfile.flush()
                        else:
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                        interval = fast_interval if burst_ticks > 0 else base_interval
                        if burst_ticks > 0:
                            burst_ticks -= 1
                        time.sleep(interval)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        _inc_counter(perf_state, "sse_dropped_events")
                        break
            finally:
                db_mod.close(stream_conn)


    return RoutesBase
