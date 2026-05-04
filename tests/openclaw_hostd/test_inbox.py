from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.inbox import HostInbox
from code_index.openclaw_hostd.inbox import InboxValidationError
from code_index.openclaw_hostd.inbox import TaskInbox
from code_index.openclaw_hostd.nats_client import AgentRunState
from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_hostd import service


class FakeNatsTransport:
    def __init__(self) -> None:
        self.connected = False
        self.published: list[tuple[str, dict[str, Any]]] = []

    def connect(self) -> None:
        self.connected = True

    def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))

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
            payload={"run": {"run_id": f"run-{payload['task_id']}"}},
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

    assert first.status == "accepted"
    assert first.run_id == "run-task-123"
    assert duplicate.status == "duplicate"
    assert duplicate.run_id == "run-task-123"
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


class ControlledClockKvTransport:
    def __init__(self) -> None:
        self.now = 100.0
        self.store: dict[tuple[str, str], dict[str, Any]] = {}

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
