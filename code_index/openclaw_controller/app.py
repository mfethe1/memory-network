"""Minimal OpenClaw controller app wrapper for embedded messaging routes."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from code_index.openclaw_messaging.adapter_registry import AdapterRegistry
from code_index.openclaw_messaging.routes import ApiResponse
from code_index.openclaw_messaging.routes import MessagingRouter
from code_index.openclaw_messaging.routes import Principal
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_controller.scheduler import FleetController
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore


@dataclass
class OpenClawControllerApp:
    store: MessagingStore
    router: MessagingRouter
    fleet_controller: FleetController | None = None

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
        return self.router.handle(
            method,
            path,
            body,
            headers=headers,
            principal=principal,
        )

    def close(self) -> None:
        self.store.close()


def create_app(
    db_path: str | Path,
    *,
    signing_secret: str,
    telegram_secret_token: str | None = None,
    register_default_adapters: bool = True,
    lease_store: Any | None = None,
    nats_client: Any | None = None,
    fleet_controller: FleetController | None = None,
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
    return OpenClawControllerApp(
        store=store,
        router=MessagingRouter(store, telegram_secret_token=telegram_secret_token),
        fleet_controller=fleet_controller,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenClaw controller embedded messaging route dispatcher."
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
        default=os.environ.get("OPENCLAW_CONTROLLER_SIGNING_SECRET"),
        help="Command reference signing secret. May also use OPENCLAW_CONTROLLER_SIGNING_SECRET.",
    )
    parser.add_argument(
        "--telegram-secret-token",
        default=os.environ.get("OPENCLAW_TELEGRAM_SECRET_TOKEN"),
        help="Telegram webhook secret token. May also use OPENCLAW_TELEGRAM_SECRET_TOKEN.",
    )
    args = parser.parse_args(argv)
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


def _is_fleet_path(path: str) -> bool:
    parts = [part for part in urlsplit(path).path.strip("/").split("/") if part]
    return bool(parts and parts[0] == "fleet")


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError("expected object")


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
