from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_controller.scheduler import FleetController
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_hostd.leases import LeaseConflictError
from code_index.openclaw_messaging.store import MessagingStore


SIGNING_SECRET = "test-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


class FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, dict(payload)))


class RaceLeaseStore(InMemoryFleetLeaseStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_task_acquire = True

    def acquire_lease(self, scope: str, resource_id: str, **kwargs: Any) -> Any:
        if scope == "task" and self.fail_task_acquire:
            self.fail_task_acquire = False
            raise LeaseConflictError("task lease conflict for raced task")
        return super().acquire_lease(scope, resource_id, **kwargs)


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


def _host_with_capabilities(
    controller: FleetController,
    host_id: str,
    *,
    provider_id: str = "codex",
    capabilities: list[str],
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
                "repo_roots": [{"path": r"E:\Projects\repo-a", "exists": True}],
                "providers": [
                    {
                        "id": provider_id,
                        "display_name": provider_id,
                        "capabilities": capabilities,
                    }
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
    required_provider_capabilities: list[str] | None = None,
    expires_at: str | None = None,
    recipients: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    assignment = {
        "repo_root": repo_root,
        "provider": provider,
        "selected_paths": ["code_index/openclaw_controller/app.py"],
        "selected_nodes": ["openclaw_controller.app"],
    }
    if required_provider_capabilities is not None:
        assignment["required_provider_capabilities"] = required_provider_capabilities
    room = store.create_room(
        room_kind="task",
        display_name=f"Task {task_id}",
        task_id=task_id,
        metadata={
            "default_delivery_targets": recipients
            if recipients is not None
            else [{"recipient_kind": "host", "recipient_id": host_id}],
            "assignment": assignment,
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
                "openclaw.task.host-a.assigned",
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


def test_repo_lease_is_released_when_task_lease_acquisition_races(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        leases = RaceLeaseStore()
        controller, nats, _leases = _controller(store, lease_store=leases)
        _host(controller, "host-a")
        command_ref = _command(store)

        result = controller.assign_task_from_command_ref(command_ref, now=NOW)

        assert result.status == "rejected"
        assert result.rejection is not None
        assert result.rejection.reason == "task_lease_conflict"
        assert leases.get_active_lease("repo", r"E:\Projects\repo-a", now=NOW) is None
        assert nats.published == []
    finally:
        store.close()


def test_host_selection_skips_eligible_host_without_delivery_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, _leases = _controller(store)
        _host(controller, "host-a")
        _host(controller, "host-b")
        command_ref = _command(
            store,
            host_id="host-b",
            recipients=[{"recipient_kind": "host", "recipient_id": "host-b"}],
        )

        result = controller.assign_task_from_command_ref(command_ref, now=NOW)

        assert result.status == "assigned"
        assert result.assignment is not None
        assert result.assignment.host_id == "host-b"
        assert nats.published[0][0] == "openclaw.task.host-b.assigned"
    finally:
        store.close()


def test_required_provider_capabilities_skip_same_provider_host_without_capability(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, _leases = _controller(store)
        _host_with_capabilities(
            controller,
            "host-a",
            capabilities=["chat"],
        )
        _host_with_capabilities(
            controller,
            "host-b",
            capabilities=["task_run", "fresh_session"],
        )
        command_ref = _command(
            store,
            host_id="host-b",
            required_provider_capabilities=["task_run", "fresh_session"],
        )

        result = controller.assign_task_from_command_ref(command_ref, now=NOW)

        assert result.status == "assigned"
        assert result.assignment is not None
        assert result.assignment.host_id == "host-b"
        assert nats.published[0][0] == "openclaw.task.host-b.assigned"
    finally:
        store.close()


def test_assignment_rejects_when_provider_id_matches_but_capability_is_missing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, _leases = _controller(store)
        _host_with_capabilities(
            controller,
            "host-a",
            capabilities=["chat"],
        )
        command_ref = _command(
            store,
            required_provider_capabilities=["task_run"],
        )

        result = controller.assign_task_from_command_ref(command_ref, now=NOW)

        assert result.status == "rejected"
        assert result.rejection is not None
        assert result.rejection.reason == "no_eligible_hosts"
        assert result.rejection.details == {
            "candidates": [
                {"host_id": "host-a", "reason": "provider_capability_missing"}
            ]
        }
        assert nats.published == []
    finally:
        store.close()


def test_successful_assignment_consumes_command_ref_and_replay_is_rejected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, nats, _leases = _controller(store)
        _host(controller, "host-a")
        command_ref = _command(store)

        assigned = controller.assign_task_from_command_ref(command_ref, now=NOW)
        replay = controller.assign_task_from_command_ref(command_ref, now=NOW)

        stored = store.get_command_ref_for_message(command_ref["message_id"])
        assert assigned.status == "assigned"
        assert stored is not None
        assert stored["status"] == "assigned"
        assert replay.status == "rejected"
        assert replay.rejection is not None
        assert replay.rejection.reason == "command_ref_consumed"
        assert [subject for subject, _payload in nats.published] == [
            "openclaw.task.host-a.assigned"
        ]
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


def test_run_health_uses_recent_and_stale_run_events_without_heartbeat(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, _nats, _leases = _controller(store)
        controller.record_run_event(
            {
                "host_id": "host-a",
                "task_id": "task-recent",
                "run_id": "run-recent",
                "status": "working",
                "event_type": "tool_call",
                "generated_at": (NOW - timedelta(minutes=2)).isoformat(),
            },
            now=NOW - timedelta(minutes=2),
        )
        controller.record_run_event(
            {
                "host_id": "host-a",
                "task_id": "task-stale",
                "run_id": "run-stale",
                "status": "working",
                "event_type": "tool_call",
                "generated_at": (NOW - timedelta(minutes=15)).isoformat(),
            },
            now=NOW - timedelta(minutes=15),
        )

        projection = controller.project_fleet(now=NOW)

        health_by_run = {
            run["run_id"]: run["run_health"] for run in projection["runs"]
        }
        assert health_by_run == {
            "run-recent": "healthy",
            "run-stale": "stale",
        }
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


def test_handoff_restart_requires_provider_capabilities(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        controller, _nats, leases = _controller(store)
        _host_with_capabilities(
            controller,
            "host-a",
            capabilities=["task_run"],
        )
        proposal = {
            "handoff_id": "handoff-1",
            "host_id": "host-a",
            "task_id": "task-123",
            "run_id": "run-123",
            "repo_root": r"E:\Projects\repo-a",
            "provider": "codex",
            "required_provider_capabilities": ["fresh_session"],
        }
        leases.acquire_lease(
            "repo",
            r"E:\Projects\repo-a",
            owner_host_id="host-a",
            now=NOW,
        )
        leases.acquire_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-123",
            now=NOW,
        )

        rejected = controller.submit_handoff_proposal(proposal, now=NOW)

        assert rejected.status == "rejected"
        assert rejected.rejection is not None
        assert rejected.rejection.reason == "host_ineligible"
    finally:
        store.close()


def test_host_liveness_uses_controller_receipt_time_not_future_host_time(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        controller, _nats, _leases = _controller(store)
        controller.record_host_heartbeat(
            {
                "kind": "openclaw.host_heartbeat",
                "schema_version": 1,
                "host_id": "host-a",
                "heartbeat_interval_seconds": 10,
                "generated_at": (NOW + timedelta(days=1)).isoformat(),
                "status": "HEALTHY",
                "capabilities": {
                    "repo_roots": [
                        {"path": r"E:\Projects\repo-a", "exists": True}
                    ],
                    "providers": [
                        {
                            "id": "codex",
                            "display_name": "Codex",
                            "capabilities": ["task_run"],
                        }
                    ],
                },
            },
            now=NOW,
        )

        projection = controller.project_fleet(now=NOW + timedelta(seconds=31))

        host = projection["hosts"][0]
        assert host["health"] == "stale"
        assert host["last_heartbeat_at"] == NOW.isoformat()
    finally:
        store.close()
