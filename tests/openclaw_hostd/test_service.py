from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_index.openclaw_hostd import service
from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.nats_client import AgentRunState
from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_hostd.nats_client import NatsUnavailableError


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
    def __init__(self, *, agent_board_payload: dict[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.agent_board_payload = agent_board_payload
        self.agent_board_calls = 0

    def submit_task(self, **payload: Any) -> GraphServerResponse:
        self.requests.append(dict(payload))
        return GraphServerResponse(
            ok=True,
            status_code=201,
            payload={"run": {"run_id": f"run-{payload['task_id']}"}},
        )

    def agent_board(self) -> GraphServerResponse:
        self.agent_board_calls += 1
        return GraphServerResponse(
            ok=True,
            status_code=200,
            payload=self.agent_board_payload or {},
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


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(message % args if args else message)


def _active_run_board_payload() -> dict[str, Any]:
    return {
        "kind": "code_index_agent_kanban",
        "columns": {
            "active": {
                "runs": [
                    {
                        "run_id": "run-graph-1",
                        "task_id": "task-graph-1",
                        "agent_id": "agent-graph-1",
                        "agent_name": "Codex",
                        "status": "working",
                        "current_subtask": "reading graph-server state",
                        "active_files": ["code_index/openclaw_hostd/service.py"],
                        "selected_nodes": ["service.run_daemon_loop"],
                        "updated_at": "2026-05-04T02:00:00+00:00",
                        "metadata": {
                            "loaded_context_handles": [
                                {"handle": "symbol:service.run_daemon_loop"}
                            ],
                            "estimated_tokens": 1234,
                            "approach_history": ["inspect", "patch"],
                        },
                    }
                ],
            },
            "done": {
                "runs": [
                    {
                        "run_id": "run-done",
                        "task_id": "task-done",
                        "agent_id": "agent-done",
                        "status": "completed",
                    }
                ],
            },
        },
    }


def _config_with_nats(tmp_path: Path) -> HostDaemonConfig:
    config = _config(tmp_path)
    return HostDaemonConfig(
        state_dir=config.state_dir,
        host_identity_path=config.host_identity_path,
        repo_roots=config.repo_roots,
        graph_server_url=config.graph_server_url,
        graph_server_token=config.graph_server_token,
        ssh_hostname=config.ssh_hostname,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        nats_url="nats://127.0.0.1:4222",
        config_path=config.config_path,
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


def test_configured_nats_transport_factory_subscribes_drains_and_publishes_agent_states(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    outbox = RecordingOutbox()
    graph = FakeGraphClient()
    factory_urls: list[str] = []

    def transport_factory(url: str) -> FakeNatsTransport:
        factory_urls.append(url)
        return transport

    service.run_daemon_loop(
        _config_with_nats(tmp_path),
        as_json=True,
        nats_transport_factory=transport_factory,
        graph_client=graph,
        outbox=outbox,
        active_run_provider=lambda: [
            AgentRunState(
                agent_id="agent-factory",
                task_id="task-factory",
                run_id="run-factory",
            )
        ],
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert factory_urls == ["nats://127.0.0.1:4222"]
    assert set(transport.subscriptions) == {
        f"openclaw.task.{HOST_ID}.assigned",
        f"openclaw.host.{HOST_ID}.inbox",
    }
    assert outbox.drain_calls >= 1
    assert [entry[1] for entry in transport.kv_entries] == [f"{HOST_ID}.run-factory"]


def test_configured_nats_without_transport_logs_disabled_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = RecordingLogger()

    def no_transport(url: str) -> object:
        raise NatsUnavailableError("optional nats-py package is not installed")

    monkeypatch.setattr(service, "create_nats_transport", no_transport)

    runtime = service.setup_nats_runtime(
        _config_with_nats(tmp_path),
        HostIdentity(host_id=HOST_ID),
        graph_client=FakeGraphClient(),
        logger=logger,
    )

    assert runtime is None
    assert logger.warnings == [
        "OpenClaw NATS unavailable; continuing without NATS: "
        "optional nats-py package is not installed"
    ]


def test_daemon_loop_default_active_run_provider_reads_graph_server_agent_board(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = FakeGraphClient(agent_board_payload=_active_run_board_payload())

    service.run_daemon_loop(
        _config_with_nats(tmp_path),
        as_json=True,
        nats_client=nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert graph.agent_board_calls == 1
    assert transport.kv_entries == [
        (
            "openclaw_agent_states",
            f"{HOST_ID}.run-graph-1",
            {
                "active_files_json": '["code_index/openclaw_hostd/service.py"]',
                "active_symbols_json": '["service.run_daemon_loop"]',
                "agent_id": "agent-graph-1",
                "approach_history_json": '["inspect","patch"]',
                "current_subtask": "reading graph-server state",
                "estimated_tokens": 1234,
                "host_id": HOST_ID,
                "last_action_at": "2026-05-04T02:00:00+00:00",
                "loaded_context_handles_json": (
                    '[{"handle":"symbol:service.run_daemon_loop"}]'
                ),
                "run_id": "run-graph-1",
                "task_id": "task-graph-1",
            },
            30,
        )
    ]


def test_cli_once_uses_configured_nats_and_default_graph_active_run_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = FakeNatsTransport()
    graph = FakeGraphClient(agent_board_payload=_active_run_board_payload())
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "host-identity.json").write_text(
        json.dumps({"host_id": HOST_ID}),
        encoding="utf-8",
    )

    monkeypatch.setenv("OPENCLAW_HOSTD_STATE_DIR", str(state_dir))
    monkeypatch.setenv("OPENCLAW_HOSTD_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("OPENCLAW_HOSTD_GRAPH_SERVER_URL", "http://127.0.0.1:8767")
    monkeypatch.setenv("OPENCLAW_HOSTD_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setattr(service, "create_nats_transport", lambda url: transport)
    monkeypatch.setattr(service, "GraphServerClient", lambda *args, **kwargs: graph)

    rc = service.main(["--once", "--json"])
    capsys.readouterr()

    assert rc == 0
    assert graph.agent_board_calls == 1
    assert [entry[1] for entry in transport.kv_entries] == [f"{HOST_ID}.run-graph-1"]


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
