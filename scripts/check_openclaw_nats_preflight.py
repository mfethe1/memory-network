#!/usr/bin/env python3
"""Preflight a host's OpenClaw NATS endpoint without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_index.openclaw_controller.service_config import redact_nats_url


OPENCLAW_NATS_URL_ENV = "OPENCLAW_NATS_URL"
OPENCLAW_HOSTD_NATS_URL_FILE_ENV = "OPENCLAW_HOSTD_NATS_URL_FILE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that an OpenClaw host can reach and authenticate to the "
            "canonical NATS broker."
        )
    )
    parser.add_argument(
        "--nats-url",
        default=os.environ.get(OPENCLAW_NATS_URL_ENV),
        help="NATS URL to test. Defaults to OPENCLAW_NATS_URL.",
    )
    parser.add_argument(
        "--nats-url-file",
        default=os.environ.get(OPENCLAW_HOSTD_NATS_URL_FILE_ENV),
        help=(
            "Protected file containing the NATS URL. Defaults to "
            "OPENCLAW_HOSTD_NATS_URL_FILE and is used when --nats-url is absent."
        ),
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=5.0,
        help="TCP/NATS connect timeout in seconds.",
    )
    parser.add_argument(
        "--allow-unauthenticated-url",
        action="store_true",
        help="Allow a URL without userinfo. Not valid for production host cutover.",
    )
    return parser


def resolve_nats_url(args: argparse.Namespace) -> str:
    url = str(args.nats_url or "").strip()
    if not url and args.nats_url_file:
        url = Path(args.nats_url_file).expanduser().read_text(encoding="utf-8").strip()
    if not url:
        raise ValueError(
            "--nats-url, --nats-url-file, OPENCLAW_NATS_URL, or "
            "OPENCLAW_HOSTD_NATS_URL_FILE is required"
        )
    parsed = urlsplit(url)
    if parsed.scheme not in {"nats", "tls", "ws", "wss"} or not parsed.hostname:
        raise ValueError("NATS URL must be an absolute nats/tls/ws/wss URL")
    if not args.allow_unauthenticated_url and not (parsed.username or parsed.password):
        raise ValueError("NATS URL must include authentication for production cutover")
    if parsed.port is None:
        raise ValueError("NATS URL must include an explicit port")
    return url


def tcp_preflight(nats_url: str, *, timeout: float) -> dict[str, Any]:
    parsed = urlsplit(nats_url)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise ValueError("NATS URL must include host and port")
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return {"host": host, "port": port, "tcp_reachable": True}


async def nats_auth_preflight(
    nats_url: str,
    *,
    timeout: float,
    nats_module: Any | None = None,
) -> dict[str, Any]:
    if nats_module is None:
        try:
            import nats as nats_module  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise RuntimeError(
                "nats-py is required for authenticated NATS preflight; "
                "install the OpenClaw optional dependency with python -m pip install -e .[openclaw]"
            ) from exc
    client = await nats_module.connect(
        servers=[nats_url],
        connect_timeout=timeout,
        allow_reconnect=False,
        max_reconnect_attempts=0,
    )
    try:
        flush = getattr(client, "flush", None)
        if flush is not None:
            result = flush(timeout=timeout)
            if hasattr(result, "__await__"):
                await result
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
    return {"nats_authenticated": True}


async def run_preflight(
    args: argparse.Namespace,
    *,
    nats_module: Any | None = None,
) -> dict[str, Any]:
    nats_url = resolve_nats_url(args)
    tcp = tcp_preflight(nats_url, timeout=args.connect_timeout)
    auth = await nats_auth_preflight(
        nats_url,
        timeout=args.connect_timeout,
        nats_module=nats_module,
    )
    return {
        "ok": True,
        "url": redact_nats_url(nats_url),
        **tcp,
        **auth,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run_preflight(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
