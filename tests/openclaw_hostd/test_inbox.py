from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.inbox import HostInbox
from code_index.openclaw_hostd.inbox import InboxValidationError
from code_index.openclaw_hostd.inbox import TaskInbox
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_hostd.nats_client import AgentRunState
from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_hostd import service


class FakeNatsTransport:
    def __init__(self) -> None:
        self.connected = False
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.supports_kv_ttl = True

    def connect(self) -> None:
        self.connected = True

    def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))

    def close(self) -> None:
        self.connected = False


class ReentrantPublishTransport(FakeNatsTransport):
    def __init__(self) -> None:
        super().__init__()
        self.on_first_publish = None

    def publish(self, subject: str, payload: bytes) -> None:
        super().publish(subject, payload)
        if len(self.published) == 1 and self.on_first_publish is not None:
            self.on_first_publish()


class FakeGraphClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def submit_task(self, **payload: Any) -> GraphServerResponse:
        self.requests.append(dict(payload))
        return GraphServerResponse(
            ok=True,
            status_code=201,
            payload={
                "run": {
                    "run_id": payload.get("run_id") or f"run-{payload['task_id']}"
                }
            },
        )


def _assignment(**overrides: Any) -> dict[str, Any]:
    message = {
        "kind": "openclaw.task_assignment",
        "schema_version": 1,
        "host_id": "host-a",
        "task_id": "task-123",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message": "Inspect selected files.",
    }
    message.update(overrides)
    return message


def _expected_planned_run_id(host_id: str, task_id: str) -> str:
    digest = hashlib.sha256(f"{host_id}\0{task_id}".encode("utf-8")).hexdigest()[:32]
    return f"run-openclaw-{digest}"


def _delivery(**overrides: Any) -> dict[str, Any]:
    message = {
        "kind": "openclaw.host_delivery",
        "schema_version": 1,
        "host_id": "host-a",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message_type": "chat",
        "room_id": "room-1",
        "body": "Heads up.",
    }
    message.update(overrides)
    return message


class ReentrantGraphClient(FakeGraphClient):
    def __init__(self) -> None:
        super().__init__()
        self.on_first_submit = None

    def submit_task(self, **payload: Any) -> GraphServerResponse:
        self.requests.append(dict(payload))
        if len(self.requests) == 1 and self.on_first_submit is not None:
            self.on_first_submit()
        return GraphServerResponse(
            ok=True,
            status_code=201,
            payload={
                "run": {
                    "run_id": payload.get("run_id") or f"run-{payload['task_id']}"
                }
            },
        )


class CrashAfterAcceptGraphClient(FakeGraphClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_run_ids: list[str] = []
        self.fail_first = True

    def submit_task(self, **payload: Any) -> GraphServerResponse:
        self.requests.append(dict(payload))
        run_id = str(payload.get("run_id") or f"run-{payload['task_id']}")
        if run_id not in self.created_run_ids:
            self.created_run_ids.append(run_id)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("host crashed after graph-server accepted")
        return GraphServerResponse(
            ok=True,
            status_code=200,
            payload={"run": {"run_id": run_id, "duplicate": True}},
        )


def test_task_inbox_validates_assignment_deduplicates_by_task_id_and_publishes_ack(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    graph = FakeGraphClient()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        nats_client=nats,
    )
    assignment = {
        "kind": "openclaw.task_assignment",
        "schema_version": 1,
        "host_id": "host-a",
        "task_id": "task-123",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message": "Inspect selected files.",
        "selected_paths": ["code_index/openclaw_hostd/service.py"],
        "provider": "codex",
    }

    first = inbox.handle_task_assignment(assignment)
    duplicate = inbox.handle_task_assignment(
        {
            **assignment,
            "message_id": "msg-duplicate",
            "delivery_id": "delivery-duplicate",
        }
    )

    expected_run_id = _expected_planned_run_id("host-a", "task-123")
    assert first.status == "accepted"
    assert first.run_id == expected_run_id
    assert duplicate.status == "duplicate"
    assert duplicate.run_id == expected_run_id
    assert len(graph.requests) == 1
    assert graph.requests[0]["task_id"] == "task-123"
    assert graph.requests[0]["selected_paths"] == [
        "code_index/openclaw_hostd/service.py"
    ]
    assert [subject for subject, _ in transport.published] == [
        "openclaw.task.host-a.ack",
        "openclaw.task.host-a.ack",
    ]
    assert [payload["status"] for _, payload in transport.published] == [
        "accepted",
        "duplicate",
    ]


def test_task_inbox_fails_closed_when_task_lease_conflicts(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    graph = FakeGraphClient()
    leases = InMemoryFleetLeaseStore()
    leases.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-b",
        owner_run_id="run-host-b",
    )
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        nats_client=nats,
        lease_store=leases,
    )

    result = inbox.handle_task_assignment(_assignment())

    assert result.status == "lease_conflict"
    assert result.duplicate is False
    assert result.ack_published is True
    assert graph.requests == []
    assert inbox._task_row("task-123") is None
    assert [payload["status"] for _, payload in transport.published] == [
        "lease_conflict"
    ]


def test_task_inbox_releases_task_lease_for_terminal_local_status(
    tmp_path: Path,
) -> None:
    graph = FakeGraphClient()
    leases = InMemoryFleetLeaseStore()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        lease_store=leases,
    )
    accepted = inbox.handle_task_assignment(_assignment())
    active = leases.get_active_lease("task", "task-123")
    assert active is not None

    released = inbox.release_task_lease_on_terminal_status(
        "task-123",
        terminal_status="completed",
        run_id=accepted.run_id,
    )

    task = leases.get_task_record("task-123")
    assert released is not None
    assert released.status == "released"
    assert leases.get_active_lease("task", "task-123") is None
    assert task is not None
    assert task.status == "completed"


def test_task_inbox_does_not_reacquire_lease_for_terminal_duplicate(
    tmp_path: Path,
) -> None:
    graph = FakeGraphClient()
    leases = InMemoryFleetLeaseStore()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        lease_store=leases,
    )
    accepted = inbox.handle_task_assignment(_assignment())
    inbox.release_task_lease_on_terminal_status(
        "task-123",
        terminal_status="completed",
        run_id=accepted.run_id,
    )

    duplicate = inbox.handle_task_assignment(
        _assignment(message_id="msg-late", delivery_id="delivery-late")
    )

    assert duplicate.status == "duplicate"
    assert leases.get_active_lease("task", "task-123") is None
    assert [request["task_id"] for request in graph.requests] == ["task-123"]


def test_task_inbox_rejects_wrong_host_without_dispatch_or_ack(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    graph = FakeGraphClient()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        nats_client=nats,
    )

    with pytest.raises(InboxValidationError, match="host_id"):
        inbox.handle_task_assignment(
            {
                "kind": "openclaw.task_assignment",
                "schema_version": 1,
                "host_id": "host-b",
                "task_id": "task-123",
                "message_id": "msg-1",
                "delivery_id": "delivery-1",
                "message": "Wrong host.",
            }
        )

    assert graph.requests == []
    assert transport.published == []


def test_task_inbox_reserves_task_before_graph_submission_for_reentrant_replay(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    graph = ReentrantGraphClient()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        nats_client=nats,
    )
    assignment = {
        "kind": "openclaw.task_assignment",
        "schema_version": 1,
        "host_id": "host-a",
        "task_id": "task-123",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message": "Inspect selected files.",
    }
    graph.on_first_submit = lambda: inbox.handle_task_assignment(
        {
            **assignment,
            "message_id": "msg-replay",
            "delivery_id": "delivery-replay",
        }
    )

    result = inbox.handle_task_assignment(assignment)

    assert result.status == "accepted"
    assert [request["task_id"] for request in graph.requests] == ["task-123"]


def test_task_ids_that_sanitize_to_same_text_get_distinct_planned_run_ids(
    tmp_path: Path,
) -> None:
    graph = FakeGraphClient()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
    )

    slash = inbox.handle_task_assignment(
        _assignment(
            task_id="task/a",
            message_id="msg-slash",
            delivery_id="delivery-slash",
        )
    )
    space = inbox.handle_task_assignment(
        _assignment(
            task_id="task a",
            message_id="msg-space",
            delivery_id="delivery-space",
        )
    )

    assert slash.run_id == _expected_planned_run_id("host-a", "task/a")
    assert space.run_id == _expected_planned_run_id("host-a", "task a")
    assert slash.run_id != space.run_id
    assert [request["task_id"] for request in graph.requests] == ["task/a", "task a"]
    assert graph.requests[0]["run_id"] != graph.requests[1]["run_id"]


def test_processing_task_without_run_id_is_recovered_after_crash(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "inbox.db"
    crashed = TaskInbox(db_path, host_id="host-a", graph_client=FakeGraphClient())
    crashed._reserve_task(_assignment())
    crashed.close()
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    graph = FakeGraphClient()
    recovered = TaskInbox(
        db_path,
        host_id="host-a",
        graph_client=graph,
        nats_client=nats,
    )

    first_replay = recovered.handle_task_assignment(_assignment())
    second_replay = recovered.handle_task_assignment(_assignment(message_id="msg-2"))

    assert first_replay.status == "accepted"
    assert first_replay.run_id == _expected_planned_run_id("host-a", "task-123")
    assert second_replay.status == "duplicate"
    assert [request["task_id"] for request in graph.requests] == ["task-123"]


def test_crash_after_graph_accept_replay_uses_same_planned_run_id(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "inbox.db"
    graph = CrashAfterAcceptGraphClient()
    first = TaskInbox(db_path, host_id="host-a", graph_client=graph)
    with pytest.raises(RuntimeError, match="host crashed"):
        first.handle_task_assignment(_assignment())
    first.close()
    recovered = TaskInbox(db_path, host_id="host-a", graph_client=graph)

    replay = recovered.handle_task_assignment(_assignment())

    expected_run_id = _expected_planned_run_id("host-a", "task-123")
    assert replay.status == "accepted"
    assert replay.run_id == expected_run_id
    assert [request["run_id"] for request in graph.requests] == [
        expected_run_id,
        expected_run_id,
    ]
    assert graph.created_run_ids == [expected_run_id]


def test_task_ack_is_reserved_before_publish_so_reentrant_replay_does_not_ack_twice(
    tmp_path: Path,
) -> None:
    transport = ReentrantPublishTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    graph = FakeGraphClient()
    inbox = TaskInbox(
        tmp_path / "inbox.db",
        host_id="host-a",
        graph_client=graph,
        nats_client=nats,
    )
    assignment = {
        "kind": "openclaw.task_assignment",
        "schema_version": 1,
        "host_id": "host-a",
        "task_id": "task-123",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message": "Inspect selected files.",
    }
    transport.on_first_publish = lambda: inbox.handle_task_assignment(assignment)

    result = inbox.handle_task_assignment(assignment)

    assert result.status == "accepted"
    assert [subject for subject, _ in transport.published] == [
        "openclaw.task.host-a.ack"
    ]


def test_unpublished_task_ack_is_retried_on_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "inbox.db"
    inbox = TaskInbox(db_path, host_id="host-a", graph_client=FakeGraphClient())
    result = inbox.handle_task_assignment(_assignment())
    assert result.ack_published is False
    inbox.close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE openclaw_task_ack_log
           SET ack_published_at = NULL
         WHERE message_id = 'msg-1'
           AND delivery_id = 'delivery-1'
        """
    )
    conn.commit()
    conn.close()
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    recovered = TaskInbox(
        db_path,
        host_id="host-a",
        graph_client=FakeGraphClient(),
        nats_client=nats,
    )

    replay = recovered.handle_task_assignment(_assignment())
    duplicate = recovered.handle_task_assignment(_assignment())

    assert replay.ack_published is True
    assert duplicate.ack_published is False
    assert [payload["task_id"] for _, payload in transport.published] == ["task-123"]


def test_host_inbox_acks_duplicate_message_delivery_once(tmp_path: Path) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    inbox = HostInbox(
        tmp_path / "host-inbox.db",
        host_id="host-a",
        nats_client=nats,
    )
    delivery = {
        "kind": "openclaw.host_delivery",
        "schema_version": 1,
        "host_id": "host-a",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message_type": "chat",
        "room_id": "room-1",
        "body": "Heads up.",
    }

    first = inbox.handle_message_delivery(delivery)
    duplicate = inbox.handle_message_delivery(delivery)

    assert first.status == "acked"
    assert first.duplicate is False
    assert duplicate.status == "acked"
    assert duplicate.duplicate is True
    assert transport.published == [
        (
            "openclaw.host.host-a.messages.ack",
            {
                "delivery_id": "delivery-1",
                "host_id": "host-a",
                "kind": "openclaw.message_delivery_ack",
                "message_id": "msg-1",
                "schema_version": 1,
                "status": "acked",
            },
        )
    ]


def test_host_ack_is_reserved_before_publish_so_reentrant_replay_does_not_ack_twice(
    tmp_path: Path,
) -> None:
    transport = ReentrantPublishTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    inbox = HostInbox(
        tmp_path / "host-inbox.db",
        host_id="host-a",
        nats_client=nats,
    )
    delivery = {
        "kind": "openclaw.host_delivery",
        "schema_version": 1,
        "host_id": "host-a",
        "message_id": "msg-1",
        "delivery_id": "delivery-1",
        "message_type": "chat",
        "room_id": "room-1",
        "body": "Heads up.",
    }
    transport.on_first_publish = lambda: inbox.handle_message_delivery(delivery)

    result = inbox.handle_message_delivery(delivery)

    assert result.status == "acked"
    assert [subject for subject, _ in transport.published] == [
        "openclaw.host.host-a.messages.ack"
    ]


def test_unpublished_message_ack_is_retried_on_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "host-inbox.db"
    inbox = HostInbox(db_path, host_id="host-a")
    result = inbox.handle_message_delivery(_delivery())
    assert result.ack_published is False
    inbox.close()
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    recovered = HostInbox(db_path, host_id="host-a", nats_client=nats)

    replay = recovered.handle_message_delivery(_delivery())
    duplicate = recovered.handle_message_delivery(_delivery())

    assert replay.ack_published is True
    assert duplicate.ack_published is False
    assert [payload["message_id"] for _, payload in transport.published] == ["msg-1"]


def test_host_inbox_requires_valid_signed_command_ref_for_mutating_delivery(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    inbox = HostInbox(
        tmp_path / "host-inbox.db",
        host_id="host-a",
        nats_client=nats,
        command_ref_verifier=lambda command_ref: command_ref.get("signature") == "ok",
    )
    command_delivery = {
        "kind": "openclaw.host_delivery",
        "schema_version": 1,
        "host_id": "host-a",
        "message_id": "msg-command",
        "delivery_id": "delivery-command",
        "message_type": "command",
        "body": "Assign task.",
        "command_ref": {"command_id": "cmd-1", "signature": "bad"},
    }

    with pytest.raises(InboxValidationError, match="command_ref"):
        inbox.handle_message_delivery(command_delivery)

    accepted = inbox.handle_message_delivery(
        {**command_delivery, "command_ref": {"command_id": "cmd-1", "signature": "ok"}}
    )

    assert accepted.status == "acked"
    assert [payload["message_id"] for _, payload in transport.published] == [
        "msg-command"
    ]


def test_host_inbox_requires_room_id_for_non_mutating_delivery(tmp_path: Path) -> None:
    inbox = HostInbox(tmp_path / "host-inbox.db", host_id="host-a")

    with pytest.raises(InboxValidationError, match="room_id"):
        inbox.handle_message_delivery(_delivery(room_id=""))


class ControlledClockKvTransport:
    def __init__(self) -> None:
        self.now = 100.0
        self.store: dict[tuple[str, str], dict[str, Any]] = {}
        self.supports_kv_ttl = True

    def connect(self) -> None:
        return None

    def kv_put(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int | float | None = None,
    ) -> None:
        self.store[(bucket, key)] = {
            "payload": json.loads(payload.decode("utf-8")),
            "expires_at": self.now + float(ttl_seconds or 0),
            "ttl_seconds": ttl_seconds,
        }

    def get(self, bucket: str, key: str) -> dict[str, Any] | None:
        entry = self.store.get((bucket, key))
        if entry is None:
            return None
        if self.now >= entry["expires_at"]:
            self.store.pop((bucket, key), None)
            return None
        return dict(entry)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_agent_state_is_published_on_heartbeat_and_expires_after_missed_heartbeats(
    tmp_path: Path,
) -> None:
    transport = ControlledClockKvTransport()
    nats = NatsClient(transport=transport)
    nats.connect()
    config = HostDaemonConfig(
        state_dir=tmp_path / "state",
        host_identity_path=tmp_path / "state" / "host-id.json",
        repo_roots=(tmp_path,),
        graph_server_url=None,
        heartbeat_interval_seconds=10,
    )
    active_run = AgentRunState(
        agent_id="agent-1",
        task_id="task-123",
        run_id="run-123",
        current_subtask="writing inbox tests",
        active_files=("code_index/openclaw_hostd/inbox.py",),
        active_symbols=("TaskInbox", "HostInbox"),
        loaded_context_handles=({"kind": "doc", "handle": "CONTEXT.md"},),
        estimated_tokens=2048,
        approach_history=("tdd",),
        last_action_at="2026-05-03T23:00:00+00:00",
    )

    heartbeat = service.run_once(
        config,
        as_json=True,
        nats_client=nats,
        active_agent_runs=[active_run],
    )

    key = f"{heartbeat['host_id']}.run-123"
    entry = transport.get("openclaw_agent_states", key)
    assert entry is not None
    assert entry["ttl_seconds"] == 30
    payload = entry["payload"]
    assert payload["agent_id"] == "agent-1"
    assert payload["host_id"] == heartbeat["host_id"]
    assert payload["task_id"] == "task-123"
    assert payload["run_id"] == "run-123"
    assert json.loads(payload["active_files_json"]) == [
        "code_index/openclaw_hostd/inbox.py"
    ]
    assert json.loads(payload["active_symbols_json"]) == ["TaskInbox", "HostInbox"]
    assert json.loads(payload["loaded_context_handles_json"]) == [
        {"handle": "CONTEXT.md", "kind": "doc"}
    ]
    assert payload["estimated_tokens"] == 2048
    assert json.loads(payload["approach_history_json"]) == ["tdd"]
    assert payload["last_action_at"] == "2026-05-03T23:00:00+00:00"

    transport.advance(29.9)
    assert transport.get("openclaw_agent_states", key) is not None
    transport.advance(0.2)
    assert transport.get("openclaw_agent_states", key) is None
