"""Start the code_index live graph server for an agent plugin session."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

for _parent in Path(__file__).resolve().parents:
    if (_parent / "code_index").is_dir():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from code_index import agent_providers  # noqa: E402


PROVIDER_COMMANDS = agent_providers.PROVIDER_COMMANDS


def _source_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "code_index").is_dir():
            return parent
    return Path(__file__).resolve().parents[3]


def _with_source_pythonpath(env: dict[str, str]) -> dict[str, str]:
    source = str(_source_root())
    existing = env.get("PYTHONPATH", "")
    parts = [source]
    if existing:
        parts.append(existing)
    next_env = dict(env)
    next_env["PYTHONPATH"] = os.pathsep.join(parts)
    return next_env


def _resolve_root(root: str) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"root does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"root is not a directory: {path}")
    return path


def _ensure_index(root: Path, env: dict[str, str], *, refresh: bool = False) -> None:
    db_path = root / ".code_index" / "index.db"
    if db_path.exists() and not refresh:
        return
    if db_path.exists() and refresh:
        command = [
            sys.executable,
            "-m",
            "code_index",
            "update",
            "--root",
            str(root),
            "--all",
            "--json",
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "code_index",
            "init",
            "--root",
            str(root),
            "--json",
        ]
    subprocess.check_call(command, cwd=str(root), env=env)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start code_index graph-server with optional local agent command dispatch."
    )
    parser.add_argument("--root", default=".", help="repo root to serve")
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
    provider = agent_providers.normalize_provider_id(args.provider)
    if provider != "custom":
        preset = PROVIDER_COMMANDS.get(provider)
        if preset is None:
            agent_providers.require_provider(provider)
            raise ValueError(f"agent provider has no command preset: {provider}")
        return preset, provider
    return None, "custom"


def _validate_agent_command(args: argparse.Namespace) -> tuple[bool, str]:
    try:
        command, provider = _resolve_agent_command(args)
    except ValueError as exc:
        return False, str(exc)
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
    try:
        root = _resolve_root(args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.skip_provider_check:
        ok, message = _validate_agent_command(args)
        if not ok:
            print(f"error: {message}", file=sys.stderr)
            return 2
        if args.check_only:
            print(message)
            if args.refresh_index and (root / ".code_index" / "index.db").exists():
                print(f"index will be refreshed at {root / '.code_index'}")
            elif args.ensure_index and not (root / ".code_index" / "index.db").exists():
                print(f"index will be initialized at {root / '.code_index'}")
            return 0
    elif args.check_only:
        print("provider check skipped")
        if args.refresh_index and (root / ".code_index" / "index.db").exists():
            print(f"index will be refreshed at {root / '.code_index'}")
        elif args.ensure_index and not (root / ".code_index" / "index.db").exists():
            print(f"index will be initialized at {root / '.code_index'}")
        return 0

    env = _with_source_pythonpath(os.environ.copy())
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
    if args.ensure_index or args.refresh_index:
        try:
            _ensure_index(root, env, refresh=bool(args.refresh_index))
        except subprocess.CalledProcessError as exc:
            print(f"error: index setup failed with exit code {exc.returncode}", file=sys.stderr)
            return int(exc.returncode) or 2
    elif not (root / ".code_index" / "index.db").exists():
        print(f"error: no index at {root / '.code_index'}. pass --ensure-index to create one.", file=sys.stderr)
        return 2

    command = [
        sys.executable,
        "-m",
        "code_index",
        "graph-server",
        "--root",
        str(root),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.quiet:
        command.append("--quiet")
    return subprocess.call(command, cwd=str(root), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
