"""Start the code_index live graph server for an agent plugin session."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

for _parent in Path(__file__).resolve().parents:
    if (_parent / "code_index").is_dir():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from code_index import agent_providers  # noqa: E402
from code_index import agent_sessions  # noqa: E402


def _source_root() -> Path:
    return agent_sessions.find_source_root(Path(__file__).resolve())


def _with_source_pythonpath(env: dict[str, str]) -> dict[str, str]:
    return agent_sessions.with_source_pythonpath(env, source_root=_source_root())


def _resolve_root(root: str) -> Path:
    return agent_sessions.create_target_session(root).root


def _ensure_index(root: Path, env: dict[str, str], *, refresh: bool = False) -> None:
    session = agent_sessions.create_target_session(root)
    policy = (
        agent_sessions.IndexPolicy.REFRESH
        if refresh
        else agent_sessions.IndexPolicy.ENSURE
    )
    agent_sessions.prepare_session_index(
        session,
        env,
        policy=policy,
        check_call=subprocess.check_call,
        python_executable=sys.executable,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start code_index graph-server with optional local agent command dispatch."
    )
    parser.add_argument("--root", default=".", help="repo root to serve")
    parser.add_argument(
        "--scope",
        help="starting directory inside --root for graph focus and task defaults",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", default="8767", help="bind port")
    parser.add_argument(
        "--agent-command",
        help=(
            "local command template for graph-submitted tasks; placeholders include "
            "{message}, {provider_prompt}, {provider_prompt_file}, {last_message}, "
            "{mcp_config_file}, {root}, and {task_json}"
        ),
    )
    parser.add_argument(
        "--provider",
        choices=agent_providers.provider_choices(),
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
        "--ensure-index",
        action="store_true",
        default=True,
        help="initialize .code_index/index.db in --root before starting when missing",
    )
    parser.add_argument(
        "--no-ensure-index",
        dest="ensure_index",
        action="store_false",
        help="fail if --root has no existing .code_index/index.db",
    )
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="run `code_index update --all` before starting when an index already exists",
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
    return agent_sessions.command_executable(command)


def _resolve_agent_command(args: argparse.Namespace) -> tuple[str | None, str]:
    provider = agent_sessions.create_provider_selection(
        args.provider,
        agent_command=args.agent_command,
    )
    return agent_sessions.resolve_agent_command(provider)


def _validate_agent_command(args: argparse.Namespace) -> tuple[bool, str]:
    provider = agent_sessions.create_provider_selection(
        args.provider,
        agent_command=args.agent_command,
    )
    return agent_sessions.validate_agent_command(provider)


def _print_index_plan(
    session: agent_sessions.TargetSession,
    policy: agent_sessions.IndexPolicy,
) -> None:
    db_path = session.root / ".code_index" / "index.db"
    index_dir = session.root / ".code_index"
    if policy == agent_sessions.IndexPolicy.REFRESH and db_path.exists():
        print(f"index will be refreshed at {index_dir}")
    elif policy != agent_sessions.IndexPolicy.NO_INDEX and not db_path.exists():
        print(f"index will be initialized at {index_dir}")


def main() -> int:
    args = parse_args()
    try:
        session = agent_sessions.create_target_session(args.root, scope=args.scope)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    provider = agent_sessions.create_provider_selection(
        args.provider,
        agent_command=args.agent_command,
    )
    policy = agent_sessions.index_policy_from_flags(
        ensure_index=bool(args.ensure_index),
        refresh_index=bool(args.refresh_index),
    )
    if not args.skip_provider_check:
        ok, message = agent_sessions.validate_agent_command(provider)
        if not ok:
            print(f"error: {message}", file=sys.stderr)
            return 2
        if args.check_only:
            print(message)
            _print_index_plan(session, policy)
            return 0
    elif args.check_only:
        print("provider check skipped")
        _print_index_plan(session, policy)
        return 0

    env = agent_sessions.build_graph_env(
        os.environ.copy(),
        provider,
        source_root=_source_root(),
        graph_token=args.graph_token,
        command_timeout=args.command_timeout,
        max_output_events=args.max_output_events,
    )
    try:
        agent_sessions.prepare_session_index(
            session,
            env,
            policy=policy,
            check_call=subprocess.check_call,
            python_executable=sys.executable,
    )
    except subprocess.CalledProcessError as exc:
        print(
            f"error: index setup failed with exit code {exc.returncode}",
            file=sys.stderr,
        )
        return int(exc.returncode) or 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    graph = agent_sessions.GraphDefaults(host=args.host, port=str(args.port))
    return agent_sessions.launch_graph_server(
        session,
        env,
        graph=graph,
        quiet=bool(args.quiet),
        call=subprocess.call,
        python_executable=sys.executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
