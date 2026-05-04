from __future__ import annotations

import json
from pathlib import Path
import threading
from urllib.request import urlopen

import pytest

from code_index.openclaw_controller.app import _ControllerHTTPServer
from code_index.openclaw_controller.app import OpenClawControllerService
from code_index.openclaw_controller.app import build_service_runtime
from code_index.openclaw_controller.service_config import (
    OPENCLAW_CONTROLLER_DB_PATH_ENV,
    OPENCLAW_MESSAGING_DB_PATH_ENV,
    OpenClawConfigError,
    resolve_service_paths,
)


def _development_env(tmp_path: Path) -> dict[str, str]:
    return {
        "OPENCLAW_DEPLOYMENT_MODE": "development",
        "OPENCLAW_CONTROLLER_SIGNING_SECRET": "service-signing-secret",
        OPENCLAW_CONTROLLER_DB_PATH_ENV: str(tmp_path / "controller.db"),
        OPENCLAW_MESSAGING_DB_PATH_ENV: str(tmp_path / "messaging.db"),
        "OPENCLAW_CONTEXT_STORE_PATH": str(tmp_path / "context.db"),
        "OPENCLAW_REQUIRE_NATS": "0",
        "PORT": "8123",
    }


def test_resolve_service_paths_defaults_to_railway_volume_mount(tmp_path: Path) -> None:
    mount = tmp_path / "railway-volume"
    mount.mkdir()

    paths = resolve_service_paths(
        {
            "OPENCLAW_DEPLOYMENT_MODE": "railway",
            "RAILWAY_VOLUME_MOUNT_PATH": str(mount),
        }
    )

    assert paths.messaging_db_path == mount / "openclaw" / "messaging.db"
    assert paths.controller_db_path == mount / "openclaw" / "controller-state.db"
    assert paths.context_store_path == mount / "openclaw" / "context-store.db"
    assert paths.messaging_db_path.parent.is_dir()


def test_build_service_runtime_requires_signing_secret(tmp_path: Path) -> None:
    with pytest.raises(OpenClawConfigError):
        build_service_runtime(
            environ={
                "OPENCLAW_DEPLOYMENT_MODE": "development",
                OPENCLAW_CONTROLLER_DB_PATH_ENV: str(tmp_path / "controller.db"),
                OPENCLAW_MESSAGING_DB_PATH_ENV: str(tmp_path / "messaging.db"),
                "OPENCLAW_CONTEXT_STORE_PATH": str(tmp_path / "context.db"),
            }
        )


def test_build_service_runtime_rejects_memory_path_in_railway_mode(
    tmp_path: Path,
) -> None:
    mount = tmp_path / "railway-volume"
    mount.mkdir()

    with pytest.raises(OpenClawConfigError):
        build_service_runtime(
            environ={
                "OPENCLAW_DEPLOYMENT_MODE": "railway",
                "RAILWAY_VOLUME_MOUNT_PATH": str(mount),
                "OPENCLAW_CONTROLLER_SIGNING_SECRET": "service-signing-secret",
                OPENCLAW_MESSAGING_DB_PATH_ENV: ":memory:",
            }
        )


def test_service_health_and_readiness_report_degraded_nats_without_secrets(
    tmp_path: Path,
) -> None:
    env = _development_env(tmp_path)
    env.update(
        {
            "OPENCLAW_REQUIRE_NATS": "1",
            "OPENCLAW_NATS_URL": "nats://nats-user:super-pass@example.invalid:4222",
        }
    )
    runtime = build_service_runtime(environ=env)
    try:
        health = runtime.health_payload()
        status_code, ready = runtime.readiness_payload()
        serialized = json.dumps({"health": health, "ready": ready}, sort_keys=True)

        assert health["status"] == "degraded"
        assert health["checks"]["nats"]["configured"] is True
        assert health["checks"]["nats"]["reachable"] is False
        assert health["checks"]["nats"]["url"] == "nats://example.invalid:4222"
        assert "service-signing-secret" not in serialized
        assert "super-pass" not in serialized
        assert status_code == 503
        assert ready["ready"] is False
        assert "nats_unreachable" in ready["reasons"]
    finally:
        runtime.close()


def test_controller_service_routes_health_ready_messaging_and_fleet(
    tmp_path: Path,
) -> None:
    runtime = build_service_runtime(environ=_development_env(tmp_path))
    service = OpenClawControllerService(runtime)
    try:
        room = runtime.app.store.create_room(
            room_kind="fleet",
            display_name="OpenClaw Fleet",
        )

        health = service.handle_http_request("GET", "/health")
        ready = service.handle_http_request("GET", "/ready")
        rooms = service.handle_http_request("GET", "/rooms")
        heartbeat = service.handle_http_request(
            "POST",
            "/fleet/hosts/heartbeat",
            body={
                "host_id": "host-a",
                "heartbeat_interval_seconds": 10,
                "capabilities": {
                    "repo_roots": [{"path": r"E:\Repos\repo-a", "exists": True}],
                    "providers": [
                        {
                            "id": "codex",
                            "display_name": "Codex",
                            "capabilities": ["task_run"],
                        }
                    ],
                },
            },
            headers={
                "X-OpenClaw-Principal-Id": "fleet-service",
                "X-OpenClaw-Principal-Scopes": "fleet:ingest",
            },
        )

        assert health.status_code == 200
        assert ready.status_code == 200
        assert rooms.status_code == 200
        assert rooms.body["rooms"][0]["room_id"] == room["room_id"]
        assert heartbeat.status_code == 200
        assert heartbeat.body["host"]["host_id"] == "host-a"
    finally:
        runtime.close()


def test_controller_http_server_can_read_sqlite_stores_from_request_thread(
    tmp_path: Path,
) -> None:
    runtime = build_service_runtime(environ=_development_env(tmp_path))
    service = OpenClawControllerService(runtime)
    runtime.app.store.create_room(
        room_kind="fleet",
        display_name="OpenClaw Fleet",
    )
    server = _ControllerHTTPServer(
        ("127.0.0.1", 0),
        service=service,
        use_ipv6=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(f"{base_url}/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/rooms", timeout=5) as response:
            rooms = json.loads(response.read().decode("utf-8"))

        assert health["status"] == "ok"
        assert rooms["rooms"][0]["display_name"] == "OpenClaw Fleet"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        runtime.close()
