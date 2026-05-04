"""`code_index fleet-mcp-serve`: Read-heavy Fleet MCP surface for OpenClaw M2.

Exposes six fleet tools over a separate FastMCP server (stdio or HTTP):
  fleet_task_status          - task + run projection.
  fleet_query_agent_states   - Fleet Context Graph (hosts + runs).
  fleet_submit_handoff       - handoff proposal via M1 auth constraints.
  fleet_query_fumemory       - fumemory context pointer queries.
  fleet_get_context_manifest - signed manifest retrieval.
  fleet_publish_work_summary - auditable work-summary event.

Write operations (update, assign, cancel, shell, lease mutation) are NOT
registered.  fleet_submit_handoff is gated by the existing M1 controller
constraints, not by raw fleet/lease mutation.

HTTP transport uses the same bearer-token posture as `mcp-serve`:
  - Token resolved via flag > file > env > generate + persist.
  - Stored at .code_index/fleet-mcp-token (0600 on POSIX).
  - Per-client scopes checked against required fleet read scopes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from code_index.commands.mcp_auth import (
    _generate_token,
    _is_loopback,
    _read_token_file,
    _write_token_file,
)
from code_index.commands.mcp_serve_cmd import _run_http
from code_index.openclaw_controller.fleet_mcp import (
    FLEET_TOOL_DESCRIPTIONS,
    describe_fleet_surface,
)

FLEET_TOKEN_FILENAME = "fleet-mcp-token"
FLEET_TOKEN_ENV_VAR = "OPENCLAW_FLEET_MCP_TOKEN"

_UNAVAILABLE = {
    "error": "mcp Python SDK is not installed",
    "hint": "install the `mcp` package, or use `--describe` to inspect the static surface",
}

__all__ = [
    "FLEET_TOKEN_FILENAME",
    "describe_fleet_surface",
    "FLEET_TOOL_DESCRIPTIONS",
    "run",
]


def _mcp_available() -> bool:
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    except Exception:
        return False
    return True


def _resolve_fleet_token(
    *,
    flag: str | None = None,
    token_dir: Path | None = None,
    env_var: str = FLEET_TOKEN_ENV_VAR,
    filename: str = FLEET_TOKEN_FILENAME,
) -> str:
    if flag:
        return flag
    token_dir = token_dir or Path(".code_index")
    path = token_dir / filename
    if path.is_file():
        from_file = _read_token_file(path)
        if from_file:
            return from_file
    from_env = os.environ.get(env_var, "").strip()
    if from_env:
        return from_env
    token = _generate_token()
    _write_token_file(path, token)
    print(
        "code_index fleet-mcp-serve: generated bearer token "
        f"at {path} (env {env_var})",
        file=sys.stderr,
    )
    return token


def run(args: argparse.Namespace | list[str] | None = None) -> int:
    """Entry point for `code_index fleet-mcp-serve`."""
    if not isinstance(args, argparse.Namespace):
        parser = _build_parser()
        args = parser.parse_args(args)

    if args.describe:
        print(json.dumps(describe_fleet_surface(), indent=2, sort_keys=True))
        return 0

    if not _mcp_available():
        print(json.dumps(_UNAVAILABLE))
        return 1

    fleet_controller = _load_fleet_controller(args)
    if fleet_controller is None:
        print(json.dumps({"error": "fleet controller is not available"}))
        return 1

    context_store = _load_context_store(args)

    from code_index.openclaw_controller.fleet_mcp import build_fleet_fastmcp

    mcp_server = build_fleet_fastmcp(
        fleet_controller=fleet_controller,
        context_store=context_store,
    )

    transport = args.transport.lower()
    if transport == "http":
        host = args.host or "127.0.0.1"
        port = args.port or 8766
        if not _is_loopback(host) and not args.allow_remote:
            print(
                json.dumps(
                    {
                        "error": "non-loopback bind requires --allow-remote",
                        "hint": "pass --allow-remote to bind to non-loopback addresses",
                    }
                )
            )
            return 1
        token = _resolve_fleet_token(flag=args.token)
        print(json.dumps({"transport": "http", "host": host, "port": port}))
        return _run_http(mcp_server, host=host, port=port, expected_token=token)
    else:
        mcp_server.run(transport="stdio")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "OpenClaw fleet MCP server - read-heavy fleet tools over stdio or HTTP."
        )
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print the static fleet tool surface and exit.",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http"],
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--host", default=None, help="HTTP host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: 8766).")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow non-loopback HTTP bind.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token override for HTTP transport.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path for the context store (optional).",
    )
    return parser


def _load_fleet_controller(args: argparse.Namespace) -> Any | None:
    # For the CLI entry point, we return an in-memory FleetController as the
    # default.  Production deployments wire a persistent store here.
    try:
        from code_index.openclaw_controller.scheduler import FleetController
        from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
        from code_index.openclaw_messaging.store import MessagingStore

        signing_secret = os.environ.get("OPENCLAW_CONTROLLER_SIGNING_SECRET", "")
        if not signing_secret:
            signing_secret = _generate_token()
        store = MessagingStore(":memory:", signing_secret=signing_secret)
        return FleetController(
            messaging_store=store,
            lease_store=InMemoryFleetLeaseStore(),
        )
    except Exception:
        return None


def _load_context_store(args: argparse.Namespace) -> Any | None:
    db_path = getattr(args, "db", None)
    if not db_path:
        return None
    try:
        from code_index.openclaw_context.store import SQLiteContextStore

        return SQLiteContextStore(db_path)
    except Exception:
        return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
