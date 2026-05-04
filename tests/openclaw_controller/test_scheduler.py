from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_controller.scheduler import FleetController
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_messaging.store import MessagingStore


SIGNING_SECRET = "test-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


class FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, dict(payload)))


def _store(tmp_path: Path) -> MessagingStore:
    return MessagingStore(tmp_path / "messages.db", signing_secret=SIGNING_SECRET)


def _controller(
    store: MessagingStore,
    *,
    lease_store: InMemoryFleetLeaseStore | None = None,
    nats: FakeNats | None = None,
) -> tuple[FleetController, FakeNats, InMemoryFleetLeaseStore]:
    leases = lease_store or InMemoryFleetLeaseStore()
    publisher = nats or FakeNats()
    return (
        FleetController(
            messaging_store=store,
            lease_store=leases,
            nats_client=publisher,
            restart_cooldown_seconds=90,
        ),
        publisher,
        leases,
    )


def _host(
    controller: FleetController,
    host_id: str,
    *,
    repo_root: str = r"E:\Projects\repo-a",
    providers: list[str] | None = None,
    now: datetime = NOW,
) -> None:
    controller.record_host_heartbeat(
        {
            "kind": "openclaw.host_heartbeat",
            "schema_version": 1,
            "host_id": host_id,
            "heartbeat_interval_seconds": 10,
            "generated_at": now.isoformat(),
            "capabilities": {
                "repo_roots": [{"path": repo_root, "exists": True}],
                "providers": [
                    {
                        "id": provider,
                        "display_name": provider,
                        "capabilities": ["task_run"],
                    }
                    for provider in (providers or ["codex"])
                ],
            },
        },
        now=now,
    )


def _command(
    store: MessagingStore,
    *,
    task_id: str = "task-123",
    host_id: str = "host-a",
    body: str = "Implement the task.",
    repo_root: str = r"E:\Projects\repo-a",
    provider: str = "codex",
    expires_at: str | None = None,
) -> dict[str, Any]:
    room = store.create_room(
        room_kind="task",
        display_name=f"Task {task_id}",
        task_id=task_id,
        metadata={
            "default_delivery_targets": [
                {"recipient_kind": "host", "recipient_id": host_id}
            ],
            "assignment": {
                "repo_root": repo_root,
                "provider": provider,
                "selected_paths": ["code_index/openclaw_controller/app.py"],
                "selected_nodes": ["openclaw_controller.app"],
            },
        },
    )
    result = store.create_message(
        room_id=room["room_id"],
        sender_kind="human",
        sender_id="operator-1",
        body=body,
        message_type="command",
        command_type="assign_task",
        target_scope={"kind": "task", "task_id": task_id},
        expires_at=expires_at,
    )
    return result["command_ref"]


def test_controller_assigns_task_only_to_eligible_host_and_publishes_nats_shape(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, leases = _controller(store)
        _host(controller, "host-a")
        _host(controller, "host-b", providers=["kimi"])
        command_ref = _command(store)

        result = controller.assign_task_from_command_ref(command_ref, now=NOW)

        assert result.status == "assigned"
        assert result.assignment is not None
        assert result.assignment.host_id == "host-a"
        assert result.room_message_update["message_id"] == command_ref["message_id"]
        assert result.room_message_update["status"] == "assigned"
        assert leases.get_active_lease("repo", r"E:\Projects\repo-a", now=NOW)
        assert leases.get_active_lease("task", "task-123", now=NOW)
        assert nats.published == [
            (
                "openclaw.deliver.host-a.tasks",
                {
                    "kind": "openclaw.task.assigned",
                    "schema_version": 1,
                    "host_id": "host-a",
                    "task_id": "task-123",
                    "message_id": command_ref["message_id"],
                    "delivery_id": store.list_deliveries(command_ref["message_id"])[0][
                        "delivery_id"
                    ],
                    "message": "Implement the task.",
                    "repo_root": r"E:\Projects\repo-a",
                    "provider": "codex",
                    "selected_paths": ["code_index/openclaw_controller/app.py"],
                    "selected_nodes": ["openclaw_controller.app"],
                },
            )
        ]
    finally:
        store.close()


def test_controller_refuses_assignment_when_repo_lease_is_held_elsewhere(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, leases = _controller(store)
        _host(controller, "host-a")
        leases.acquire_lease(
            "repo",
            r"E:\Projects\repo-a",
            owner_host_id="host-b",
            now=NOW - timedelta(minutes=1),
        )
        command_ref = _command(store)

        result = controller.assign_task_from_command_ref(command_ref, now=NOW)

        assert result.status == "rejected"
        assert result.rejection is not None
        assert result.rejection.reason == "repo_lease_conflict"
        assert result.room_message_update["status"] == "rejected"
        assert nats.published == []
        assert leases.get_active_lease("task", "task-123", now=NOW) is None
    finally:
        store.close()


def test_controller_rejects_unsigned_expired_or_invalid_command_references(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, _leases = _controller(store)
        _host(controller, "host-a")
        valid = _command(store)
        unsigned = {key: value for key, value in valid.items() if key != "signed_payload"}
        expired = _command(
            store,
            task_id="task-expired",
            body="Expired task.",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        tampered = dict(valid)
        signed = json.loads(tampered["signed_payload"])
        signed["payload"]["message_id"] = "msg-wrong"
        tampered["signed_payload"] = json.dumps(signed)

        results = [
            controller.assign_task_from_command_ref(command_ref, now=NOW)
            for command_ref in (unsigned, expired, tampered)
        ]

        assert [result.status for result in results] == [
            "rejected",
            "rejected",
            "rejected",
        ]
        assert [result.rejection.reason for result in results if result.rejection] == [
            "invalid_command_ref",
            "invalid_command_ref",
            "invalid_command_ref",
        ]
        assert nats.published == []
    finally:
        store.close()


def test_health_projection_marks_run_stale_without_mutating_agent_run_status(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, _nats, leases = _controller(store)
        stale_action = NOW - timedelta(minutes=15)
        controller.record_run_event(
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-123",
                "status": "working",
                "event_type": "run_status",
                "generated_at": stale_action.isoformat(),
            },
            now=stale_action,
        )
        controller.record_agent_state(
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-123",
                "last_action_at": stale_action.isoformat(),
            },
            now=stale_action,
        )
        leases.record_task_status(
            "task-123",
            status="working",
            host_id="host-a",
            run_id="run-123",
            now=stale_action,
        )

        projection = controller.project_fleet(now=NOW)

        run = projection["runs"][0]
        task = leases.get_task_record("task-123")
        assert run["run_health"] == "stale"
        assert run["agent_run_status"] == "working"
        assert task is not None
        assert task.status == "working"
    finally:
        store.close()


def test_handoff_restart_requires_valid_leases_and_restart_cooldown(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, _nats, leases = _controller(store)
        _host(controller, "host-a")
        proposal = {
            "handoff_id": "handoff-1",
            "host_id": "host-a",
            "task_id": "task-123",
            "run_id": "run-123",
            "repo_root": r"E:\Projects\repo-a",
            "provider": "codex",
            "reason": "context pressure",
        }

        missing_lease = controller.submit_handoff_proposal(proposal, now=NOW)

        repo_lease = leases.acquire_lease(
            "repo",
            r"E:\Projects\repo-a",
            owner_host_id="host-a",
            now=NOW,
        )
        task_lease = leases.acquire_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-123",
            now=NOW,
        )
        authorized = controller.submit_handoff_proposal(
            {**proposal, "handoff_id": "handoff-2"},
            now=NOW + timedelta(seconds=1),
        )
        cooldown = controller.submit_handoff_proposal(
            {**proposal, "handoff_id": "handoff-3"},
            now=NOW + timedelta(seconds=30),
        )
        leases.release_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-123",
            fencing_revision=task_lease.fencing_revision,
            now=NOW + timedelta(seconds=91),
        )
        leases.release_lease(
            "repo",
            r"E:\Projects\repo-a",
            owner_host_id="host-a",
            fencing_revision=repo_lease.fencing_revision,
            now=NOW + timedelta(seconds=91),
        )
        invalid_after_release = controller.submit_handoff_proposal(
            {**proposal, "handoff_id": "handoff-4"},
            now=NOW + timedelta(seconds=120),
        )

        assert missing_lease.status == "rejected"
        assert missing_lease.rejection is not None
        assert missing_lease.rejection.reason == "lease_invalid"
        assert authorized.status == "authorized"
        assert authorized.handoff is not None
        assert cooldown.status == "rejected"
        assert cooldown.rejection is not None
        assert cooldown.rejection.reason == "restart_cooldown_active"
        assert invalid_after_release.status == "rejected"
        assert invalid_after_release.rejection is not None
        assert invalid_after_release.rejection.reason == "lease_invalid"
    finally:
        store.close()
