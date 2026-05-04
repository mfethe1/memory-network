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
from code_index.openclaw_hostd.heartbeat import build_heartbeat_payload
from code_index.openclaw_hostd.identity import load_or_create_host_identity
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.inbox import HostInbox, TaskInbox
from code_index.openclaw_hostd.logging import get_logger, redact_mapping
from code_index.openclaw_hostd.nats_client import AgentRunState, NatsClient
from code_index.openclaw_hostd.nats_client import NatsUnavailableError
from code_index.openclaw_hostd.nats_client import publish_agent_state_entries
from code_index.openclaw_hostd.outbox import EventOutbox


ActiveRunProvider = Callable[[], Iterable[AgentRunState | Mapping[str, Any]]]


@dataclass(frozen=True)
class HostDaemonNatsRuntime:
    nats_client: Any
    outbox: Any
    task_inbox: TaskInbox
    host_inbox: HostInbox


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


def create_configured_nats_client(config: HostDaemonConfig) -> NatsClient | None:
    if not config.nats_url:
        return None
    return NatsClient()


def setup_nats_runtime(
    config: HostDaemonConfig,
    identity: HostIdentity,
    *,
    nats_client: Any | None = None,
    graph_client: Any | None = None,
    outbox: Any | None = None,
    command_ref_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    logger: Any | None = None,
) -> HostDaemonNatsRuntime | None:
    logger = logger or get_logger()
    nats_client = nats_client or create_configured_nats_client(config)
    if nats_client is None:
        return None
    try:
        if not getattr(nats_client, "connected", False):
            nats_client.connect()
    except (NatsUnavailableError, RuntimeError, OSError) as exc:
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
    graph_client: Any | None = None,
    outbox: Any | None = None,
    active_run_provider: ActiveRunProvider = empty_active_run_provider,
    command_ref_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> None:
    identity = load_or_create_host_identity(config.host_identity_path)
    runtime = setup_nats_runtime(
        config,
        identity,
        nats_client=nats_client,
        graph_client=graph_client,
        outbox=outbox,
        command_ref_verifier=command_ref_verifier,
    )
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


def _graph_client_for_config(config: HostDaemonConfig) -> GraphServerClient:
    if not config.graph_server_url:
        raise ValueError("graph_server_url is required for NATS task inbox setup")
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
            run_once(
                config,
                as_json=bool(args.json),
                probe_graph_server=bool(args.probe_graph_server),
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
