"""`code_index graph-server`: local live graph server with SSE."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands.graph_html import render_html
from code_index.commands.graph_model import build_graph
from code_index.commands.graph_notes import graph_notes_block, notes_path, upsert_note
from code_index.locking import writer_lock


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")


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
            agent_activity.record_event(
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
            "agent_events_path": "/api/agent-events",
        }
        return payload
    finally:
        db_mod.close(conn)


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

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
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
            if route == "/notes.json":
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(graph_notes_block(config.root)),
                )
                return
            if route == "/events":
                self._stream_events()
                return
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found", "path": route}),
            )

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
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
            if route == "/api/agent-events":
                self._record_agent_event(payload)
                return
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found", "path": route}),
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
                    run_id = payload.get("run_id")
                    run = (
                        agent_activity.get_run(conn, str(run_id))
                        if run_id
                        else agent_activity.latest_active_run(conn, agent_name=agent_name)
                    )
                    if run is None:
                        run = agent_activity.start_run(
                            conn,
                            agent_name=agent_name,
                            prompt=str(payload.get("prompt") or ""),
                            metadata={"source": "graph-server"},
                        )
                    event = agent_activity.record_event(
                        conn,
                        run_id=run["run_id"],
                        event_type=event_type,
                        file_path=payload.get("file_path") or payload.get("file"),
                        symbol_path=payload.get("symbol_path"),
                        message=payload.get("message"),
                        payload=payload.get("payload") or {},
                    )
                finally:
                    db_mod.close(conn)
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "event": event}))

        def _stream_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_signature: dict[str, Any] | None = None
            interval = max(0.25, float(getattr(args, "event_interval", 1.0) or 1.0))
            while True:
                try:
                    signature = _state_signature(config)
                    if signature != last_signature:
                        last_signature = signature
                        data = json.dumps({"type": "graph", **signature})
                        self.wfile.write(f"event: graph\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    time.sleep(interval)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break

    return GraphHandler


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    host = args.host or "127.0.0.1"
    port = int(args.port or 8767)
    server = ThreadingHTTPServer((host, port), _make_handler(config, args))
    server.quiet = bool(getattr(args, "quiet", False))  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/repo-graph.html"
    print(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("graph server stopped")
    finally:
        server.server_close()
    return 0
