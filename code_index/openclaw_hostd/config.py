"""Configuration loading for the OpenClaw host daemon."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_PATH_ENV = "OPENCLAW_HOSTD_CONFIG"
STATE_DIR_ENV = "OPENCLAW_HOSTD_STATE_DIR"
HOST_IDENTITY_PATH_ENV = "OPENCLAW_HOSTD_HOST_ID_PATH"
REPO_ROOTS_ENV = "OPENCLAW_HOSTD_REPO_ROOTS"
GRAPH_SERVER_URL_ENV = "OPENCLAW_HOSTD_GRAPH_SERVER_URL"
SSH_HOSTNAME_ENV = "OPENCLAW_HOSTD_SSH_HOSTNAME"
HEARTBEAT_INTERVAL_ENV = "OPENCLAW_HOSTD_HEARTBEAT_INTERVAL_SECONDS"

DEFAULT_GRAPH_SERVER_URL = "http://127.0.0.1:8767/health"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class HostDaemonConfig:
    state_dir: Path
    host_identity_path: Path
    repo_roots: tuple[Path, ...]
    graph_server_url: str | None = DEFAULT_GRAPH_SERVER_URL
    ssh_hostname: str | None = None
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    config_path: Path | None = None


def _default_state_dir() -> Path:
    return Path.home() / ".code_index" / "openclaw_hostd"


def _read_json_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"OpenClaw host daemon config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: OpenClaw host daemon config must be a JSON object")
    return payload


def _optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def _path_tuple(value: Any, *, default: tuple[Path, ...]) -> tuple[Path, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        values = [part for part in value.split(os.pathsep) if part.strip()]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("repo_roots must be a list of paths or pathsep string")
    paths: list[Path] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("repo_roots entries must be non-empty strings")
        paths.append(Path(item).expanduser())
    return tuple(paths)


def _positive_int(value: Any, *, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a string value")
    return value.strip() or None


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> HostDaemonConfig:
    environ = os.environ if env is None else env
    selected_config_path = _optional_path(
        str(config_path) if config_path is not None else environ.get(CONFIG_PATH_ENV)
    )
    data: dict[str, Any] = {}
    if selected_config_path is not None:
        data = _read_json_config(selected_config_path)

    state_dir = _optional_path(environ.get(STATE_DIR_ENV)) or _optional_path(
        data.get("state_dir")
    )
    state_dir = state_dir or _default_state_dir()

    host_identity_path = _optional_path(environ.get(HOST_IDENTITY_PATH_ENV))
    if host_identity_path is None:
        host_identity_path = _optional_path(data.get("host_identity_path"))
    host_identity_path = host_identity_path or state_dir / "host-identity.json"

    root_default = (cwd or Path.cwd(),)
    repo_roots = _path_tuple(data.get("repo_roots"), default=root_default)
    if environ.get(REPO_ROOTS_ENV):
        repo_roots = _path_tuple(environ.get(REPO_ROOTS_ENV), default=repo_roots)

    graph_server_url = _text_or_none(
        environ.get(
            GRAPH_SERVER_URL_ENV,
            data.get("graph_server_url", DEFAULT_GRAPH_SERVER_URL),
        )
    )
    ssh_hostname = _text_or_none(
        environ.get(SSH_HOSTNAME_ENV, data.get("ssh_hostname"))
    )
    heartbeat_interval_seconds = _positive_int(
        environ.get(
            HEARTBEAT_INTERVAL_ENV,
            data.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
        ),
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    )

    return HostDaemonConfig(
        state_dir=state_dir,
        host_identity_path=host_identity_path,
        repo_roots=tuple(repo_roots),
        graph_server_url=graph_server_url,
        ssh_hostname=ssh_hostname,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        config_path=selected_config_path,
    )
