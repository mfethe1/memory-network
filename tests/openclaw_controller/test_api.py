from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_controller.app import create_app
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_messaging.routes import Principal


SIGNING_SECRET = "test-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


class FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, dict(payload)))


def _heartbeat(host_id: str = "host-a") -> dict[str, Any]:
    return {
        "kind": "openclaw.host_heartbeat",
        "schema_version": 1,
        "host_id": host_id,
        "heartbeat_interval_seconds": 10,
        "capabilities": {
            "repo_roots": [{"path": r"E:\Projects\repo-a", "exists": True}],
            "providers": [
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "capabilities": ["task_run"],
                }
            ],
        },
    }


def _command_ref(app: Any, *, task_id: str = "task-123") -> dict[str, Any]:
    room = app.store.create_room(
        room_kind="task",
        display_name=f"Task {task_id}",
        task_id=task_id,
        metadata={
            "default_delivery_targets": [
                {"recipient_kind": "host", "recipient_id": "host-a"}
            ],
            "assignment": {
                "repo_root": r"E:\Projects\repo-a",
                "provider": "codex",
                "selected_paths": ["code_index/openclaw_controller/app.py"],
            },
        },
    )
    response = app.handle_request(
        "POST",
        "/messages",
        {
            "room_id": room["room_id"],
            "sender_kind": "human",
            "sender_id": "operator-1",
            "body": "Implement task.",
            "message_type": "command",
            "command_type": "assign_task",
            "target_scope": {"kind": "task", "task_id": task_id},
        },
        principal=Principal(
            principal_id="operator-1",
            scopes=frozenset({"message:write", "command:write"}),
        ),
    )
    assert response.status_code == 201
    return response.body["command_ref"]


def test_fleet_task_route_assigns_eligible_host_and_preserves_messaging_routes(
    tmp_path: Path,
) -> None:
    nats = FakeNats()
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=nats,
    )
    try:
        heartbeat = app.handle_request("POST", "/fleet/hosts/heartbeat", _heartbeat())
        command_ref = _command_ref(app)

        assigned = app.handle_request(
            "POST",
            "/fleet/tasks",
            {
                "command_ref": command_ref,
                "provider": "malicious-body-value",
            },
        )
        rooms = app.handle_request("GET", "/rooms")

        assert heartbeat.status_code == 200
        assert assigned.status_code == 202
        assert assigned.body["status"] == "assigned"
        assert assigned.body["assignment"]["host_id"] == "host-a"
        assert assigned.body["room_message_update"]["status"] == "assigned"
        assert nats.published[0][0] == "openclaw.task.host-a.assigned"
        assert nats.published[0][1]["provider"] == "codex"
        assert rooms.status_code == 200
        assert len(rooms.body["rooms"]) == 1
    finally:
        app.close()


def test_fleet_task_route_returns_rejected_assignment_shape_for_repo_lease_conflict(
    tmp_path: Path,
) -> None:
    leases = InMemoryFleetLeaseStore()
    leases.acquire_lease(
        "repo",
        r"E:\Projects\repo-a",
        owner_host_id="host-b",
        ttl_seconds=None,
        now=NOW,
    )
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=leases,
        nats_client=FakeNats(),
    )
    try:
        app.handle_request("POST", "/fleet/hosts/heartbeat", _heartbeat())
        command_ref = _command_ref(app)

        rejected = app.handle_request(
            "POST",
            "/fleet/tasks",
            {"command_ref": command_ref},
        )

        assert rejected.status_code == 409
        assert rejected.body["status"] == "rejected"
        assert rejected.body["assignment"] is None
        assert rejected.body["rejection"]["reason"] == "repo_lease_conflict"
        assert rejected.body["room_message_update"]["status"] == "rejected"
    finally:
        app.close()


def test_fleet_route_rejects_invalid_command_reference(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=FakeNats(),
    )
    try:
        app.handle_request("POST", "/fleet/hosts/heartbeat", _heartbeat())

        rejected = app.handle_request(
            "POST",
            "/fleet/tasks",
            {"command_ref": {"command_id": "cmd-unsigned"}},
        )

        assert rejected.status_code == 403
        assert rejected.body["status"] == "rejected"
        assert rejected.body["rejection"]["reason"] == "invalid_command_ref"
    finally:
        app.close()


def test_fleet_projection_exposes_context_health_and_handoff_state(
    tmp_path: Path,
) -> None:
    leases = InMemoryFleetLeaseStore()
    leases.acquire_lease(
        "repo",
        r"E:\Projects\repo-a",
        owner_host_id="host-a",
        ttl_seconds=None,
        now=NOW,
    )
    leases.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-123",
        ttl_seconds=None,
        now=NOW,
    )
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=leases,
        nats_client=FakeNats(),
    )
    try:
        app.handle_request("POST", "/fleet/hosts/heartbeat", _heartbeat())
        app.handle_request(
            "POST",
            "/fleet/context/health",
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-123",
                "health": "warning",
                "estimated_tokens": 76000,
            },
        )
        handoff = app.handle_request(
            "POST",
            "/fleet/handoffs",
            {
                "handoff_id": "handoff-1",
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-123",
                "repo_root": r"E:\Projects\repo-a",
                "provider": "codex",
                "reason": "context pressure",
            },
        )
        projection = app.handle_request("GET", "/fleet")

        assert handoff.status_code == 202
        run = projection.body["runs"][0]
        host = projection.body["hosts"][0]
        assert run["context_health"]["health"] == "warning"
        assert run["handoff_state"]["status"] == "authorized"
        assert host["context_health"]["run-123"]["estimated_tokens"] == 76000
        assert host["handoff_state"]["run-123"]["handoff_id"] == "handoff-1"
    finally:
        app.close()
