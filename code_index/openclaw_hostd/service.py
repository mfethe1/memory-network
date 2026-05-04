"""Minimal OpenClaw host daemon service entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from typing import Sequence

from code_index.openclaw_hostd.config import HostDaemonConfig, load_config
from code_index.openclaw_hostd.graph_client import GraphServerClient
from code_index.openclaw_hostd.graph_client import GraphServerResponse
from code_index.openclaw_hostd.heartbeat import build_heartbeat_payload
from code_index.openclaw_hostd.identity import load_or_create_host_identity
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.inbox import HostInbox, TaskInbox
from code_index.openclaw_hostd.logging import get_logger, redact_mapping
from code_index.openclaw_hostd.nats_client import AgentRunState, NatsClient
from code_index.openclaw_hostd.nats_client import NatsUnavailableError
from code_index.openclaw_hostd.nats_client import create_nats_transport
from code_index.openclaw_hostd.nats_client import publish_agent_state_entries
from code_index.openclaw_hostd.outbox import EventOutbox


ActiveRunProvider = Callable[[], Iterable[AgentRunState | Mapping[str, Any]]]
NatsTransportFactory = Callable[[str], Any]
_STOPPED_ACTIVE_RUN_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "canceled",
        "review",
        "needs_review",
        "needs-review",
        "done",
    }
)


@dataclass(frozen=True)
class HostDaemonNatsRuntime:
    nats_client: Any
    outbox: Any
    task_inbox: TaskInbox
    host_inbox: HostInbox


class DisabledGraphServerClient:
    def agent_board(self) -> GraphServerResponse:
        return GraphServerResponse(
            ok=True,
            status_code=None,
            payload={"active_runs": []},
        )

    def submit_task(self, **payload: Any) -> GraphServerResponse:
        return GraphServerResponse(
            ok=False,
            status_code=None,
            error="graph-server URL is not configured",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-index-openclaw-hostd",
        description="Run the OpenClaw host daemon skeleton.",
    )
    parser.add_argument(
        "--config",
        help="Path to a JSON OpenClaw host daemon config file.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Emit one heartbeat and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print heartbeat payloads as JSON.",
    )
    parser.add_argument(
        "--probe-graph-server",
        action="store_true",
        help="Check local graph-server availability during heartbeat generation.",
    )
    return parser


def _emit_payload(payload: dict[str, object], *, as_json: bool) -> None:
    safe_payload = redact_mapping(payload)
    if as_json:
        print(json.dumps(safe_payload, indent=2, sort_keys=True))
        return

    graph_server = safe_payload["capabilities"]["graph_server"]  # type: ignore[index]
    print(
        "openclaw host heartbeat "
        f"host_id={safe_payload['host_id']} "
        f"graph_server_available={graph_server['available']}"
    )


def _probe_graph_server_provider_registry(
    url: str,
    *,
    bearer_token: str | None = None,
) -> bool:
    try:
        return GraphServerClient(
            url,
            timeout=0.5,
            bearer_token=bearer_token,
        ).health().available
    except ValueError:
        return False


def empty_active_run_provider() -> tuple[AgentRunState, ...]:
    return ()


def create_configured_nats_client(
    config: HostDaemonConfig,
    *,
    transport_factory: NatsTransportFactory | None = None,
) -> NatsClient | None:
    if not config.nats_url:
        return None
    factory = transport_factory or create_nats_transport
    return NatsClient(transport=factory(config.nats_url))


def graph_server_active_run_provider(
    graph_client: Any | None,
    *,
    logger: Any | None = None,
) -> ActiveRunProvider:
    def _provider() -> tuple[AgentRunState, ...]:
        if graph_client is None:
            return ()
        try:
            agent_board = getattr(graph_client, "agent_board")
            response = agent_board()
        except Exception as exc:
            _logger_debug(
                logger,
                "OpenClaw graph-server active run lookup failed: %s",
                exc,
            )
            return ()
        ok = bool(getattr(response, "ok", True))
        if not ok:
            _logger_debug(
                logger,
                "OpenClaw graph-server active run lookup returned unavailable: %s",
                getattr(response, "error", None),
            )
            return ()
        payload = getattr(response, "payload", response)
        if not isinstance(payload, Mapping):
            return ()
        return tuple(_agent_run_states_from_graph_payload(payload))

    return _provider


def _agent_run_states_from_graph_payload(
    payload: Mapping[str, Any],
) -> list[AgentRunState]:
    states: list[AgentRunState] = []
    seen: set[str] = set()
    for run in _graph_payload_runs(payload):
        state = _agent_run_state_from_graph_run(run)
        if state is None or state.run_id in seen:
            continue
        seen.add(state.run_id)
        states.append(state)
    return states


def _graph_payload_runs(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    runs: list[Mapping[str, Any]] = []

    def add_many(value: Any) -> None:
        if isinstance(value, list):
            runs.extend(item for item in value if isinstance(item, Mapping))

    add_many(payload.get("active_runs"))
    add_many(payload.get("runs"))
    agent = payload.get("agent")
    if isinstance(agent, Mapping):
        add_many(agent.get("active_runs"))
    columns = payload.get("columns")
    if isinstance(columns, Mapping):
        for column_name, column in columns.items():
            if str(column_name).strip().lower() == "done":
                continue
            if isinstance(column, Mapping):
                add_many(column.get("runs"))
    return runs


def _agent_run_state_from_graph_run(run: Mapping[str, Any]) -> AgentRunState | None:
    run_id = _run_text(run.get("run_id") or run.get("id"))
    if not run_id:
        return None
    status = _run_text(run.get("status")).lower()
    if status in _STOPPED_ACTIVE_RUN_STATUSES:
        return None
    metadata = run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
    assert isinstance(metadata, Mapping)
    task_id = _run_text(
        run.get("task_id")
        or metadata.get("task_id")
        or metadata.get("openclaw_task_id")
        or run_id
    )
    agent_id = _run_text(
        run.get("agent_id")
        or metadata.get("agent_id")
        or run.get("agent_name")
        or run.get("provider")
        or run_id
    )
    active_files = _tuple_field(
        run.get("active_files")
        or run.get("selected_paths")
        or metadata.get("selected_paths")
    )
    active_symbols = _tuple_field(
        run.get("active_symbols")
        or run.get("selected_nodes")
        or metadata.get("selected_nodes")
    )
    return AgentRunState(
        agent_id=agent_id,
        task_id=task_id,
        run_id=run_id,
        current_subtask=_run_text(
            run.get("current_subtask")
            or run.get("status_message")
            or run.get("message")
            or run.get("status")
        ),
        active_files=tuple(str(item) for item in active_files),
        active_symbols=tuple(str(item) for item in active_symbols),
        loaded_context_handles=tuple(
            item
            for item in _tuple_field(
                run.get("loaded_context_handles")
                or metadata.get("loaded_context_handles")
                or run.get("context_handles")
                or metadata.get("context_handles")
            )
            if isinstance(item, Mapping)
        ),
        estimated_tokens=_run_int(
            run.get("estimated_tokens") or metadata.get("estimated_tokens")
        ),
        approach_history=tuple(
            str(item)
            for item in _tuple_field(
                run.get("approach_history") or metadata.get("approach_history")
            )
        ),
        last_action_at=(
            run.get("last_action_at")
            or run.get("updated_at")
            or run.get("heartbeat_at")
            or run.get("started_at")
        ),
    )


def _tuple_field(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return (text,)
        if isinstance(parsed, list):
            return tuple(parsed)
        return (text,)
    if isinstance(value, Mapping):
        return (dict(value),)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _run_text(value: Any) -> str:
    return str(value or "").strip()


def _run_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _logger_debug(logger: Any | None, message: str, *args: object) -> None:
    debug = getattr(logger, "debug", None)
    if debug is not None:
        debug(message, *args)


def setup_nats_runtime(
    config: HostDaemonConfig,
    identity: HostIdentity,
    *,
    nats_client: Any | None = None,
    nats_transport_factory: NatsTransportFactory | None = None,
    graph_client: Any | None = None,
    outbox: Any | None = None,
    command_ref_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    logger: Any | None = None,
) -> HostDaemonNatsRuntime | None:
    logger = logger or get_logger()
    try:
        nats_client = nats_client or create_configured_nats_client(
            config,
            transport_factory=nats_transport_factory,
        )
        if nats_client is None:
            return None
        if not getattr(nats_client, "connected", False):
            nats_client.connect()
    except (NatsUnavailableError, RuntimeError, OSError, ValueError) as exc:
        logger.warning("OpenClaw NATS unavailable; continuing without NATS: %s", exc)
        return None

    outbox = outbox or EventOutbox(config.state_dir / "event-outbox.db")
    graph_client = graph_client or _graph_client_for_config(config)
    task_inbox = TaskInbox(
        config.state_dir / "task-inbox.db",
        host_id=identity.host_id,
        graph_client=graph_client,
        nats_client=nats_client,
        outbox=outbox,
    )
    host_inbox = HostInbox(
        config.state_dir / "host-inbox.db",
        host_id=identity.host_id,
        nats_client=nats_client,
        outbox=outbox,
        command_ref_verifier=command_ref_verifier,
    )
    nats_client.subscribe(
        f"openclaw.task.{identity.host_id}.assigned",
        lambda message: task_inbox.handle_task_assignment(_message_payload(message)),
    )
    nats_client.subscribe(
        f"openclaw.host.{identity.host_id}.inbox",
        lambda message: host_inbox.handle_message_delivery(_message_payload(message)),
    )
    outbox.drain(nats_client)
    return HostDaemonNatsRuntime(
        nats_client=nats_client,
        outbox=outbox,
        task_inbox=task_inbox,
        host_inbox=host_inbox,
    )


def run_daemon_loop(
    config: HostDaemonConfig,
    *,
    as_json: bool,
    probe_graph_server: bool = False,
    nats_client: Any | None = None,
    nats_transport_factory: NatsTransportFactory | None = None,
    graph_client: Any | None = None,
    outbox: Any | None = None,
    active_run_provider: ActiveRunProvider | None = None,
    command_ref_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    logger: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> None:
    logger = logger or get_logger()
    identity = load_or_create_host_identity(config.host_identity_path)
    runtime = setup_nats_runtime(
        config,
        identity,
        nats_client=nats_client,
        nats_transport_factory=nats_transport_factory,
        graph_client=graph_client,
        outbox=outbox,
        command_ref_verifier=command_ref_verifier,
        logger=logger,
    )
    if active_run_provider is None and runtime is not None:
        provider_graph_client = graph_client
        if provider_graph_client is None:
            provider_graph_client = getattr(runtime.task_inbox, "graph_client", None)
        active_run_provider = graph_server_active_run_provider(
            provider_graph_client,
            logger=logger,
        )
    if active_run_provider is None:
        active_run_provider = empty_active_run_provider
    iterations = 0
    while True:
        run_once(
            config,
            as_json=as_json,
            probe_graph_server=probe_graph_server,
            nats_client=runtime.nats_client if runtime is not None else None,
            active_agent_runs=active_run_provider(),
        )
        if runtime is not None:
            runtime.outbox.drain(runtime.nats_client)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        sleep(config.heartbeat_interval_seconds)


def _graph_client_for_config(config: HostDaemonConfig) -> Any:
    if not config.graph_server_url:
        return DisabledGraphServerClient()
    return GraphServerClient(
        config.graph_server_url,
        bearer_token=config.graph_server_token,
    )


def _message_payload(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return dict(message)
    data = getattr(message, "data", message)
    if isinstance(data, bytes):
        payload = json.loads(data.decode("utf-8"))
    elif isinstance(data, str):
        payload = json.loads(data)
    else:
        raise ValueError("NATS inbox message must be a JSON object")
    if not isinstance(payload, dict):
        raise ValueError("NATS inbox message must be a JSON object")
    return payload


def run_once(
    config: HostDaemonConfig,
    *,
    as_json: bool,
    probe_graph_server: bool = False,
    nats_client: Any | None = None,
    active_agent_runs: Iterable[AgentRunState | Mapping[str, Any]] = (),
) -> dict[str, object]:
    identity = load_or_create_host_identity(config.host_identity_path)
    graph_server_probe = None
    if probe_graph_server:
        graph_server_probe = lambda url: _probe_graph_server_provider_registry(
            url,
            bearer_token=config.graph_server_token,
        )
    payload = build_heartbeat_payload(
        config,
        identity,
        graph_server_probe=graph_server_probe,
        probe_graph_server=probe_graph_server,
    )
    if nats_client is not None:
        publish_agent_state_entries(
            nats_client,
            host_id=identity.host_id,
            active_agent_runs=active_agent_runs,
            heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        )
    _emit_payload(payload, as_json=as_json)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = get_logger()
    try:
        config = load_config(args.config)
        if args.once:
            run_daemon_loop(
                config,
                as_json=bool(args.json),
                probe_graph_server=bool(args.probe_graph_server),
                sleep=lambda seconds: None,
                max_iterations=1,
            )
            return 0

        run_daemon_loop(
            config,
            as_json=bool(args.json),
            probe_graph_server=bool(args.probe_graph_server),
        )
    except KeyboardInterrupt:
        logger.info("OpenClaw host daemon interrupted")
        return 130
    except Exception as exc:
        print(f"OpenClaw host daemon failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
