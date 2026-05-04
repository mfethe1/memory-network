"""Reusable target-session helpers for agent/plugin launchers."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from code_index import agent_providers
from code_index import config as cfg_mod
from code_index import scopes

DEFAULT_GRAPH_HOST = "127.0.0.1"
DEFAULT_GRAPH_PORT = "8767"


class IndexPolicy(Enum):
    ENSURE = "ensure"
    REFRESH = "refresh"
    NO_INDEX = "no-index"


@dataclass(frozen=True)
class TargetSession:
    root: Path
    scope: Path


@dataclass(frozen=True)
class AgentProviderSelection:
    provider: str = "custom"
    agent_command: str | None = None


@dataclass(frozen=True)
class GraphDefaults:
    host: str = DEFAULT_GRAPH_HOST
    port: str = DEFAULT_GRAPH_PORT

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/repo-graph.html"


def create_target_session(
    path: str | Path = ".",
    *,
    scope: str | Path | None = None,
    require_exists: bool = True,
) -> TargetSession:
    target = Path(path).expanduser().resolve()
    if require_exists and not target.exists():
        raise ValueError(f"root does not exist: {target}")
    if target.exists() and not target.is_dir():
        raise ValueError(f"root is not a directory: {target}")
    root = cfg_mod.find_root(target) if target.exists() else None
    root = root or target
    if scope is not None:
        scope_selection = scopes.resolve_scope(root, scope)
        return TargetSession(root=root, scope=Path(scope_selection.path))
    try:
        inferred_scope = target.relative_to(root)
    except ValueError:
        inferred_scope = Path(".")
    return TargetSession(root=root, scope=inferred_scope or Path("."))


def create_provider_selection(
    provider: str | None = "custom",
    *,
    agent_command: str | None = None,
) -> AgentProviderSelection:
    normalized = agent_providers.normalize_provider_id(provider)
    agent_providers.require_provider(normalized)
    return AgentProviderSelection(provider=normalized, agent_command=agent_command)


def index_policy_from_flags(*, ensure_index: bool, refresh_index: bool) -> IndexPolicy:
    if refresh_index:
        return IndexPolicy.REFRESH
    if ensure_index:
        return IndexPolicy.ENSURE
    return IndexPolicy.NO_INDEX


def find_source_root(start: str | Path | None = None) -> Path:
    current = Path(start).resolve() if start is not None else Path(__file__).resolve()
    for parent in (current, *current.parents):
        if (parent / "code_index").is_dir():
            return parent
    return Path(__file__).resolve().parents[1]


def with_source_pythonpath(
    env: Mapping[str, str],
    *,
    source_root: str | Path | None = None,
) -> dict[str, str]:
    source = str(
        Path(source_root).resolve() if source_root is not None else find_source_root()
    )
    existing = env.get("PYTHONPATH", "")
    parts = [source]
    if existing:
        parts.append(existing)
    next_env = dict(env)
    next_env["PYTHONPATH"] = os.pathsep.join(parts)
    return next_env


def mcp_server_config(
    *,
    root: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    server: dict[str, Any] = {
        "command": "python",
        "args": ["-m", "code_index", "mcp-serve", "--root", "."],
    }
    resolved_source_root = (
        Path(source_root).resolve() if source_root is not None else find_source_root()
    )
    if root is None or resolved_source_root != Path(root).resolve():
        server["env"] = {"PYTHONPATH": str(resolved_source_root)}
    return server


def build_graph_env(
    base_env: Mapping[str, str],
    provider: AgentProviderSelection,
    *,
    source_root: str | Path | None = None,
    graph_token: str | None = None,
    command_timeout: str | None = None,
    max_output_events: str | None = None,
) -> dict[str, str]:
    env = with_source_pythonpath(base_env, source_root=source_root)
    if provider.agent_command:
        env["CODE_INDEX_AGENT_COMMAND"] = provider.agent_command
    elif provider.provider != "custom":
        env["CODE_INDEX_AGENT_PROVIDER"] = provider.provider
    if graph_token:
        env["CODE_INDEX_GRAPH_TOKEN"] = graph_token
    if command_timeout:
        env["CODE_INDEX_AGENT_COMMAND_TIMEOUT"] = command_timeout
    if max_output_events:
        env["CODE_INDEX_AGENT_MAX_OUTPUT_EVENTS"] = max_output_events
    return env


def resolve_agent_command(provider: AgentProviderSelection) -> tuple[str | None, str]:
    if provider.agent_command:
        return provider.agent_command, "custom"
    if provider.provider != "custom":
        preset = agent_providers.provider_command_template(provider.provider)
        if preset is None:
            raise ValueError(f"agent provider has no command preset: {provider.provider}")
        return preset, provider.provider
    return None, "custom"


def command_executable(command: str) -> str | None:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    return parts[0].strip("\"'")


def validate_agent_command(provider: AgentProviderSelection) -> tuple[bool, str]:
    try:
        command, provider_id = resolve_agent_command(provider)
    except ValueError as exc:
        return False, str(exc)
    if not command:
        return True, "no local agent command configured"
    executable = command_executable(command)
    if not executable:
        return (
            False,
            f"could not parse agent command for provider {provider_id!r}: {command}",
        )
    if Path(executable).is_absolute() and Path(executable).exists():
        return True, f"found {executable}"
    found = shutil.which(executable)
    if found:
        return True, f"found {executable} at {found}"
    return (
        False,
        (
            f"agent provider {provider_id!r} requires executable {executable!r} on PATH. "
            "Install it, pass --agent-command with an absolute command, or use --skip-provider-check."
        ),
    )


CheckCall = Callable[..., object]


def prepare_session_index(
    session: TargetSession,
    env: Mapping[str, str],
    *,
    policy: IndexPolicy = IndexPolicy.ENSURE,
    check_call: CheckCall = subprocess.check_call,
    python_executable: str = sys.executable,
) -> None:
    db_path = session.root / ".code_index" / "index.db"
    if db_path.exists() and policy != IndexPolicy.REFRESH:
        return
    if policy == IndexPolicy.NO_INDEX:
        raise ValueError(
            f"no index at {session.root / '.code_index'}. "
            "pass --ensure-index to create one."
        )
    subcommand = (
        "update" if db_path.exists() and policy == IndexPolicy.REFRESH else "init"
    )
    command = [
        python_executable,
        "-m",
        "code_index",
        subcommand,
        "--root",
        str(session.root),
    ]
    if subcommand == "update":
        command.append("--all")
    command.append("--json")
    check_call(command, cwd=str(session.root), env=dict(env))


def graph_server_command(
    session: TargetSession,
    *,
    graph: GraphDefaults | None = None,
    quiet: bool = False,
    python_executable: str = sys.executable,
) -> list[str]:
    graph = graph or GraphDefaults()
    command = [
        python_executable,
        "-m",
        "code_index",
        "graph-server",
        "--root",
        str(session.root),
        "--host",
        graph.host,
        "--port",
        str(graph.port),
    ]
    scope = str(session.scope).replace("\\", "/")
    if scope and scope != ".":
        command.extend(["--scope", scope])
    if quiet:
        command.append("--quiet")
    return command


def launcher_args(
    launcher_script: str | Path,
    *,
    graph: GraphDefaults | None = None,
    provider: AgentProviderSelection | None = None,
    index_policy: IndexPolicy = IndexPolicy.ENSURE,
    root_arg: str = ".",
    scope_arg: str | Path | None = None,
) -> list[str]:
    graph = graph or GraphDefaults()
    provider = provider or AgentProviderSelection()
    command = [
        "python",
        str(launcher_script),
        "--root",
        root_arg,
        "--host",
        graph.host,
        "--port",
        str(graph.port),
    ]
    if scope_arg is not None and str(scope_arg).strip() not in {"", "."}:
        command.extend(["--scope", str(scope_arg).replace("\\", "/")])
    if index_policy == IndexPolicy.ENSURE:
        command.append("--ensure-index")
    elif index_policy == IndexPolicy.REFRESH:
        command.append("--refresh-index")
    else:
        command.append("--no-ensure-index")
    if provider.agent_command:
        command.extend(["--agent-command", provider.agent_command])
    elif provider.provider != "custom":
        command.extend(["--provider", provider.provider])
    return command


Call = Callable[..., int]


def launch_graph_server(
    session: TargetSession,
    env: Mapping[str, str],
    *,
    graph: GraphDefaults | None = None,
    quiet: bool = False,
    call: Call = subprocess.call,
    python_executable: str = sys.executable,
) -> int:
    return call(
        graph_server_command(
            session,
            graph=graph,
            quiet=quiet,
            python_executable=python_executable,
        ),
        cwd=str(session.root),
        env=dict(env),
    )
