from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_index.openclaw_hostd import service
from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.nats_client import AgentRunState
from code_index.openclaw_hostd.nats_client import NatsClient


HOST_ID = "host_0123456789abcdef0123456789abcdef"


class FakeNatsTransport:
    def __init__(self) -> None:
        self.connected = False
        self.subscriptions: dict[str, Any] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.kv_entries: list[tuple[str, str, dict[str, Any], int | float | None]] = []

    def connect(self) -> None:
        self.connected = True

    def subscribe(self, subject: str, callback: Any) -> None:
        self.subscriptions[subject] = callback

    def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode("utf-8"))))

    def kv_put(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int | float | None = None,
    ) -> None:
        self.kv_entries.append(
            (bucket, key, json.loads(payload.decode("utf-8")), ttl_seconds)
        )

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


class RecordingOutbox:
    def __init__(self) -> None:
        self.drain_calls = 0

    def drain(self, nats_client: Any) -> None:
        self.drain_calls += 1


def _config(tmp_path: Path) -> HostDaemonConfig:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "host-id.json").write_text(
        json.dumps({"host_id": HOST_ID}),
        encoding="utf-8",
    )
    return HostDaemonConfig(
        state_dir=state_dir,
        host_identity_path=state_dir / "host-id.json",
        repo_roots=(tmp_path,),
        graph_server_url="http://127.0.0.1:8767/health",
        heartbeat_interval_seconds=10,
    )


def test_daemon_loop_uses_connected_nats_for_subscriptions_outbox_and_agent_states(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    outbox = RecordingOutbox()
    graph = FakeGraphClient()
    active_runs = [
        AgentRunState(
            agent_id="agent-1",
            task_id="task-1",
            run_id="run-1",
            current_subtask="checking service wiring",
            last_action_at="2026-05-04T01:00:00+00:00",
        )
    ]

    service.run_daemon_loop(
        _config(tmp_path),
        as_json=True,
        nats_client=nats,
        graph_client=graph,
        outbox=outbox,
        active_run_provider=lambda: active_runs,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert transport.connected is True
    assert set(transport.subscriptions) == {
        f"openclaw.task.{HOST_ID}.assigned",
        f"openclaw.host.{HOST_ID}.inbox",
    }
    assert outbox.drain_calls >= 1
    assert transport.kv_entries == [
        (
            "openclaw_agent_states",
            f"{HOST_ID}.run-1",
            {
                "active_files_json": "[]",
                "active_symbols_json": "[]",
                "agent_id": "agent-1",
                "approach_history_json": "[]",
                "current_subtask": "checking service wiring",
                "estimated_tokens": 0,
                "host_id": HOST_ID,
                "last_action_at": "2026-05-04T01:00:00+00:00",
                "loaded_context_handles_json": "[]",
                "run_id": "run-1",
                "task_id": "task-1",
            },
            30,
        )
    ]


def test_daemon_loop_nats_callbacks_route_task_and_host_inbox_messages(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = FakeGraphClient()
    service.run_daemon_loop(
        _config(tmp_path),
        as_json=True,
        nats_client=nats,
        graph_client=graph,
        active_run_provider=tuple,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    transport.subscriptions[f"openclaw.task.{HOST_ID}.assigned"](
        {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": HOST_ID,
            "task_id": "task-123",
            "message_id": "msg-task",
            "delivery_id": "delivery-task",
            "message": "Run the task.",
        }
    )
    transport.subscriptions[f"openclaw.host.{HOST_ID}.inbox"](
        {
            "kind": "openclaw.host_delivery",
            "schema_version": 1,
            "host_id": HOST_ID,
            "message_id": "msg-host",
            "delivery_id": "delivery-host",
            "message_type": "chat",
            "body": "FYI",
        }
    )

    assert [request["task_id"] for request in graph.requests] == ["task-123"]
    assert [subject for subject, _ in transport.published] == [
        f"openclaw.task.{HOST_ID}.ack",
        f"openclaw.host.{HOST_ID}.messages.ack",
    ]


def test_daemon_loop_without_nats_runs_one_heartbeat_safely(tmp_path: Path) -> None:
    service.run_daemon_loop(
        _config(tmp_path),
        as_json=True,
        active_run_provider=lambda: [
            AgentRunState(agent_id="agent-1", task_id="task-1", run_id="run-1")
        ],
        sleep=lambda seconds: None,
        max_iterations=1,
    )
