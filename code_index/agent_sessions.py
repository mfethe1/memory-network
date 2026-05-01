"""Reusable target-session helpers for agent/plugin launchers."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from code_index import config as cfg_mod


class IndexPolicy(Enum):
    ENSURE = "ensure"
    REFRESH = "refresh"
    NO_INDEX = "no-index"


@dataclass(frozen=True)
class TargetSession:
    root: Path
    scope: Path


def create_target_session(path: str | Path = ".") -> TargetSession:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"root does not exist: {target}")
    if not target.is_dir():
        raise ValueError(f"root is not a directory: {target}")
    root = cfg_mod.find_root(target) or target
    try:
        scope = target.relative_to(root)
    except ValueError:
        scope = Path(".")
    return TargetSession(root=root, scope=scope or Path("."))


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
            f"no index at {session.root / '.code_index'}. pass --ensure-index to create one."
        )
    subcommand = "update" if db_path.exists() and policy == IndexPolicy.REFRESH else "init"
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
