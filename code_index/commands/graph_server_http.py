"""HTTP handler factory for the live graph server."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

from code_index import agent_providers
from code_index import config as cfg_mod
from code_index.commands.graph_html import render_html
from code_index.commands.graph_mobile import render_mobile_html
from code_index.commands.graph_notes import graph_notes_block, upsert_note
from code_index.commands.graph_server_perf import (
    _inc_counter,
    _make_perf_state,
    _perf_snapshot,
)
from code_index.commands.graph_server_state import (
    _agent_runtime_payload,
    _build_debug_payload,
    _build_payload,
    _record_user_note_event,
)
from code_index.commands.graph_server_router import Router
from code_index.commands.graph_server_routes import _make_routes_class
from code_index.commands.graph_server_utils import (
    GRAPH_SESSION_COOKIE,
    GRAPH_SESSION_MAX_AGE_SECONDS,
    GRAPH_TOKEN_ENV_VAR,
    _auth_page_html,
    _json_bytes,
    _now_iso,
    _session_cookie_value,
    _validate_bearer,
)

PREFLIGHT_TTL_SECONDS = 10 * 60


def _make_handler(config: cfg_mod.Config, args: argparse.Namespace):
    from code_index.commands.graph_server_utils import _cookie_value, _preflight_secret

    preflight_secret = _preflight_secret()
    perf_state = _make_perf_state()

    RoutesBase = _make_routes_class(config, args, preflight_secret, perf_state)

    class GraphHandler(BaseHTTPRequestHandler, RoutesBase):
        server_version = "code_index-graph/1"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            if getattr(self.server, "quiet", False):
                return
            super().log_message(format, *args)

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str = "application/json",
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
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

        def _is_authorized(self) -> bool:
            token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
            if not token:
                return True
            if _validate_bearer(self.headers.get("Authorization"), token):
                return True
            expected_cookie = _session_cookie_value(preflight_secret, token)
            cookie = _cookie_value(self.headers.get("Cookie"), GRAPH_SESSION_COOKIE)
            if cookie and hmac.compare_digest(cookie, expected_cookie):
                return True
            return False

        def _authorized(self) -> bool:
            if self._is_authorized():
                return True
            route = urlparse(self.path).path
            _inc_counter(perf_state, "auth_failures", route or "unknown")
            self._send_unauthorized()
            return False

        def _send_auth_page(self) -> None:
            self._send_bytes(
                HTTPStatus.OK,
                _auth_page_html().encode("utf-8"),
                "text/html",
            )

        def _create_browser_session(self) -> None:
            token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
            if not token:
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes({"ok": True, "auth_required": False}),
                )
                return
            if not _validate_bearer(self.headers.get("Authorization"), token):
                _inc_counter(perf_state, "auth_failures", "/api/auth/browser-session")
                self._send_unauthorized()
                return
            cookie_value = _session_cookie_value(preflight_secret, token)
            cookie = (
                f"{GRAPH_SESSION_COOKIE}={cookie_value}; "
                f"Max-Age={GRAPH_SESSION_MAX_AGE_SECONDS}; "
                "Path=/; HttpOnly; SameSite=Strict"
            )
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "auth_required": True,
                        "auth": "browser-session-cookie",
                        "cookie_name": GRAPH_SESSION_COOKIE,
                    }
                ),
                headers={"Set-Cookie": cookie},
            )

        def _read_json_payload(self) -> dict[str, Any] | None:
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid Content-Length"}),
                )
                return None
            if length < 0:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid Content-Length"}),
                )
                return None
            MAX_BODY_SIZE = 2 * 1024 * 1024  # 2 MB
            if length > MAX_BODY_SIZE:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "body too large"}),
                )
                return None
            try:
                body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(body or "{}")
            except UnicodeDecodeError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "body must be UTF-8 JSON"}),
                )
                return None
            except json.JSONDecodeError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid JSON body"}),
                )
                return None
            if not isinstance(payload, dict):
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "JSON body must be an object"}),
                )
                return None
            return payload

        @classmethod
        def _build_router(cls) -> Router:
            """Assemble route table. Called once during class creation."""
            router = Router()
            # Public (auth optional)
            router.get("/api/auth/browser-session", cls._route_browser_session)
            # HTML / JSON assets
            router.get("/", cls._route_repo_graph_html)
            router.get("/repo-graph.html", cls._route_repo_graph_html)
            router.get("/mobile.html", cls._route_mobile_html)
            router.get("/m", cls._route_mobile_html)
            router.get("/repo-graph.json", cls._route_repo_graph_json)
            router.get("/notes.json", cls._route_notes_json)
            # Debug / meta
            router.get("/api/debug", cls._route_debug)
            router.get("/api/debug/perf", cls._route_debug_perf)
            router.get("/api/agent-providers", cls._route_agent_providers)
            router.get("/api/agent-board", cls._route_agent_board)
            router.get("/api/file-claims", cls._route_file_claims)
            router.get("/api/search", cls._route_search)
            router.get("/api/symbols", cls._route_symbols)
            router.get("/api/events", cls._route_events)
            router.get("/api/events/summary", cls._route_events_summary)
            router.get("/api/agent-runs/{run_id}", cls._route_agent_run_get)
            router.get("/events", cls._route_stream_events)
            # POST routes
            router.post("/api/auth/browser-session", cls._route_browser_session_post)
            router.post("/api/notes", cls._route_notes_post)
            router.post("/api/agent-runs", cls._route_agent_runs_post)
            router.post("/api/agent-task-preflight", cls._route_preflight_post)
            router.post("/api/agent-runs/{run_id}/messages", cls._route_agent_run_message)
            router.post("/api/agent-runs/{run_id}/cancel", cls._route_agent_run_cancel)
            router.post("/api/agent-runs/{run_id}/accept-review", cls._route_agent_run_accept_review)
            router.post("/api/agent-runs/{run_id}/archive", cls._route_agent_run_archive)
            router.post("/api/agent-events", cls._route_agent_events_post)
            router.post("/api/file-claims/{claim_id}/renew", cls._route_claim_renew)
            router.post("/api/file-claims/{claim_id}/release", cls._route_claim_release)
            router.post("/api/file-claims", cls._route_file_claims_post)
            return router

        # ------------------------------------------------------------------
        # Route handlers (thin wrappers around existing methods)
        # ------------------------------------------------------------------

        def _route_repo_graph_html(self, _params: dict[str, str]) -> None:
            payload = _build_payload(config, args)
            self._send_bytes(
                HTTPStatus.OK,
                render_html(payload).encode("utf-8"),
                "text/html",
            )

        def _route_mobile_html(self, _params: dict[str, str]) -> None:
            payload = {
                "kind": "code_index_graph_mobile",
                "root": str(config.root),
                "generated_at": _now_iso(),
                "agent": _agent_runtime_payload(),
                "live": {
                    "server": True,
                    "desktop_graph_path": "/repo-graph.html",
                    "graph_path": "/repo-graph.json",
                    "mobile_path": "/mobile.html",
                    "events_path": "/events",
                    "debug_path": "/api/debug",
                    "debug_perf_path": "/api/debug/perf",
                    "search_path": "/api/search",
                    "agent_board_path": "/api/agent-board",
                    "agent_preflight_path": "/api/agent-task-preflight",
                    "agent_runs_path": "/api/agent-runs",
                    "agent_run_detail_path": "/api/agent-runs/{run_id}",
                    "agent_run_messages_path": "/api/agent-runs/{run_id}/messages",
                    "agent_run_cancel_path": "/api/agent-runs/{run_id}/cancel",
                    "agent_run_accept_review_path": "/api/agent-runs/{run_id}/accept-review",
                    "agent_run_archive_path": "/api/agent-runs/{run_id}/archive",
                    "agent_providers_path": "/api/agent-providers",
                    "file_claims_path": "/api/file-claims",
                    "events_summary_path": "/api/events/summary",
                },
            }
            self._send_bytes(
                HTTPStatus.OK,
                render_mobile_html(payload).encode("utf-8"),
                "text/html",
            )

        def _route_repo_graph_json(self, _params: dict[str, str]) -> None:
            self._send_bytes(HTTPStatus.OK, _json_bytes(_build_payload(config, args)))

        def _route_notes_json(self, _params: dict[str, str]) -> None:
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(graph_notes_block(config.root)),
            )

        def _route_debug(self, _params: dict[str, str]) -> None:
            perf = _perf_snapshot(perf_state)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(_build_debug_payload(config, args, perf)),
            )

        def _route_debug_perf(self, _params: dict[str, str]) -> None:
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(_perf_snapshot(perf_state)),
            )

        def _route_agent_providers(self, _params: dict[str, str]) -> None:
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "kind": "code_index_agent_provider_registry",
                        "providers": agent_providers.provider_registry_payload(),
                        "runtime": _agent_runtime_payload(),
                    }
                ),
            )

        def _route_agent_board(self, _params: dict[str, str]) -> None:
            self._send_agent_board()

        def _route_file_claims(self, _params: dict[str, str]) -> None:
            self._send_file_claims()

        def _route_search(self, _params: dict[str, str]) -> None:
            parsed = urlparse(self.path)
            self._send_search(parsed.query)

        def _route_symbols(self, _params: dict[str, str]) -> None:
            parsed = urlparse(self.path)
            self._send_symbols(parsed.query)

        def _route_events(self, _params: dict[str, str]) -> None:
            parsed = urlparse(self.path)
            self._send_events(parsed.query)

        def _route_events_summary(self, _params: dict[str, str]) -> None:
            self._send_events_summary()

        def _route_agent_run_get(self, params: dict[str, str]) -> None:
            self._send_agent_run(params["run_id"])

        def _route_stream_events(self, _params: dict[str, str]) -> None:
            self._stream_events()

        def _route_browser_session(self, _params: dict[str, str]) -> None:
            # GET variant falls through to 404; auth page is handled in do_GET
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found"}),
            )

        def _route_browser_session_post(self, _params: dict[str, str]) -> None:
            self._create_browser_session()

        def _route_notes_post(self, _params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
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

        def _route_agent_runs_post(self, _params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._start_agent_run(payload)

        def _route_preflight_post(self, _params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._preflight_agent_task(payload)

        def _route_agent_run_message(self, params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._send_agent_run_message(params["run_id"], payload)

        def _route_agent_run_cancel(self, params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._cancel_agent_run(params["run_id"])

        def _route_agent_run_accept_review(self, params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._accept_agent_run_review(params["run_id"], payload)

        def _route_agent_run_archive(self, params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._archive_agent_run(params["run_id"])

        def _route_agent_events_post(self, _params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._record_agent_event(payload)

        def _route_claim_renew(self, params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._renew_file_claim(params["claim_id"], payload)

        def _route_claim_release(self, params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._release_file_claim(params["claim_id"], payload)

        def _route_file_claims_post(self, _params: dict[str, str]) -> None:
            payload = self._read_json_payload()
            if payload is None:
                return
            self._manage_file_claims(payload)

        # ------------------------------------------------------------------
        # Dispatch
        # ------------------------------------------------------------------

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            # Public auth endpoint bypasses authorization
            if route == "/api/auth/browser-session" and method == "POST":
                self._create_browser_session()
                return
            if not self._is_authorized():
                if route in {"/", "/repo-graph.html", "/mobile.html", "/m"} and os.environ.get(
                    GRAPH_TOKEN_ENV_VAR, ""
                ).strip():
                    self._send_auth_page()
                    return
                _inc_counter(perf_state, "auth_failures", route or "unknown")
                self._send_unauthorized()
                return
            resolved = self._router.resolve(method, route)
            if resolved is None:
                self._send_bytes(
                    HTTPStatus.NOT_FOUND,
                    _json_bytes({"error": "not found", "path": route}),
                )
                return
            handler, params = resolved
            handler(self, params)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _callback_base_url(self) -> str:
            host = self.headers.get("Host")
            if not host:
                address = self.server.server_address  # type: ignore[attr-defined]
                host = f"{address[0]}:{address[1]}"
            return f"http://{host}"

    GraphHandler._router = GraphHandler._build_router()
    return GraphHandler
