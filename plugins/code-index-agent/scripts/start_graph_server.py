"""Start the code_index live graph server for an agent plugin session."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


PROVIDER_COMMANDS = {
    "claude": "claude -p {message}",
    "codex": "codex exec {message}",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start code_index graph-server with optional local agent command dispatch."
    )
    parser.add_argument("--root", default=".", help="repo root to serve")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", default="8768", help="bind port")
    parser.add_argument(
        "--agent-command",
        help=(
            "local command template for graph-submitted tasks, for example "
            "'claude -p {message}' or 'codex exec {message}'"
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["custom", "claude", "codex"],
        default="custom",
        help="provider preset used when --agent-command is omitted",
    )
    parser.add_argument(
        "--graph-token",
        help="optional bearer token required for browser POSTs and callbacks",
    )
    parser.add_argument(
        "--command-timeout",
        help="optional seconds before a local agent command is marked failed",
    )
    parser.add_argument(
        "--max-output-events",
        help="optional max stdout/stderr lines posted as tool events",
    )
    parser.add_argument(
        "--skip-provider-check",
        action="store_true",
        help="skip PATH checks for the selected local agent command",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate configuration and exit without starting graph-server",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress HTTP request logs")
    return parser.parse_args()


def _command_executable(command: str) -> str | None:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    return parts[0].strip("\"'")


def _resolve_agent_command(args: argparse.Namespace) -> tuple[str | None, str]:
    if args.agent_command:
        return args.agent_command, "custom"
    if args.provider != "custom":
        return PROVIDER_COMMANDS[args.provider], args.provider
    return None, "custom"


def _validate_agent_command(args: argparse.Namespace) -> tuple[bool, str]:
    command, provider = _resolve_agent_command(args)
    if not command:
        return True, "no local agent command configured"
    executable = _command_executable(command)
    if not executable:
        return False, f"could not parse agent command for provider {provider!r}: {command}"
    if Path(executable).is_absolute() and Path(executable).exists():
        return True, f"found {executable}"
    found = shutil.which(executable)
    if found:
        return True, f"found {executable} at {found}"
    return (
        False,
        (
            f"agent provider {provider!r} requires executable {executable!r} on PATH. "
            "Install it, pass --agent-command with an absolute command, or use --skip-provider-check."
        ),
    )


def main() -> int:
    args = parse_args()
    if not args.skip_provider_check:
        ok, message = _validate_agent_command(args)
        if not ok:
            print(f"error: {message}", file=sys.stderr)
            return 2
        if args.check_only:
            print(message)
            return 0
    elif args.check_only:
        print("provider check skipped")
        return 0

    env = os.environ.copy()
    if args.agent_command:
        env["CODE_INDEX_AGENT_COMMAND"] = args.agent_command
    elif args.provider != "custom":
        env["CODE_INDEX_AGENT_PROVIDER"] = args.provider
    if args.graph_token:
        env["CODE_INDEX_GRAPH_TOKEN"] = args.graph_token
    if args.command_timeout:
        env["CODE_INDEX_AGENT_COMMAND_TIMEOUT"] = args.command_timeout
    if args.max_output_events:
        env["CODE_INDEX_AGENT_MAX_OUTPUT_EVENTS"] = args.max_output_events

    command = [
        sys.executable,
        "-m",
        "code_index",
        "graph-server",
        "--root",
        args.root,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.quiet:
        command.append("--quiet")
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
