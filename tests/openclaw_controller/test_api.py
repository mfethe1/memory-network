from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_controller.app import create_app
from code_index.openclaw_hostd import service
from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_messaging.routes import Principal


SIGNING_SECRET = "test-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
INGEST_PRINCIPAL = Principal(
    principal_id="host-a",
    scopes=frozenset({"fleet:ingest"}),
)
HOST_A_INGEST_PRINCIPAL = Principal(
    principal_id="host-a",
    scopes=frozenset({"host:ingest"}),
)
FLEET_INGEST_PRINCIPAL = Principal(
    principal_id="fleet-service",
    scopes=frozenset({"fleet:ingest"}),
)
ASSIGN_PRINCIPAL = Principal(
    principal_id="controller",
    scopes=frozenset({"command:write"}),
)
HANDOFF_PRINCIPAL = Principal(
    principal_id="context-manager",
    scopes=frozenset({"fleet:handoff"}),
)


class FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, dict(payload)))


class BridgedNatsTransport:
    def __init__(self) -> None:
        self.connected = False
        self.subscriptions: dict[str, Any] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []

    def connect(self) -> None:
        self.connected = True

    def subscribe(self, subject: str, callback: Any) -> None:
        self.subscriptions[subject] = callback

    def publish(self, subject: str, payload: bytes) -> None:
        decoded = json.loads(payload.decode("utf-8"))
        self.published.append((subject, decoded))
        parts = subject.split(".")
        if (
            len(parts) == 4
            and parts[0] == "openclaw"
            and parts[1] == "task"
            and parts[3] == "assigned"
        ):
            delivery_subject = f"openclaw.deliver.{parts[2]}.tasks"
            callback = self.subscriptions.get(delivery_subject)
            if callback is not None:
                callback(decoded)

    def close(self) -> None:
        self.connected = False


class FakeGraphClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def submit_task(self, **payload: Any) -> GraphServerResponse:
        self.requests.append(dict(payload))
        return GraphServerResponse(
            ok=True,
            status_code=201,
            payload={"run": {"run_id": payload["run_id"]}},
        )


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
        heartbeat = app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )
        command_ref = _command_ref(app)

        assigned = app.handle_request(
            "POST",
            "/fleet/tasks",
            {
                "command_ref": command_ref,
                "provider": "malicious-body-value",
            },
            principal=ASSIGN_PRINCIPAL,
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
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )
        command_ref = _command_ref(app)

        rejected = app.handle_request(
            "POST",
            "/fleet/tasks",
            {"command_ref": command_ref},
            principal=ASSIGN_PRINCIPAL,
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
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )

        rejected = app.handle_request(
            "POST",
            "/fleet/tasks",
            {"command_ref": {"command_id": "cmd-unsigned"}},
            principal=ASSIGN_PRINCIPAL,
        )

        assert rejected.status_code == 403
        assert rejected.body["status"] == "rejected"
        assert rejected.body["rejection"]["reason"] == "invalid_command_ref"
    finally:
        app.close()


def test_fleet_write_routes_reject_untrusted_or_missing_principal(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=FakeNats(),
    )
    try:
        heartbeat_body = {
            **_heartbeat(),
            "principal": {
                "principal_id": "host-a",
                "scopes": ["fleet:ingest"],
            },
        }

        unauthenticated_heartbeat = app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            heartbeat_body,
        )
        wrong_scope_heartbeat = app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=ASSIGN_PRINCIPAL,
        )

        assert unauthenticated_heartbeat.status_code == 403
        assert wrong_scope_heartbeat.status_code == 403
    finally:
        app.close()


def test_host_scoped_ingest_principal_cannot_spoof_another_host(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=FakeNats(),
    )
    try:
        spoofed_heartbeat = app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat("host-b"),
            principal=HOST_A_INGEST_PRINCIPAL,
        )
        spoofed_agent_state = app.handle_request(
            "POST",
            "/fleet/agent-states",
            {
                "host_id": "host-b",
                "run_id": "run-b",
                "task_id": "task-b",
                "run_status": "working",
            },
            principal=HOST_A_INGEST_PRINCIPAL,
        )
        spoofed_run_event = app.handle_request(
            "POST",
            "/fleet/run-events",
            {
                "host_id": "host-b",
                "run_id": "run-b",
                "task_id": "task-b",
                "event_type": "tool_call",
            },
            principal=HOST_A_INGEST_PRINCIPAL,
        )

        fleet_heartbeat = app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat("host-b"),
            principal=FLEET_INGEST_PRINCIPAL,
        )
        fleet_agent_state = app.handle_request(
            "POST",
            "/fleet/agent-states",
            {
                "host_id": "host-b",
                "run_id": "run-b",
                "task_id": "task-b",
                "run_status": "working",
            },
            principal=FLEET_INGEST_PRINCIPAL,
        )
        fleet_run_event = app.handle_request(
            "POST",
            "/fleet/run-events",
            {
                "host_id": "host-b",
                "run_id": "run-b",
                "task_id": "task-b",
                "event_type": "tool_call",
            },
            principal=FLEET_INGEST_PRINCIPAL,
        )

        assert spoofed_heartbeat.status_code == 403
        assert spoofed_agent_state.status_code == 403
        assert spoofed_run_event.status_code == 403
        assert fleet_heartbeat.status_code == 200
        assert fleet_agent_state.status_code == 200
        assert fleet_run_event.status_code == 200
    finally:
        app.close()


def test_fleet_task_assignment_requires_trusted_assignment_scope(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=FakeNats(),
    )
    try:
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )
        command_ref = _command_ref(app)

        unauthenticated = app.handle_request(
            "POST",
            "/fleet/tasks",
            {
                "command_ref": command_ref,
                "principal": {
                    "principal_id": "controller",
                    "scopes": ["command:write"],
                },
            },
        )
        wrong_scope = app.handle_request(
            "POST",
            "/fleet/tasks",
            {"command_ref": command_ref},
            principal=INGEST_PRINCIPAL,
        )

        assert unauthenticated.status_code == 403
        assert wrong_scope.status_code == 403
    finally:
        app.close()


def test_handoff_route_requires_handoff_scope(tmp_path: Path) -> None:
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
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )
        body = {
            "handoff_id": "handoff-1",
            "host_id": "host-a",
            "task_id": "task-123",
            "run_id": "run-123",
            "repo_root": r"E:\Projects\repo-a",
            "provider": "codex",
        }

        unauthenticated = app.handle_request("POST", "/fleet/handoffs", body)
        wrong_scope = app.handle_request(
            "POST",
            "/fleet/handoffs",
            body,
            principal=ASSIGN_PRINCIPAL,
        )

        assert unauthenticated.status_code == 403
        assert wrong_scope.status_code == 403
    finally:
        app.close()


def test_controller_assignment_reaches_hostd_task_inbox_through_broker_bridge(
    tmp_path: Path,
) -> None:
    transport = BridgedNatsTransport()
    nats = NatsClient(transport=transport)
    leases = InMemoryFleetLeaseStore()
    graph = FakeGraphClient()
    config = HostDaemonConfig(
        state_dir=tmp_path / "host-state",
        host_identity_path=tmp_path / "host-state" / "host-id.json",
        repo_roots=(tmp_path,),
        graph_server_url="http://127.0.0.1:8767",
    )
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id="host-a"),
        nats_client=nats,
        graph_client=graph,
        lease_store=leases,
    )
    assert runtime is not None
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=leases,
        nats_client=nats,
    )
    try:
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )
        command_ref = _command_ref(app)

        response = app.handle_request(
            "POST",
            "/fleet/tasks",
            {"command_ref": command_ref},
            principal=ASSIGN_PRINCIPAL,
        )

        assert response.status_code == 202
        assert [subject for subject, _ in transport.published] == [
            "openclaw.task.host-a.assigned",
            "openclaw.task.host-a.ack",
        ]
        assert [request["task_id"] for request in graph.requests] == ["task-123"]
    finally:
        runtime.close()
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
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat(),
            principal=INGEST_PRINCIPAL,
        )
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
            principal=INGEST_PRINCIPAL,
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
            principal=HANDOFF_PRINCIPAL,
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
