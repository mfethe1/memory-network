from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from code_index.openclaw_hostd import service
from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.leases import LeaseConflictError
from code_index.openclaw_hostd.leases import SQLiteFleetLeaseStore
from code_index.openclaw_hostd.nats_client import AgentRunState
from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_hostd.nats_client import NatsUnavailableError


HOST_ID = "host_0123456789abcdef0123456789abcdef"


class FakeNatsTransport:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.subscriptions: dict[str, Any] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.kv_entries: list[tuple[str, str, dict[str, Any], int | float | None]] = []
        self.supports_kv_ttl = True

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
        self.closed = True
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


class RespectRunIdGraphClient(FakeGraphClient):
    def submit_task(self, **payload: Any) -> GraphServerResponse:
        self.requests.append(dict(payload))
        return GraphServerResponse(
            ok=True,
            status_code=201,
            payload={"run": {"run_id": payload["run_id"]}},
        )


class RecordingOutbox:
    def __init__(self) -> None:
        self.drain_calls = 0
        self.closed = False

    def drain(self, nats_client: Any) -> None:
        self.drain_calls += 1

    def close(self) -> None:
        self.closed = True


class FailingKvTransport(FakeNatsTransport):
    def kv_put(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int | float | None = None,
    ) -> None:
        raise RuntimeError("kv unavailable")


class FakeNatsMessage:
    def __init__(self, payload: dict[str, Any], *, reply: str = "") -> None:
        self.data = json.dumps(payload).encode("utf-8")
        self.reply = reply
        self.ack_count = 0

    def ack(self) -> None:
        self.ack_count += 1


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
        self.errors: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(message % args if args else message)

    def error(self, message: str, *args: object) -> None:
        self.errors.append(message % args if args else message)


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
        fleet_lease_store_path=config.fleet_lease_store_path,
    )


def _config_with_nats_and_leases(tmp_path: Path) -> HostDaemonConfig:
    config = _config_with_nats(tmp_path)
    return HostDaemonConfig(
        state_dir=config.state_dir,
        host_identity_path=config.host_identity_path,
        repo_roots=config.repo_roots,
        graph_server_url=config.graph_server_url,
        graph_server_token=config.graph_server_token,
        ssh_hostname=config.ssh_hostname,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        nats_url=config.nats_url,
        config_path=config.config_path,
        fleet_lease_store_path=tmp_path / "central-fleet-leases.db",
    )


def _config_with_nats_without_graph(tmp_path: Path) -> HostDaemonConfig:
    config = _config_with_nats(tmp_path)
    return HostDaemonConfig(
        state_dir=config.state_dir,
        host_identity_path=config.host_identity_path,
        repo_roots=config.repo_roots,
        graph_server_url=None,
        graph_server_token=config.graph_server_token,
        ssh_hostname=config.ssh_hostname,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        nats_url=config.nats_url,
        config_path=config.config_path,
        fleet_lease_store_path=config.fleet_lease_store_path,
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

    assert transport.closed is True
    assert transport.connected is False
    assert set(transport.subscriptions) == {
        f"openclaw.deliver.{HOST_ID}.tasks",
        f"openclaw.host.{HOST_ID}.inbox",
    }
    assert [subject for subject, _payload in transport.published] == [
        f"openclaw.host.{HOST_ID}.heartbeat",
        f"openclaw.host.{HOST_ID}.capabilities",
    ]
    heartbeat_payload = transport.published[0][1]
    capabilities_payload = transport.published[1][1]
    assert heartbeat_payload["kind"] == "openclaw.host_heartbeat"
    assert heartbeat_payload["host_id"] == HOST_ID
    assert capabilities_payload == {
        "kind": "openclaw.host_capabilities",
        "schema_version": 1,
        "generated_at": heartbeat_payload["generated_at"],
        "host_id": HOST_ID,
        "ssh_hostname": heartbeat_payload["ssh_hostname"],
        "capabilities": heartbeat_payload["capabilities"],
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
        f"openclaw.deliver.{HOST_ID}.tasks",
        f"openclaw.host.{HOST_ID}.inbox",
    }
    assert outbox.drain_calls >= 1
    assert [entry[1] for entry in transport.kv_entries] == [f"{HOST_ID}.run-factory"]


def test_configured_nats_runs_with_empty_graph_server_url(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    outbox = RecordingOutbox()
    factory_urls: list[str] = []

    def transport_factory(url: str) -> FakeNatsTransport:
        factory_urls.append(url)
        return transport

    service.run_daemon_loop(
        _config_with_nats_without_graph(tmp_path),
        as_json=True,
        nats_transport_factory=transport_factory,
        outbox=outbox,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert factory_urls == ["nats://127.0.0.1:4222"]
    assert set(transport.subscriptions) == {
        f"openclaw.deliver.{HOST_ID}.tasks",
        f"openclaw.host.{HOST_ID}.inbox",
    }
    assert outbox.drain_calls >= 1
    assert transport.kv_entries == []


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


def test_default_active_run_provider_ignores_non_active_kanban_columns(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = FakeGraphClient(
        agent_board_payload={
            "columns": {
                "ready": {
                    "runs": [
                        {
                            "run_id": "run-ready",
                            "task_id": "task-ready",
                            "agent_id": "agent-ready",
                            "status": "ready",
                        }
                    ]
                },
                "active": {
                    "runs": [
                        {
                            "run_id": "run-active",
                            "task_id": "task-active",
                            "agent_id": "agent-active",
                            "status": "working",
                        }
                    ]
                },
            }
        }
    )

    service.run_daemon_loop(
        _config_with_nats(tmp_path),
        as_json=True,
        nats_client=nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert [key for _, key, _, _ in transport.kv_entries] == [
        f"{HOST_ID}.run-active"
    ]


def test_daemon_loop_closes_runtime_resources_on_limited_return(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    outbox = RecordingOutbox()

    service.run_daemon_loop(
        _config_with_nats(tmp_path),
        as_json=True,
        nats_client=nats,
        graph_client=FakeGraphClient(),
        outbox=outbox,
        active_run_provider=tuple,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert transport.closed is True
    assert nats.connected is False
    assert outbox.closed is True


def test_agent_state_publish_failure_does_not_kill_heartbeat_loop(
    tmp_path: Path,
) -> None:
    transport = FailingKvTransport()
    nats = NatsClient(transport=transport)
    logger = RecordingLogger()

    service.run_daemon_loop(
        _config_with_nats(tmp_path),
        as_json=True,
        nats_client=nats,
        graph_client=FakeGraphClient(),
        active_run_provider=lambda: [
            AgentRunState(agent_id="agent-1", task_id="task-1", run_id="run-1")
        ],
        logger=logger,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    assert any("agent state" in message for message in logger.warnings)


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
    identity = HostIdentity(host_id=HOST_ID)
    runtime = service.setup_nats_runtime(
        _config(tmp_path),
        identity,
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None

    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
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
            "room_id": "room-1",
        }
    )

    assert [request["task_id"] for request in graph.requests] == ["task-123"]
    assert [subject for subject, _ in transport.published] == [
        f"openclaw.task.{HOST_ID}.ack",
        f"openclaw.host.{HOST_ID}.messages.ack",
    ]
    runtime.close()


def test_nats_subscription_callbacks_are_processed_off_callback_thread(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = FakeGraphClient()
    runtime = service.setup_nats_runtime(
        _config(tmp_path),
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None
    errors: list[BaseException] = []

    def invoke_callback() -> None:
        try:
            transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
                {
                    "kind": "openclaw.task.assigned",
                    "schema_version": 1,
                    "host_id": HOST_ID,
                    "task_id": "task-thread",
                    "message_id": "msg-thread",
                    "delivery_id": "delivery-thread",
                    "message": "Run from callback thread.",
                }
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke_callback)
    thread.start()
    thread.join(timeout=5)
    runtime.close()

    assert errors == []
    assert [request["task_id"] for request in graph.requests] == ["task-thread"]


def test_nats_runtime_uses_configured_shared_lease_store_by_default(
    tmp_path: Path,
) -> None:
    config = _config_with_nats_and_leases(tmp_path)
    central = SQLiteFleetLeaseStore(config.fleet_lease_store_path)
    central.acquire_lease(
        "task",
        "task-conflict",
        owner_host_id="host-other",
        owner_run_id="run-other",
    )
    central.close()
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = FakeGraphClient()
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None

    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
        {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": HOST_ID,
            "task_id": "task-conflict",
            "message_id": "msg-conflict",
            "delivery_id": "delivery-conflict",
            "message": "This should not dispatch.",
        }
    )
    runtime.close()

    assert graph.requests == []
    assert [payload["status"] for _, payload in transport.published] == [
        "lease_conflict"
    ]


def test_daemon_loop_releases_task_lease_when_graph_reports_terminal_run(
    tmp_path: Path,
) -> None:
    config = _config_with_nats_and_leases(tmp_path)
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = RespectRunIdGraphClient()
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None
    assignment = {
        "kind": "openclaw.task.assigned",
        "schema_version": 1,
        "host_id": HOST_ID,
        "task_id": "task-complete",
        "message_id": "msg-complete",
        "delivery_id": "delivery-complete",
        "message": "Complete normally.",
    }
    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](assignment)
    run_id = graph.requests[0]["run_id"]
    runtime.close()
    central = SQLiteFleetLeaseStore(config.fleet_lease_store_path)
    assert central.get_active_lease("task", "task-complete") is not None
    central.close()
    graph.agent_board_payload = {
        "runs": [
            {
                "run_id": run_id,
                "task_id": "task-complete",
                "status": "completed",
            }
        ]
    }
    loop_transport = FakeNatsTransport()
    loop_nats = NatsClient(transport=loop_transport)

    service.run_daemon_loop(
        config,
        as_json=True,
        nats_client=loop_nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    verified = SQLiteFleetLeaseStore(config.fleet_lease_store_path)
    task = verified.get_task_record("task-complete")
    try:
        assert verified.get_active_lease("task", "task-complete") is None
        assert task is not None
        assert task.status == "completed"
        assert task.terminal_status == "completed"
    finally:
        verified.close()


def test_daemon_loop_renews_active_task_lease_before_original_ttl_expires(
    tmp_path: Path,
) -> None:
    config = _config_with_nats_and_leases(tmp_path)
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = RespectRunIdGraphClient()
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None
    runtime.task_inbox.lease_ttl_seconds = 0.08
    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
        {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": HOST_ID,
            "task_id": "task-renew",
            "message_id": "msg-renew",
            "delivery_id": "delivery-renew",
            "message": "Stay active long enough to renew.",
        }
    )
    run_id = graph.requests[0]["run_id"]
    runtime.close()
    time.sleep(0.03)
    graph.agent_board_payload = {
        "runs": [
            {
                "run_id": run_id,
                "task_id": "task-renew",
                "status": "working",
                "updated_at": "2026-05-04T12:00:00+00:00",
            }
        ]
    }

    loop_transport = FakeNatsTransport()
    loop_nats = NatsClient(transport=loop_transport)
    service.run_daemon_loop(
        config,
        as_json=True,
        nats_client=loop_nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )
    time.sleep(0.08)
    other_host = SQLiteFleetLeaseStore(config.fleet_lease_store_path)
    try:
        with pytest.raises(LeaseConflictError, match=HOST_ID):
            other_host.acquire_lease(
                "task",
                "task-renew",
                owner_host_id="host-other",
                owner_run_id="run-other",
            )
    finally:
        other_host.close()


def test_daemon_loop_terminal_release_after_renewal_uses_current_fence(
    tmp_path: Path,
) -> None:
    config = _config_with_nats_and_leases(tmp_path)
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = RespectRunIdGraphClient()
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None
    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
        {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": HOST_ID,
            "task_id": "task-renew-complete",
            "message_id": "msg-renew-complete",
            "delivery_id": "delivery-renew-complete",
            "message": "Renew then complete.",
        }
    )
    run_id = graph.requests[0]["run_id"]
    runtime.close()
    graph.agent_board_payload = {
        "runs": [
            {
                "run_id": run_id,
                "task_id": "task-renew-complete",
                "status": "working",
            }
        ]
    }
    first_loop_transport = FakeNatsTransport()
    first_loop_nats = NatsClient(transport=first_loop_transport)
    service.run_daemon_loop(
        config,
        as_json=True,
        nats_client=first_loop_nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )
    graph.agent_board_payload = {
        "runs": [
            {
                "run_id": run_id,
                "task_id": "task-renew-complete",
                "status": "completed",
            }
        ]
    }
    second_loop_transport = FakeNatsTransport()
    second_loop_nats = NatsClient(transport=second_loop_transport)

    service.run_daemon_loop(
        config,
        as_json=True,
        nats_client=second_loop_nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    verified = SQLiteFleetLeaseStore(config.fleet_lease_store_path)
    task = verified.get_task_record("task-renew-complete")
    try:
        assert verified.get_active_lease("task", "task-renew-complete") is None
        assert task is not None
        assert task.status == "completed"
    finally:
        verified.close()


def test_daemon_loop_wrong_run_terminal_row_does_not_release_current_lease(
    tmp_path: Path,
) -> None:
    config = _config_with_nats_and_leases(tmp_path)
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = RespectRunIdGraphClient()
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None
    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
        {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": HOST_ID,
            "task_id": "task-wrong-run",
            "message_id": "msg-wrong-run",
            "delivery_id": "delivery-wrong-run",
            "message": "Ignore stale terminal rows.",
        }
    )
    runtime.close()
    graph.agent_board_payload = {
        "runs": [
            {
                "run_id": "run-old",
                "task_id": "task-wrong-run",
                "status": "completed",
            }
        ]
    }

    loop_transport = FakeNatsTransport()
    loop_nats = NatsClient(transport=loop_transport)
    service.run_daemon_loop(
        config,
        as_json=True,
        nats_client=loop_nats,
        graph_client=graph,
        sleep=lambda seconds: None,
        max_iterations=1,
    )

    verified = SQLiteFleetLeaseStore(config.fleet_lease_store_path)
    task = verified.get_task_record("task-wrong-run")
    try:
        assert verified.get_active_lease("task", "task-wrong-run") is not None
        assert task is not None
        assert task.status == "accepted"
    finally:
        verified.close()


def test_graph_payload_missing_run_id_does_not_release_current_lease(
    tmp_path: Path,
) -> None:
    config = _config_with_nats_and_leases(tmp_path)
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    graph = RespectRunIdGraphClient()
    runtime = service.setup_nats_runtime(
        config,
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=graph,
    )
    assert runtime is not None
    transport.subscriptions[f"openclaw.deliver.{HOST_ID}.tasks"](
        {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": HOST_ID,
            "task_id": "task-missing-run",
            "message_id": "msg-missing-run",
            "delivery_id": "delivery-missing-run",
            "message": "Ignore malformed terminal row.",
        }
    )
    active = runtime.lease_store.get_active_lease("task", "task-missing-run")
    assert active is not None

    released = service.release_terminal_task_leases_from_graph_payload(
        runtime.task_inbox,
        {"runs": [{"task_id": "task-missing-run", "status": "completed"}]},
    )

    task = runtime.lease_store.get_task_record("task-missing-run")
    try:
        assert released == []
        assert (
            runtime.lease_store.get_active_lease("task", "task-missing-run")
            == active
        )
        assert task is not None
        assert task.status == "accepted"
    finally:
        runtime.close()


def test_nats_message_ack_and_reply_are_sent_after_delivery_processing(
    tmp_path: Path,
) -> None:
    transport = FakeNatsTransport()
    nats = NatsClient(transport=transport)
    runtime = service.setup_nats_runtime(
        _config(tmp_path),
        HostIdentity(host_id=HOST_ID),
        nats_client=nats,
        graph_client=FakeGraphClient(),
    )
    assert runtime is not None
    message = FakeNatsMessage(
        {
            "kind": "openclaw.host_delivery",
            "schema_version": 1,
            "host_id": HOST_ID,
            "message_id": "msg-reply",
            "delivery_id": "delivery-reply",
            "message_type": "chat",
            "room_id": "room-1",
            "body": "FYI",
        },
        reply="_INBOX.ack",
    )

    transport.subscriptions[f"openclaw.host.{HOST_ID}.inbox"](message)
    runtime.close()

    assert message.ack_count == 1
    assert [subject for subject, _ in transport.published] == [
        f"openclaw.host.{HOST_ID}.messages.ack",
        "_INBOX.ack",
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


def test_configured_context_store_unavailable_emits_degraded_context_health(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unavailable_path = tmp_path / "context-store-is-directory"
    unavailable_path.mkdir()
    base = _config(tmp_path)
    config = HostDaemonConfig(
        state_dir=base.state_dir,
        host_identity_path=base.host_identity_path,
        repo_roots=base.repo_roots,
        graph_server_url=base.graph_server_url,
        graph_server_token=base.graph_server_token,
        ssh_hostname=base.ssh_hostname,
        heartbeat_interval_seconds=base.heartbeat_interval_seconds,
        nats_url=base.nats_url,
        fleet_lease_store_path=base.fleet_lease_store_path,
        config_path=base.config_path,
        context_store_path=unavailable_path,
    )

    payload = service.run_once(
        config,
        as_json=True,
        context_probe=service._configured_context_probe(config),
        active_agent_runs=[
            AgentRunState(
                agent_id="agent-1",
                task_id="task-1",
                run_id="run-1",
            )
        ],
    )
    capsys.readouterr()

    assert payload["context"]["metrics"][0]["degraded_reasons"] == [
        "fumemory_unavailable"
    ]
    assert payload["context"]["health_flags"] == [
        {
            "run_id": "run-1",
            "severity": "warning",
            "event_kind": "context_manager_degraded",
            "reasons": ["fumemory_unavailable"],
        }
    ]
