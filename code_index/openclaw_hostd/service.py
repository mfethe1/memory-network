"""Minimal OpenClaw host daemon service entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable, Mapping
from typing import Any
from typing import Sequence

from code_index.openclaw_hostd.config import HostDaemonConfig, load_config
from code_index.openclaw_hostd.graph_client import GraphServerClient
from code_index.openclaw_hostd.heartbeat import build_heartbeat_payload
from code_index.openclaw_hostd.identity import load_or_create_host_identity
from code_index.openclaw_hostd.logging import get_logger, redact_mapping
from code_index.openclaw_hostd.nats_client import AgentRunState
from code_index.openclaw_hostd.nats_client import publish_agent_state_entries


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

        while True:
            run_once(
                config,
                as_json=bool(args.json),
                probe_graph_server=bool(args.probe_graph_server),
            )
            time.sleep(config.heartbeat_interval_seconds)
    except KeyboardInterrupt:
        logger.info("OpenClaw host daemon interrupted")
        return 130
    except Exception as exc:
        print(f"OpenClaw host daemon failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
