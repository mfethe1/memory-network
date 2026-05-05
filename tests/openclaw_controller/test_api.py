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
TELEGRAM_SECRET = "telegram-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
LENNY_HOST_ID = "host_6a163e09f5744561a0827d30253b3ba8"
ROSIE_HOST_ID = "host_a23037f43daa41b19d1d441ec514af33"
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


def _telegram_update(
    *,
    update_id: int = 100,
    message_id: int = 200,
    text: str = "/assign task-123 Implement the task.",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": -100123, "type": "group", "title": "OpenClaw Fleet"},
            "from": {"id": 42, "username": "operator", "first_name": "Operator"},
            "text": text,
        },
    }


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


def test_single_telegram_chat_assigns_task_to_eligible_host_without_predelegation(
    tmp_path: Path,
) -> None:
    nats = FakeNats()
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        telegram_secret_token=TELEGRAM_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=nats,
    )
    try:
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat("host-a"),
            principal=FLEET_INGEST_PRINCIPAL,
        )
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat("host-b"),
            principal=FLEET_INGEST_PRINCIPAL,
        )
        app.store.set_adapter_command_promotion("telegram", enabled=True)
        fleet_room = app.store.create_room(
            room_kind="fleet",
            display_name="OpenClaw Fleet",
            metadata={
                "assignment": {
                    "repo_root": r"E:\Projects\repo-a",
                    "provider": "codex",
                    "selected_paths": ["code_index/openclaw_controller/app.py"],
                }
            },
        )
        app.store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=fleet_room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        app.store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        response = app.handle_request(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

        assert response.status_code == 201
        assert response.body["message"]["target_scope"] == {
            "kind": "task",
            "task_id": "task-123",
        }
        assert response.body["command_ref"]["command_type"] == "assign_task"
        assert response.body["auto_assignment"]["status"] == "assigned"
        assert response.body["auto_assignment"]["assignment"]["host_id"] == "host-a"
        assert [subject for subject, _payload in nats.published] == [
            "openclaw.task.host-a.assigned"
        ]
        assert nats.published[0][1]["message"] == "Implement the task."
        assert [
            (delivery["recipient_kind"], delivery["recipient_id"])
            for delivery in response.body["auto_assignment"]["deliveries"]
        ] == [("host", "host-a")]
    finally:
        app.close()


def test_host_task_claim_route_assigns_the_claimant_for_untagged_telegram_work(
    tmp_path: Path,
) -> None:
    nats = FakeNats()
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        telegram_secret_token=TELEGRAM_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=nats,
    )
    try:
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat("host-a"),
            principal=FLEET_INGEST_PRINCIPAL,
        )
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            _heartbeat("host-z"),
            principal=FLEET_INGEST_PRINCIPAL,
        )
        fleet_room = app.store.create_room(
            room_kind="fleet",
            display_name="OpenClaw Fleet",
            metadata={
                "assignment": {
                    "repo_root": r"E:\Projects\repo-a",
                    "provider": "codex",
                    "selected_paths": ["code_index/openclaw_controller/app.py"],
                }
            },
        )
        app.store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=fleet_room["room_id"],
        )
        message_response = app.handle_request(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(text="please check my email"),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )
        message_id = message_response.body["message"]["message_id"]

        spoofed = app.handle_request(
            "POST",
            "/fleet/task-claims",
            {
                "message_id": message_id,
                "claimant_host_id": "host-a",
            },
            principal=Principal(
                principal_id="host-z",
                scopes=frozenset({"host:ingest"}),
            ),
        )
        claimed = app.handle_request(
            "POST",
            "/fleet/task-claims",
            {"message_id": message_id},
            principal=Principal(
                principal_id="host-z",
                scopes=frozenset({"host:ingest"}),
            ),
        )

        assert message_response.status_code == 201
        assert message_response.body["command_ref"] is None
        assert spoofed.status_code == 403
        assert claimed.status_code == 202
        assert claimed.body["status"] == "assigned"
        assert claimed.body["assignment"]["host_id"] == "host-z"
        assert claimed.body["assignment"]["task_id"] == f"telegram-msg:{message_id}"
        assert [subject for subject, _payload in nats.published] == [
            "openclaw.task.host-z.assigned"
        ]
    finally:
        app.close()


def test_telegram_poll_route_uses_same_assignment_path_as_webhook(
    tmp_path: Path,
) -> None:
    nats = FakeNats()
    http_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_telegram_poll(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        http_calls.append((url, dict(payload)))
        return {
            "ok": True,
            "result": [
                _telegram_update(
                    update_id=101,
                    message_id=201,
                    text="/assign task-123 @lenny repair the inbox test",
                )
            ],
        }

    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        telegram_secret_token=TELEGRAM_SECRET,
        telegram_bot_token="telegram-bot-token",
        telegram_http_client=fake_telegram_poll,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=nats,
    )
    try:
        app.handle_request(
            "POST",
            "/fleet/hosts/heartbeat",
            {**_heartbeat("host-z"), "host_aliases": ["lenny"]},
            principal=FLEET_INGEST_PRINCIPAL,
        )
        app.store.set_adapter_command_promotion("telegram", enabled=True)
        task_room = app.store.create_room(
            room_kind="task",
            display_name="Task 123",
            task_id="task-123",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-z"}
                ],
                "assignment": {
                    "repo_root": r"E:\Projects\repo-a",
                    "provider": "codex",
                    "selected_paths": ["code_index/openclaw_controller/app.py"],
                },
            },
        )
        app.store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=task_room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        app.store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        response = app.handle_request(
            "POST",
            "/adapters/telegram/poll",
            {"persist_update_offset": True},
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

        assert response.status_code == 200
        assert http_calls == [
            (
                "https://api.telegram.org/bottelegram-bot-token/getUpdates",
                {"timeout": 0},
            )
        ]
        assert response.body["results"][0]["message"]["body"] == (
            "/assign task-123 @lenny repair the inbox test"
        )
        assert response.body["auto_assignments"][0]["status"] == "assigned"
        assert response.body["auto_assignments"][0]["assignment"]["host_id"] == "host-z"
        assert response.body["cursor"]["cursor_value"] == "102"
        assert [subject for subject, _payload in nats.published] == [
            "openclaw.task.host-z.assigned"
        ]
    finally:
        app.close()


def test_telegram_poll_route_requires_trusted_adapter_access(tmp_path: Path) -> None:
    def fake_telegram_poll(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("unauthorized poll should not reach Telegram")

    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        telegram_secret_token=TELEGRAM_SECRET,
        telegram_bot_token="telegram-bot-token",
        telegram_http_client=fake_telegram_poll,
    )
    try:
        response = app.handle_request(
            "POST",
            "/adapters/telegram/poll",
            {"persist_update_offset": True},
        )

        assert response.status_code == 403
        assert response.body["error"] == "Telegram poll requires trusted adapter access"
    finally:
        app.close()


def test_unified_telegram_routes_lenny_and_rosie_aliases_to_distinct_hosts(
    tmp_path: Path,
) -> None:
    nats = FakeNats()
    lease_store = InMemoryFleetLeaseStore()
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        telegram_secret_token=TELEGRAM_SECRET,
        lease_store=lease_store,
        nats_client=nats,
    )
    try:
        for host_id, alias in (("host-lenny", "lenny"), ("host-rosie", "rosie")):
            response = app.handle_request(
                "POST",
                "/fleet/hosts/heartbeat",
                {
                    **_heartbeat(host_id),
                    "host_aliases": [alias],
                    "capabilities": {
                        **_heartbeat(host_id)["capabilities"],
                        "repo_roots": [
                            {"path": r"E:\Projects\openclaw", "exists": True}
                        ],
                    },
                },
                principal=FLEET_INGEST_PRINCIPAL,
            )
            assert response.status_code == 200

        app.store.set_adapter_command_promotion("telegram", enabled=True)
        fleet_room = app.store.create_room(
            room_kind="fleet",
            display_name="OpenClaw Fleet",
            metadata={
                "assignment": {
                    "repo_root": r"E:\Projects\openclaw",
                    "provider": "codex",
                    "selected_paths": ["."],
                }
            },
        )
        app.store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=fleet_room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        app.store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        lenny = app.handle_request(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(
                update_id=610,
                message_id=710,
                text="@lenny summarize repo status",
            ),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )
        repo_lease = lease_store.get_active_lease("repo", r"E:\Projects\openclaw")
        assert repo_lease is not None
        lease_store.release_lease(
            "repo",
            r"E:\Projects\openclaw",
            owner_host_id="host-lenny",
            fencing_revision=repo_lease.fencing_revision,
        )
        rosie = app.handle_request(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(
                update_id=611,
                message_id=711,
                text="@rosie check graph server health",
            ),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

        assert lenny.status_code == 201
        assert rosie.status_code == 201
        assert lenny.body["auto_assignment"]["assignment"]["host_id"] == "host-lenny"
        assert rosie.body["auto_assignment"]["assignment"]["host_id"] == "host-rosie"
        assert lenny.body["auto_assignment"]["assignment"]["task_id"].startswith(
            "telegram-msg:"
        )
        assert rosie.body["auto_assignment"]["assignment"]["task_id"].startswith(
            "telegram-msg:"
        )
        assert [subject for subject, _payload in nats.published] == [
            "openclaw.task.host-lenny.assigned",
            "openclaw.task.host-rosie.assigned",
        ]
    finally:
        app.close()


def test_unified_telegram_assign_routes_production_aliases_to_stable_host_ids(
    tmp_path: Path,
) -> None:
    nats = FakeNats()
    lease_store = InMemoryFleetLeaseStore()
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        telegram_secret_token=TELEGRAM_SECRET,
        lease_store=lease_store,
        nats_client=nats,
    )
    try:
        for host_id, alias in ((LENNY_HOST_ID, "lenny"), (ROSIE_HOST_ID, "rosie")):
            response = app.handle_request(
                "POST",
                "/fleet/hosts/heartbeat",
                {
                    **_heartbeat(host_id),
                    "host_aliases": [alias],
                    "capabilities": {
                        **_heartbeat(host_id)["capabilities"],
                        "repo_roots": [
                            {"path": r"E:\Projects\openclaw", "exists": True}
                        ],
                    },
                },
                principal=FLEET_INGEST_PRINCIPAL,
            )
            assert response.status_code == 200

        app.store.set_adapter_command_promotion("telegram", enabled=True)
        fleet_room = app.store.create_room(
            room_kind="fleet",
            display_name="OpenClaw Fleet",
            metadata={
                "assignment": {
                    "repo_root": r"E:\Projects\openclaw",
                    "provider": "codex",
                    "selected_paths": ["."],
                }
            },
        )
        app.store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=fleet_room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        app.store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        lenny = app.handle_request(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(
                update_id=620,
                message_id=720,
                text="/assign task-lenny-smoke @lenny summarize the local repo status",
            ),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )
        repo_lease = lease_store.get_active_lease("repo", r"E:\Projects\openclaw")
        assert repo_lease is not None
        lease_store.release_lease(
            "repo",
            r"E:\Projects\openclaw",
            owner_host_id=LENNY_HOST_ID,
            fencing_revision=repo_lease.fencing_revision,
        )
        rosie = app.handle_request(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(
                update_id=621,
                message_id=721,
                text="/assign task-rosie-smoke @rosie check the graph server health",
            ),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

        assert lenny.status_code == 201
        assert rosie.status_code == 201
        assert lenny.body["auto_assignment"]["assignment"]["host_id"] == LENNY_HOST_ID
        assert rosie.body["auto_assignment"]["assignment"]["host_id"] == ROSIE_HOST_ID
        assert lenny.body["auto_assignment"]["assignment"]["task_id"] == (
            "task-lenny-smoke"
        )
        assert rosie.body["auto_assignment"]["assignment"]["task_id"] == (
            "task-rosie-smoke"
        )
        assert [subject for subject, _payload in nats.published] == [
            f"openclaw.task.{LENNY_HOST_ID}.assigned",
            f"openclaw.task.{ROSIE_HOST_ID}.assigned",
        ]
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


def test_controller_nats_callbacks_persist_heartbeat_capabilities_and_ack_events(
    tmp_path: Path,
) -> None:
    transport = BridgedNatsTransport()
    nats = NatsClient(transport=transport)
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        nats_client=nats,
        lease_store=InMemoryFleetLeaseStore(),
    )
    try:
        command_ref = _command_ref(app)
        task_delivery = app.store.list_deliveries(command_ref["message_id"])[0]
        host_room = app.store.create_room(
            room_kind="host",
            display_name="Host A Inbox",
            host_id="host-a",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"}
                ]
            },
        )
        host_message = app.store.create_message(
            room_id=host_room["room_id"],
            sender_kind="controller",
            sender_id="controller",
            body="FYI",
            target_scope={"kind": "host", "host_id": "host-a"},
        )["message"]
        host_delivery = app.store.list_deliveries(host_message["message_id"])[0]

        transport.subscriptions["openclaw.host.*.heartbeat"](_heartbeat("host-a"))
        transport.subscriptions["openclaw.host.*.capabilities"](
            {
                "kind": "openclaw.host_capabilities",
                "schema_version": 1,
                "host_id": "host-a",
                "capabilities": {
                    "repo_roots": [{"path": r"E:\Projects\repo-a", "exists": True}],
                    "providers": [
                        {
                            "id": "kimi",
                            "display_name": "Kimi",
                            "capabilities": ["task_run"],
                        }
                    ],
                },
            }
        )
        transport.subscriptions["openclaw.task.*.ack"](
            {
                "kind": "openclaw.task_ack",
                "schema_version": 1,
                "host_id": "host-a",
                "task_id": "task-123",
                "message_id": command_ref["message_id"],
                "delivery_id": task_delivery["delivery_id"],
                "status": "accepted",
                "run_id": "run-123",
            }
        )
        transport.subscriptions["openclaw.host.*.messages.ack"](
            {
                "kind": "openclaw.message_delivery_ack",
                "schema_version": 1,
                "host_id": "host-a",
                "message_id": host_message["message_id"],
                "delivery_id": host_delivery["delivery_id"],
                "status": "acked",
            }
        )

        projection = app.handle_request("GET", "/fleet")
        host = projection.body["hosts"][0]
        acked_task_delivery = app.store.list_deliveries(command_ref["message_id"])[0]
        acked_host_delivery = app.store.list_deliveries(host_message["message_id"])[0]

        assert transport.connected is True
        assert set(transport.subscriptions) >= {
            "openclaw.host.*.heartbeat",
            "openclaw.host.*.capabilities",
            "openclaw.task.*.ack",
            "openclaw.host.*.messages.ack",
        }
        assert host["host_id"] == "host-a"
        assert [provider["id"] for provider in host["providers"]] == ["kimi"]
        assert acked_task_delivery["delivery_status"] == "acked"
        assert acked_task_delivery["metadata"]["task_ack"]["status"] == "accepted"
        assert acked_task_delivery["metadata"]["task_ack"]["run_id"] == "run-123"
        assert acked_host_delivery["delivery_status"] == "acked"
        assert acked_host_delivery["metadata"]["host_message_ack"]["host_id"] == (
            "host-a"
        )
    finally:
        app.close()


def test_host_scoped_principal_cannot_ack_another_hosts_delivery(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "messages.db",
        signing_secret=SIGNING_SECRET,
        lease_store=InMemoryFleetLeaseStore(),
        nats_client=FakeNats(),
    )
    try:
        host_room = app.store.create_room(
            room_kind="host",
            display_name="Host B Inbox",
            host_id="host-b",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-b"}
                ]
            },
        )
        message = app.store.create_message(
            room_id=host_room["room_id"],
            sender_kind="controller",
            sender_id="controller",
            body="FYI",
            target_scope={"kind": "host", "host_id": "host-b"},
        )["message"]
        delivery = app.store.list_deliveries(message["message_id"])[0]

        spoofed = app.handle_request(
            "POST",
            "/fleet/messages/ack",
            {
                "host_id": "host-a",
                "message_id": message["message_id"],
                "delivery_id": delivery["delivery_id"],
                "status": "acked",
            },
            principal=HOST_A_INGEST_PRINCIPAL,
        )
        valid = app.handle_request(
            "POST",
            "/fleet/messages/ack",
            {
                "host_id": "host-b",
                "message_id": message["message_id"],
                "delivery_id": delivery["delivery_id"],
                "status": "acked",
            },
            principal=Principal(
                principal_id="host-b",
                scopes=frozenset({"host:ingest"}),
            ),
        )

        assert spoofed.status_code == 400
        assert valid.status_code == 200
        assert valid.body["delivery"]["recipient_id"] == "host-b"
        assert valid.body["delivery"]["delivery_status"] == "acked"
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
