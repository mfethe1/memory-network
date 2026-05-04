"""Background launcher for Graph Agent Companion.

The installed ``index`` console command starts the repo-scoped agent plugin
launcher in a detached child process so the user's shell is immediately usable.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from code_index import agent_providers
from code_index import agent_sessions

LOG_FILENAME = "graph-agent-companion.log"
PID_FILENAME = "graph-agent-companion.pid"

PopenFactory = Callable[..., Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="index",
        description=(
            "Launch Graph Agent Companion for the current directory in the background."
        ),
    )
    parser.add_argument(
        "--root",
        default=".",
        help="directory to serve (default: current directory / nearest .code_index)",
    )
    parser.add_argument(
        "--scope",
        help=(
            "starting directory inside --root for graph focus; defaults to the "
            "current directory when it is inside an indexed root"
        ),
    )
    parser.add_argument("--host", default=agent_sessions.DEFAULT_GRAPH_HOST)
    parser.add_argument("--port", default=agent_sessions.DEFAULT_GRAPH_PORT)
    parser.add_argument(
        "--provider",
        choices=agent_providers.provider_choices(),
        default="custom",
        help="provider preset used when --agent-command is omitted",
    )
    parser.add_argument(
        "--agent-command",
        help="custom local agent command template; overrides --provider",
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
        "--ensure-index",
        action="store_true",
        default=True,
        help="initialize .code_index/index.db in --root before starting when missing",
    )
    parser.add_argument(
        "--no-ensure-index",
        dest="ensure_index",
        action="store_false",
        help="fail in the background process if no index already exists",
    )
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="refresh an existing index before starting",
    )
    parser.add_argument(
        "--skip-provider-check",
        action="store_true",
        help="skip PATH checks for the selected local agent command",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate configuration and print the launch command without starting",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress graph-server HTTP request logs",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="run in the foreground instead of detaching",
    )
    parser.add_argument(
        "--log-file",
        help=(
            "background log path (default: .code_index/"
            f"{LOG_FILENAME} under the resolved root)"
        ),
    )
    return parser


def _policy_from_args(args: argparse.Namespace) -> agent_sessions.IndexPolicy:
    return agent_sessions.index_policy_from_flags(
        ensure_index=bool(args.ensure_index),
        refresh_index=bool(args.refresh_index),
    )


def _runtime_paths(root: Path, log_file: str | None) -> tuple[Path, Path]:
    index_dir = root / ".code_index"
    index_dir.mkdir(parents=True, exist_ok=True)
    if log_file:
        requested = Path(log_file).expanduser()
        log_path = requested if requested.is_absolute() else root / requested
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        log_path = index_dir / LOG_FILENAME
    return log_path, index_dir / PID_FILENAME


def _session_scope_arg(session: agent_sessions.TargetSession) -> str | None:
    scope = str(session.scope).replace("\\", "/")
    if scope and scope != ".":
        return scope
    return None


def _build_agent_plugin_command(
    args: argparse.Namespace,
    session: agent_sessions.TargetSession,
    *,
    python_executable: str,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "code_index",
        "agent-plugin",
        "start",
        "--root",
        str(session.root),
    ]
    scope = _session_scope_arg(session)
    if scope:
        command.extend(["--scope", scope])
    command.extend(["--host", str(args.host), "--port", str(args.port)])
    command.extend(["--provider", args.provider])
    if args.agent_command:
        command.extend(["--agent-command", args.agent_command])
    if args.graph_token:
        command.extend(["--graph-token", args.graph_token])
    if args.command_timeout:
        command.extend(["--command-timeout", args.command_timeout])
    if args.max_output_events:
        command.extend(["--max-output-events", args.max_output_events])
    policy = _policy_from_args(args)
    if policy == agent_sessions.IndexPolicy.REFRESH:
        command.append("--refresh-index")
    elif policy == agent_sessions.IndexPolicy.ENSURE:
        command.append("--ensure-index")
    else:
        command.append("--no-ensure-index")
    if args.skip_provider_check:
        command.append("--skip-provider-check")
    if args.quiet:
        command.append("--quiet")
    return command


def _detached_kwargs(root: Path, env: dict[str, str], log_handle: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _validate_provider(args: argparse.Namespace) -> tuple[bool, str]:
    provider = agent_sessions.create_provider_selection(
        args.provider,
        agent_command=args.agent_command,
    )
    return agent_sessions.validate_agent_command(provider)


def _print_check(
    session: agent_sessions.TargetSession,
    command: Sequence[str],
    provider_message: str,
) -> None:
    print(provider_message)
    print(f"root: {session.root}")
    scope = _session_scope_arg(session) or "."
    print(f"scope: {scope}")
    print("command: " + " ".join(command))


def _print_launched(
    *,
    pid: int,
    url: str,
    log_path: Path,
    pid_path: Path,
) -> None:
    print("Graph Agent Companion launched in background.")
    print(f"PID: {pid}")
    print(f"URL: {url}")
    print(f"Log: {log_path}")
    print(f"PID file: {pid_path}")


def main(
    argv: Sequence[str] | None = None,
    *,
    popen: PopenFactory = subprocess.Popen,
    python_executable: str = sys.executable,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        session = agent_sessions.create_target_session(args.root, scope=args.scope)
        provider_message = "provider check skipped"
        if not args.skip_provider_check:
            ok, provider_message = _validate_provider(args)
            if not ok:
                print(f"error: {provider_message}", file=sys.stderr)
                return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    command = _build_agent_plugin_command(
        args,
        session,
        python_executable=python_executable,
    )
    if args.check_only:
        _print_check(session, command, provider_message)
        return 0

    env = agent_sessions.with_source_pythonpath(os.environ.copy())
    graph = agent_sessions.GraphDefaults(host=str(args.host), port=str(args.port))
    if args.foreground:
        return subprocess.call(command, cwd=str(session.root), env=env)

    log_path, pid_path = _runtime_paths(session.root, args.log_file)
    try:
        with log_path.open("ab") as log_handle:
            process = popen(
                command,
                **_detached_kwargs(session.root, env, log_handle),
            )
    except OSError as exc:
        print(f"error: failed to launch background process: {exc}", file=sys.stderr)
        return 2

    pid = int(process.pid)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")
    _print_launched(pid=pid, url=graph.url, log_path=log_path, pid_path=pid_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
