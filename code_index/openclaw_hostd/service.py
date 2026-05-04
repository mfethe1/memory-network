"""Minimal OpenClaw host daemon service entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Sequence

from code_index.openclaw_hostd.config import HostDaemonConfig, load_config
from code_index.openclaw_hostd.heartbeat import build_heartbeat_payload
from code_index.openclaw_hostd.identity import load_or_create_host_identity
from code_index.openclaw_hostd.logging import get_logger, redact_mapping


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


def run_once(config: HostDaemonConfig, *, as_json: bool) -> dict[str, object]:
    identity = load_or_create_host_identity(config.host_identity_path)
    payload = build_heartbeat_payload(config, identity)
    _emit_payload(payload, as_json=as_json)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = get_logger()
    try:
        config = load_config(args.config)
        if args.once:
            run_once(config, as_json=bool(args.json))
            return 0

        while True:
            run_once(config, as_json=bool(args.json))
            time.sleep(config.heartbeat_interval_seconds)
    except KeyboardInterrupt:
        logger.info("OpenClaw host daemon interrupted")
        return 130
    except Exception as exc:
        print(f"OpenClaw host daemon failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
