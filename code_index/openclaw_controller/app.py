"""OpenClaw controller dispatcher and long-running HTTP service wrapper."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from code_index.openclaw_controller.scheduler import FleetController
from code_index.openclaw_controller.service_config import (
    OPENCLAW_CONTEXT_STORE_PATH_ENV,
    OPENCLAW_CONTROLLER_DB_PATH_ENV,
    OPENCLAW_DEPLOYMENT_MODE_ENV,
    OPENCLAW_MESSAGING_DB_PATH_ENV,
    OPENCLAW_NATS_URL_ENV,
    OPENCLAW_REQUIRE_NATS_ENV,
    OPENCLAW_SIGNING_SECRET_ENV,
    OPENCLAW_TELEGRAM_BOT_TOKEN_ENV,
    OPENCLAW_TELEGRAM_SECRET_ENV,
    OpenClawConfigError,
    OpenClawDeploymentPaths,
    redact_nats_url,
    resolve_bind_host,
    resolve_nats_url,
    resolve_port,
    resolve_require_nats,
    resolve_service_paths,
)
from code_index.openclaw_context.store import SQLiteContextStore
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_hostd.leases import SQLiteFleetLeaseStore
from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_hostd.nats_client import create_nats_transport
from code_index.openclaw_messaging.adapter_registry import AdapterRegistry
from code_index.openclaw_messaging.routes import ApiResponse
from code_index.openclaw_messaging.routes import MessagingRouter
from code_index.openclaw_messaging.routes import Principal
from code_index.openclaw_messaging.store import MessagingStore


class _UnavailableNatsPublisher:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.connected = False

    def publish(self, subject: str, payload: Mapping[str, Any]) -> None:
        raise RuntimeError(str(self.error))

    def close(self) -> None:
        return


class _ConfiguredThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        use_ipv6: bool,
    ) -> None:
        self.address_family = socket.AF_INET6 if use_ipv6 else socket.AF_INET
        super().__init__(server_address, handler_cls)


class _ControllerRequestHandler(BaseHTTPRequestHandler):
    server_version = "OpenClawController/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _handle(self) -> None:
        service = self.server.service  # type: ignore[attr-defined]
        headers = {key: value for key, value in self.headers.items()}
        length = int(self.headers.get("Content-Length") or "0")
        body: dict[str, Any] | None = None
        if length:
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"request body must be valid JSON: {exc}"},
                )
                return
            if not isinstance(parsed, dict):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "request body must decode to a JSON object"},
                )
                return
            body = parsed
        response = service.handle_http_request(
            self.command,
            self.path,
            body=body,
            headers=headers,
        )
        self._write_json(response.status_code, response.body)

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _ControllerHTTPServer(_ConfiguredThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        service: "OpenClawControllerService",
        use_ipv6: bool,
    ) -> None:
        self.service = service
        super().__init__(
            server_address,
            _ControllerRequestHandler,
            use_ipv6=use_ipv6,
        )


class OpenClawControllerService:
    def __init__(self, runtime: "OpenClawControllerServiceRuntime") -> None:
        self.runtime = runtime

    def handle_http_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        if _is_health_path(path):
            return ApiResponse(200, self.runtime.health_payload())
        if _is_ready_path(path):
            status_code, payload = self.runtime.readiness_payload()
            return ApiResponse(status_code, payload)
        principal = _principal_from_headers(headers)
        return self.runtime.app.handle_request(
            method,
            path,
            body,
            headers=headers,
            principal=principal,
        )


class OpenClawControllerServiceRuntime:
    def __init__(
        self,
        *,
        app: "OpenClawControllerApp",
        context_store: SQLiteContextStore,
        lease_store: SQLiteFleetLeaseStore,
        deployment_paths: OpenClawDeploymentPaths,
        bind_host: str,
        port: int,
        require_nats: bool,
        nats_url: str | None,
        nats_reachable: bool,
        nats_error: str | None,
    ) -> None:
        self.app = app
        self.context_store = context_store
        self.lease_store = lease_store
        self.deployment_paths = deployment_paths
        self.bind_host = bind_host
        self.port = port
        self.require_nats = require_nats
        self.nats_url = nats_url
        self.nats_reachable = nats_reachable
        self.nats_error = nats_error

    def close(self) -> None:
        self.app.close()
        self.context_store.close()
        self.lease_store.close()
        _close_quietly(getattr(self.app.fleet_controller, "nats_client", None))

    def health_payload(self) -> dict[str, Any]:
        messaging_ok, messaging_error = _check_sqlite_store(self.app.store)
        context_ok, context_error = _check_sqlite_store(self.context_store)
        controller_ok, controller_error = _check_lease_store(self.lease_store)
        volume_ok = (
            self.deployment_paths.deployment_mode != "railway"
            or self.deployment_paths.volume_mount_path is not None
        )
        nats = {
            "configured": bool(self.nats_url),
            "required": self.require_nats,
            "reachable": self.nats_reachable,
            "url": redact_nats_url(self.nats_url),
        }
        if self.nats_error:
            nats["degraded_reason"] = self.nats_error
        degraded = [
            name
            for name, ok in (
                ("messaging_db", messaging_ok),
                ("context_store", context_ok),
                ("controller_db", controller_ok),
                ("volume", volume_ok),
            )
            if not ok
        ]
        if self.require_nats and (not self.nats_url or not self.nats_reachable):
            degraded.append("nats")
        payload = {
            "service": "openclaw_controller",
            "status": "ok" if not degraded else "degraded",
            "deployment_mode": self.deployment_paths.deployment_mode,
            "process": {"alive": True},
            "checks": {
                "messaging_db": {
                    "ok": messaging_ok,
                    "path": str(self.deployment_paths.messaging_db_path),
                },
                "context_store": {
                    "ok": context_ok,
                    "path": str(self.deployment_paths.context_store_path),
                },
                "controller_db": {
                    "ok": controller_ok,
                    "path": str(self.deployment_paths.controller_db_path),
                },
                "volume": {
                    "ok": volume_ok,
                    "configured": self.deployment_paths.volume_mount_path is not None,
                    "mount_path": (
                        str(self.deployment_paths.volume_mount_path)
                        if self.deployment_paths.volume_mount_path is not None
                        else None
                    ),
                },
                "signing_secret": {"configured": True},
                "nats": nats,
            },
        }
        if messaging_error:
            payload["checks"]["messaging_db"]["error"] = messaging_error
        if context_error:
            payload["checks"]["context_store"]["error"] = context_error
        if controller_error:
            payload["checks"]["controller_db"]["error"] = controller_error
        if degraded:
            payload["degraded"] = degraded
        return payload

    def readiness_payload(self) -> tuple[int, dict[str, Any]]:
        health = self.health_payload()
        reasons: list[str] = []
        checks = health["checks"]
        for name in ("messaging_db", "context_store", "controller_db", "volume"):
            if not checks[name]["ok"]:
                reasons.append(name)
        if not checks["signing_secret"]["configured"]:
            reasons.append("signing_secret")
        if self.require_nats:
            if not self.nats_url:
                reasons.append("nats_not_configured")
            elif not self.nats_reachable:
                reasons.append("nats_unreachable")
        ready = not reasons
        payload = {
            "service": "openclaw_controller",
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "deployment_mode": self.deployment_paths.deployment_mode,
            "reasons": reasons,
        }
        if not ready:
            payload["checks"] = checks
        return (200 if ready else 503), payload


@dataclass
class OpenClawControllerApp:
    store: MessagingStore
    router: MessagingRouter
    fleet_controller: FleetController | None = None
    controller_nats_runtime: "ControllerNatsRuntime | None" = None

    def handle_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> ApiResponse:
        if _is_fleet_path(path):
            if self.fleet_controller is None:
                return ApiResponse(404, {"error": "fleet controller is not configured"})
            return FleetRouter(self.fleet_controller).handle(
                method,
                path,
                body,
                principal=principal,
            )
        response = self.router.handle(
            method,
            path,
            body,
            headers=headers,
            principal=principal,
        )
        if self.fleet_controller is not None and _is_telegram_ingest_path(
            method,
            path,
        ):
            return _with_auto_assignment(
                response,
                controller=self.fleet_controller,
                store=self.store,
            )
        return response

    def close(self) -> None:
        if self.controller_nats_runtime is not None:
            self.controller_nats_runtime.close()
        self.store.close()


@dataclass(frozen=True)
class ControllerNatsRuntime:
    nats_client: Any
    subscriptions: tuple[str, ...]

    def close(self) -> None:
        return


def create_app(
    db_path: str | Path,
    *,
    signing_secret: str,
    telegram_secret_token: str | None = None,
    telegram_bot_token: str | None = None,
    telegram_http_client: Any | None = None,
    register_default_adapters: bool = True,
    lease_store: Any | None = None,
    nats_client: Any | None = None,
    fleet_controller: FleetController | None = None,
    attach_nats_subscriptions: bool = True,
) -> OpenClawControllerApp:
    store = MessagingStore(db_path, signing_secret=signing_secret)
    if register_default_adapters:
        AdapterRegistry(store).register_defaults()
    if fleet_controller is None:
        fleet_controller = FleetController(
            messaging_store=store,
            lease_store=lease_store or InMemoryFleetLeaseStore(),
            nats_client=nats_client,
        )
    controller_nats_runtime = None
    if (
        attach_nats_subscriptions
        and nats_client is not None
        and callable(getattr(nats_client, "subscribe", None))
    ):
        controller_nats_runtime = _attach_controller_nats_runtime(
            fleet_controller,
            nats_client=nats_client,
        )
    return OpenClawControllerApp(
        store=store,
        router=MessagingRouter(
            store,
            telegram_secret_token=telegram_secret_token,
            telegram_bot_token=telegram_bot_token,
            telegram_http_client=telegram_http_client,
        ),
        fleet_controller=fleet_controller,
        controller_nats_runtime=controller_nats_runtime,
    )


class FleetRouter:
    def __init__(self, controller: FleetController) -> None:
        self.controller = controller

    def handle(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> ApiResponse:
        method = method.upper()
        parts = [
            unquote(part)
            for part in urlsplit(path).path.strip("/").split("/")
            if part
        ]
        payload = dict(body or {})
        try:
            if method == "GET" and parts == ["fleet"]:
                return ApiResponse(200, self.controller.project_fleet())
            if method == "GET" and parts == ["fleet", "hosts"]:
                return ApiResponse(
                    200,
                    {"hosts": self.controller.project_fleet()["hosts"]},
                )
            if method == "POST" and parts == ["fleet", "hosts", "heartbeat"]:
                if not _principal_can_ingest_host(principal, payload):
                    return _forbidden()
                host = self.controller.record_host_heartbeat(_object(payload))
                return ApiResponse(200, {"host": host})
            if method == "POST" and parts == ["fleet", "hosts", "capabilities"]:
                if not _principal_can_ingest_host(principal, payload):
                    return _forbidden()
                host = self.controller.record_host_capabilities(_object(payload))
                return ApiResponse(200, {"host": host})
            if method == "POST" and parts == ["fleet", "agent-states"]:
                if not _principal_can_ingest_host(principal, payload):
                    return _forbidden()
                state = self.controller.record_agent_state(_object(payload))
                return ApiResponse(200, {"agent_state": state})
            if method == "POST" and parts == ["fleet", "run-events"]:
                if not _principal_can_ingest_host(principal, payload):
                    return _forbidden()
                event = self.controller.record_run_event(_object(payload))
                return ApiResponse(200, {"run_event": event})
            if method == "POST" and parts == ["fleet", "context", "health"]:
                if not _principal_has_scope(
                    principal,
                    {"fleet:ingest", "context:write"},
                ):
                    return _forbidden()
                health = self.controller.record_context_health(_object(payload))
                return ApiResponse(200, {"context_health": health})
            if method == "POST" and parts == ["fleet", "tasks"]:
                if not _principal_has_scope(
                    principal,
                    {"command:write", "controller:write", "fleet:assign"},
                ):
                    return _forbidden()
                command_ref = payload.get("command_ref")
                if not isinstance(command_ref, Mapping):
                    return ApiResponse(
                        400,
                        {"error": "command_ref is required"},
                    )
                result = self.controller.assign_task_from_command_ref(command_ref)
                return ApiResponse(_assignment_status_code(result), result.to_dict())
            if method == "POST" and parts == ["fleet", "task-claims"]:
                claimant_host_id = _claimant_host_id(principal, payload)
                if claimant_host_id is None:
                    return _forbidden()
                message_id = _optional_text(payload.get("message_id"))
                if message_id is None:
                    return ApiResponse(400, {"error": "message_id is required"})
                result = self.controller.claim_message_as_task(
                    message_id,
                    claimant_host_id=claimant_host_id,
                )
                response_body = result.to_dict()
                if response_body.get("assignment") is not None:
                    response_body["deliveries"] = (
                        self.controller.messaging_store.list_deliveries(message_id)
                    )
                return ApiResponse(_assignment_status_code(result), response_body)
            if method == "POST" and parts == ["fleet", "tasks", "ack"]:
                if not _principal_can_ingest_host(principal, payload):
                    return _forbidden()
                result = self.controller.record_task_ack(_object(payload))
                return ApiResponse(200, result)
            if method == "POST" and parts == ["fleet", "messages", "ack"]:
                if not _principal_can_ingest_host(principal, payload):
                    return _forbidden()
                result = self.controller.record_host_message_ack(_object(payload))
                return ApiResponse(200, result)
            if method == "POST" and parts == ["fleet", "handoffs"]:
                if not _principal_has_scope(
                    principal,
                    {"fleet:handoff", "context:handoff"},
                ):
                    return _forbidden()
                result = self.controller.submit_handoff_proposal(_object(payload))
                return ApiResponse(_handoff_status_code(result), result.to_dict())
        except KeyError as exc:
            return ApiResponse(404, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            return ApiResponse(400, {"error": str(exc)})
        return ApiResponse(404, {"error": "route not found"})


def build_service_runtime(
    *,
    environ: Mapping[str, str] | None = None,
) -> OpenClawControllerServiceRuntime:
    env = dict(os.environ if environ is None else environ)
    signing_secret = str(env.get(OPENCLAW_SIGNING_SECRET_ENV, "")).strip()
    if not signing_secret:
        raise OpenClawConfigError(
            f"{OPENCLAW_SIGNING_SECRET_ENV} is required for the controller service"
        )
    deployment_paths = resolve_service_paths(env)
    lease_store = SQLiteFleetLeaseStore(deployment_paths.controller_db_path)
    context_store = SQLiteContextStore(deployment_paths.context_store_path)
    nats_url = resolve_nats_url(env)
    require_nats = resolve_require_nats(env)
    nats_client, nats_reachable, nats_error = _build_nats_client(nats_url)
    app = create_app(
        deployment_paths.messaging_db_path,
        signing_secret=signing_secret,
        telegram_secret_token=str(env.get(OPENCLAW_TELEGRAM_SECRET_ENV, "")).strip()
        or None,
        telegram_bot_token=str(env.get(OPENCLAW_TELEGRAM_BOT_TOKEN_ENV, "")).strip()
        or None,
        lease_store=lease_store,
        nats_client=nats_client,
    )
    return OpenClawControllerServiceRuntime(
        app=app,
        context_store=context_store,
        lease_store=lease_store,
        deployment_paths=deployment_paths,
        bind_host=resolve_bind_host(env),
        port=resolve_port(env, default=8000),
        require_nats=require_nats,
        nats_url=nats_url,
        nats_reachable=nats_reachable,
        nats_error=nats_error,
    )


def serve(runtime: OpenClawControllerServiceRuntime) -> int:
    service = OpenClawControllerService(runtime)
    bind_host = runtime.bind_host
    server = _ControllerHTTPServer(
        (bind_host, runtime.port),
        service=service,
        use_ipv6=":" in bind_host,
    )
    try:
        print(
            json.dumps(
                {
                    "service": "openclaw_controller",
                    "bind_host": bind_host,
                    "port": runtime.port,
                    "deployment_mode": runtime.deployment_paths.deployment_mode,
                    "messaging_db_path": str(runtime.deployment_paths.messaging_db_path),
                    "controller_db_path": str(runtime.deployment_paths.controller_db_path),
                    "context_store_path": str(runtime.deployment_paths.context_store_path),
                    "ready_path": "/ready",
                    "health_path": "/health",
                },
                sort_keys=True,
            )
        )
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        runtime.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.serve:
        try:
            runtime = build_service_runtime(environ=_service_environ(args))
        except OpenClawConfigError as exc:
            print(json.dumps({"error": str(exc)}))
            return 1
        return serve(runtime)
    try:
        body = json.loads(args.body_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--body-json must be valid JSON: {exc}")
    if not isinstance(body, dict):
        parser.error("--body-json must be a JSON object")
    if not args.signing_secret:
        parser.error("--signing-secret or OPENCLAW_CONTROLLER_SIGNING_SECRET is required")

    app = create_app(
        args.db,
        signing_secret=args.signing_secret,
        telegram_secret_token=args.telegram_secret_token,
        telegram_bot_token=args.telegram_bot_token,
    )
    try:
        response = app.handle_request(args.method, args.path, body)
        print(
            json.dumps(
                {"status_code": response.status_code, "body": response.body},
                sort_keys=True,
            )
        )
        return 0 if response.status_code < 500 else 1
    finally:
        app.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenClaw controller dispatcher and long-running HTTP service."
    )
    parser.add_argument("--db", default=":memory:", help="SQLite database path")
    parser.add_argument("--method", default="GET", help="Request method")
    parser.add_argument("--path", default="/rooms", help="Request path")
    parser.add_argument(
        "--body-json",
        default="{}",
        help="Request body JSON object for dispatcher smoke checks",
    )
    parser.add_argument(
        "--signing-secret",
        default=os.environ.get(OPENCLAW_SIGNING_SECRET_ENV),
        help=(
            "Command reference signing secret. May also use "
            "OPENCLAW_CONTROLLER_SIGNING_SECRET."
        ),
    )
    parser.add_argument(
        "--telegram-secret-token",
        default=os.environ.get(OPENCLAW_TELEGRAM_SECRET_ENV),
        help=(
            "Telegram webhook secret token. May also use "
            "OPENCLAW_TELEGRAM_SECRET_TOKEN."
        ),
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=os.environ.get(OPENCLAW_TELEGRAM_BOT_TOKEN_ENV),
        help=(
            "Telegram bot token for long-poll ingestion. May also use "
            "OPENCLAW_TELEGRAM_BOT_TOKEN."
        ),
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the long-lived HTTP controller service.",
    )
    parser.add_argument("--host", default=None, help="Service bind host override.")
    parser.add_argument("--port", type=int, default=None, help="Service port override.")
    parser.add_argument(
        "--controller-db",
        default=None,
        help="Controller SQLite state path override.",
    )
    parser.add_argument(
        "--messaging-db",
        default=None,
        help="Messaging SQLite path override.",
    )
    parser.add_argument(
        "--context-store-db",
        default=None,
        help="Context store SQLite path override.",
    )
    parser.add_argument(
        "--nats-url",
        default=None,
        help="OpenClaw NATS URL override.",
    )
    parser.add_argument(
        "--deployment-mode",
        default=None,
        help="Deployment mode override: development, production, or railway.",
    )
    parser.add_argument(
        "--require-nats",
        action="store_true",
        help="Require NATS readiness for the service.",
    )
    parser.add_argument(
        "--no-require-nats",
        action="store_true",
        help="Disable strict NATS readiness for the service.",
    )
    return parser


def _service_environ(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    overrides = {
        OPENCLAW_CONTROLLER_DB_PATH_ENV: args.controller_db,
        OPENCLAW_MESSAGING_DB_PATH_ENV: args.messaging_db,
        OPENCLAW_CONTEXT_STORE_PATH_ENV: args.context_store_db,
        OPENCLAW_NATS_URL_ENV: args.nats_url,
        OPENCLAW_DEPLOYMENT_MODE_ENV: args.deployment_mode,
        OPENCLAW_SIGNING_SECRET_ENV: args.signing_secret,
        OPENCLAW_TELEGRAM_SECRET_ENV: args.telegram_secret_token,
        OPENCLAW_TELEGRAM_BOT_TOKEN_ENV: args.telegram_bot_token,
    }
    if args.host:
        env["OPENCLAW_BIND_HOST"] = args.host
    if args.port is not None:
        env["PORT"] = str(args.port)
    if args.require_nats and args.no_require_nats:
        raise OpenClawConfigError(
            "--require-nats and --no-require-nats cannot be used together"
        )
    if args.require_nats:
        env[OPENCLAW_REQUIRE_NATS_ENV] = "1"
    elif args.no_require_nats:
        env[OPENCLAW_REQUIRE_NATS_ENV] = "0"
    for key, value in overrides.items():
        if value is not None:
            env[key] = str(value)
    return env


def _build_nats_client(
    nats_url: str | None,
) -> tuple[Any | None, bool, str | None]:
    if not nats_url:
        return None, False, None
    try:
        client = NatsClient(transport=create_nats_transport(nats_url))
        client.connect()
        return client, True, None
    except Exception as exc:
        return _UnavailableNatsPublisher(exc), False, str(exc)


def _check_sqlite_store(store: Any) -> tuple[bool, str | None]:
    try:
        ping = getattr(store, "ping", None)
        if ping is not None:
            ping()
        else:
            getattr(store, "conn").execute("SELECT 1").fetchone()
    except Exception as exc:
        return False, str(exc)
    return True, None


def _check_lease_store(store: SQLiteFleetLeaseStore) -> tuple[bool, str | None]:
    try:
        store.conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        return False, str(exc)
    return True, None


def _close_quietly(value: Any) -> None:
    if value is None:
        return
    close = getattr(value, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return


def _is_fleet_path(path: str) -> bool:
    parts = [part for part in urlsplit(path).path.strip("/").split("/") if part]
    return bool(parts and parts[0] == "fleet")


def _is_telegram_ingest_path(method: str, path: str) -> bool:
    parts = tuple(part for part in urlsplit(path).path.strip("/").split("/") if part)
    return method.upper() == "POST" and parts in {
        ("adapters", "telegram", "webhook"),
        ("adapters", "telegram", "poll"),
    }


def _is_health_path(path: str) -> bool:
    return urlsplit(path).path.rstrip("/") == "/health"


def _is_ready_path(path: str) -> bool:
    return urlsplit(path).path.rstrip("/") == "/ready"


def _principal_from_headers(headers: Mapping[str, Any] | None) -> Principal | None:
    header_map = {
        str(key).lower(): str(value).strip()
        for key, value in dict(headers or {}).items()
        if str(value).strip()
    }
    principal_id = header_map.get("x-openclaw-principal-id", "")
    scopes_text = header_map.get("x-openclaw-principal-scopes", "")
    scopes = {
        item.strip()
        for item in scopes_text.replace(",", " ").split()
        if item.strip()
    }
    if not principal_id or not scopes:
        return None
    return Principal(principal_id=principal_id, scopes=frozenset(scopes))


def _with_auto_assignment(
    response: ApiResponse,
    *,
    controller: FleetController,
    store: MessagingStore,
) -> ApiResponse:
    if response.status_code >= 400:
        return response
    if "results" in response.body and isinstance(response.body.get("results"), list):
        body = dict(response.body)
        auto_assignments = [
            _auto_assignment_for_result(result, controller=controller, store=store)
            for result in body["results"]
            if isinstance(result, Mapping)
        ]
        auto_assignments = [item for item in auto_assignments if item is not None]
        if auto_assignments:
            body["auto_assignments"] = auto_assignments
        return ApiResponse(response.status_code, body)
    auto_assignment = _auto_assignment_for_result(
        response.body,
        controller=controller,
        store=store,
    )
    if auto_assignment is None:
        return response
    body = dict(response.body)
    body["auto_assignment"] = auto_assignment
    return ApiResponse(response.status_code, body)


def _auto_assignment_for_result(
    result: Mapping[str, Any],
    *,
    controller: FleetController,
    store: MessagingStore,
) -> dict[str, Any] | None:
    if not bool(result.get("created")):
        return None
    command_ref = result.get("command_ref")
    if not isinstance(command_ref, Mapping):
        return _auto_alias_claim_for_result(
            result,
            controller=controller,
            store=store,
        )
    if str(command_ref.get("command_type") or "").strip() != "assign_task":
        return None
    assignment = controller.assign_task_from_command_ref(command_ref).to_dict()
    message_id = _optional_text(command_ref.get("message_id"))
    if message_id is not None:
        assignment["deliveries"] = store.list_deliveries(message_id)
    return assignment


def _auto_alias_claim_for_result(
    result: Mapping[str, Any],
    *,
    controller: FleetController,
    store: MessagingStore,
) -> dict[str, Any] | None:
    if not bool(result.get("created")):
        return None
    message = result.get("message")
    if not isinstance(message, Mapping):
        return None
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    routing = metadata.get("routing")
    claimable = metadata.get("claimable_work")
    if not isinstance(routing, Mapping) or not _optional_text(
        routing.get("host_alias")
    ):
        return None
    if not isinstance(claimable, Mapping):
        return None
    message_id = _optional_text(message.get("message_id"))
    if message_id is None:
        return None
    promoted = store.promote_message_to_assign_task_command_ref(message_id)
    assignment = controller.assign_task_from_command_ref(
        promoted["command_ref"],
    ).to_dict()
    assignment["deliveries"] = store.list_deliveries(message_id)
    return assignment


def _attach_controller_nats_runtime(
    controller: FleetController,
    *,
    nats_client: Any,
) -> ControllerNatsRuntime:
    if not getattr(nats_client, "connected", False):
        connect = getattr(nats_client, "connect", None)
        if callable(connect):
            connect()
    subscriptions = (
        "openclaw.host.*.heartbeat",
        "openclaw.host.*.capabilities",
        "openclaw.task.*.ack",
        "openclaw.host.*.messages.ack",
    )
    subscribe = getattr(nats_client, "subscribe", None)
    if not callable(subscribe):
        raise RuntimeError("NATS client has no subscribe()")
    subscribe(
        subscriptions[0],
        lambda message: controller.record_host_heartbeat(_message_payload(message)),
    )
    subscribe(
        subscriptions[1],
        lambda message: controller.record_host_capabilities(_message_payload(message)),
    )
    subscribe(
        subscriptions[2],
        lambda message: controller.record_task_ack(_message_payload(message)),
    )
    subscribe(
        subscriptions[3],
        lambda message: controller.record_host_message_ack(_message_payload(message)),
    )
    return ControllerNatsRuntime(
        nats_client=nats_client,
        subscriptions=subscriptions,
    )


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError("expected object")


def _message_payload(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return dict(message)
    data = getattr(message, "data", message)
    if isinstance(data, bytes):
        payload = json.loads(data.decode("utf-8"))
    elif isinstance(data, str):
        payload = json.loads(data)
    else:
        raise ValueError("controller NATS message must be a JSON object")
    if not isinstance(payload, dict):
        raise ValueError("controller NATS message must be a JSON object")
    return payload


def _principal_has_scope(
    principal: Principal | None,
    allowed_scopes: set[str],
) -> bool:
    if principal is None:
        return False
    return bool(set(principal.scopes) & allowed_scopes)


def _principal_can_ingest_host(
    principal: Principal | None,
    payload: Mapping[str, Any],
) -> bool:
    if principal is None:
        return False
    scopes = set(principal.scopes)
    if "fleet:ingest" in scopes:
        return True
    if "host:ingest" not in scopes:
        return False
    host_id = _optional_text(payload.get("host_id"))
    return host_id is not None and principal.principal_id == host_id


def _claimant_host_id(
    principal: Principal | None,
    payload: Mapping[str, Any],
) -> str | None:
    if principal is None:
        return None
    requested = _optional_text(payload.get("claimant_host_id")) or _optional_text(
        payload.get("host_id")
    )
    scopes = set(principal.scopes)
    if "host:ingest" in scopes:
        if requested is not None and requested != principal.principal_id:
            return None
        return principal.principal_id
    if scopes & {"command:write", "controller:write", "fleet:assign"}:
        return requested
    return None


def _forbidden() -> ApiResponse:
    return ApiResponse(403, {"error": "fleet write requires trusted principal scope"})


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _assignment_status_code(result: Any) -> int:
    if result.status == "assigned":
        return 202
    if result.rejection and result.rejection.reason == "invalid_command_ref":
        return 403
    return 409


def _handoff_status_code(result: Any) -> int:
    if result.status == "authorized":
        return 202
    if result.rejection and result.rejection.reason == "invalid_handoff_proposal":
        return 400
    return 409


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
