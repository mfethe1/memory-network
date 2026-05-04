"""Shared OpenClaw service configuration helpers for long-running services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Mapping

from code_index.openclaw_hostd.nats_client import _nats_url


OPENCLAW_DEPLOYMENT_MODE_ENV = "OPENCLAW_DEPLOYMENT_MODE"
OPENCLAW_BIND_HOST_ENV = "OPENCLAW_BIND_HOST"
OPENCLAW_REQUIRE_NATS_ENV = "OPENCLAW_REQUIRE_NATS"
OPENCLAW_CONTROLLER_DB_PATH_ENV = "OPENCLAW_CONTROLLER_DB_PATH"
OPENCLAW_MESSAGING_DB_PATH_ENV = "OPENCLAW_MESSAGING_DB_PATH"
OPENCLAW_CONTEXT_STORE_PATH_ENV = "OPENCLAW_CONTEXT_STORE_PATH"
OPENCLAW_NATS_URL_ENV = "OPENCLAW_NATS_URL"
OPENCLAW_SIGNING_SECRET_ENV = "OPENCLAW_CONTROLLER_SIGNING_SECRET"
OPENCLAW_TELEGRAM_SECRET_ENV = "OPENCLAW_TELEGRAM_SECRET_TOKEN"
OPENCLAW_TELEGRAM_BOT_TOKEN_ENV = "OPENCLAW_TELEGRAM_BOT_TOKEN"
RAILWAY_VOLUME_MOUNT_PATH_ENV = "RAILWAY_VOLUME_MOUNT_PATH"

DEFAULT_DB_SUBDIR = "openclaw"
DEFAULT_CONTROLLER_DB_FILENAME = "controller-state.db"
DEFAULT_MESSAGING_DB_FILENAME = "messaging.db"
DEFAULT_CONTEXT_STORE_FILENAME = "context-store.db"

_STRICT_MODES = frozenset({"production", "railway"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class OpenClawConfigError(ValueError):
    """Raised when a long-running OpenClaw service is misconfigured."""


@dataclass(frozen=True)
class OpenClawDeploymentPaths:
    deployment_mode: str
    volume_mount_path: Path | None
    controller_db_path: Path
    messaging_db_path: Path
    context_store_path: Path

    @property
    def strict_mode(self) -> bool:
        return self.deployment_mode in _STRICT_MODES


def resolve_deployment_mode(environ: Mapping[str, str] | None = None) -> str:
    env = dict(os.environ if environ is None else environ)
    explicit = str(env.get(OPENCLAW_DEPLOYMENT_MODE_ENV, "")).strip().lower()
    if explicit:
        if explicit not in {"development", "production", "railway"}:
            raise OpenClawConfigError(
                f"{OPENCLAW_DEPLOYMENT_MODE_ENV} must be development, production, or railway"
            )
        return explicit
    if any(
        str(env.get(name, "")).strip()
        for name in (
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_SERVICE_ID",
            "RAILWAY_PROJECT_ID",
            RAILWAY_VOLUME_MOUNT_PATH_ENV,
        )
    ):
        return "railway"
    return "development"


def resolve_bind_host(environ: Mapping[str, str] | None = None) -> str:
    env = dict(os.environ if environ is None else environ)
    configured = str(env.get(OPENCLAW_BIND_HOST_ENV, "")).strip()
    if configured:
        return configured
    mode = resolve_deployment_mode(env)
    return "::" if mode in _STRICT_MODES else "127.0.0.1"


def resolve_port(
    environ: Mapping[str, str] | None = None,
    *,
    default: int,
) -> int:
    env = dict(os.environ if environ is None else environ)
    raw = str(env.get("PORT", "")).strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise OpenClawConfigError("PORT must be an integer") from exc
    if port <= 0 or port > 65535:
        raise OpenClawConfigError("PORT must be between 1 and 65535")
    return port


def resolve_require_nats(environ: Mapping[str, str] | None = None) -> bool:
    env = dict(os.environ if environ is None else environ)
    raw = str(env.get(OPENCLAW_REQUIRE_NATS_ENV, "")).strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return resolve_deployment_mode(env) in _STRICT_MODES


def resolve_nats_url(environ: Mapping[str, str] | None = None) -> str | None:
    env = dict(os.environ if environ is None else environ)
    value = str(env.get(OPENCLAW_NATS_URL_ENV, "")).strip()
    if not value:
        return None
    try:
        return _nats_url(value)
    except ValueError as exc:
        raise OpenClawConfigError(str(exc)) from exc


def resolve_service_paths(
    environ: Mapping[str, str] | None = None,
) -> OpenClawDeploymentPaths:
    env = dict(os.environ if environ is None else environ)
    mode = resolve_deployment_mode(env)
    volume_mount_path = _resolve_volume_mount_path(env, mode)
    controller = resolve_sqlite_path(
        env,
        env_var=OPENCLAW_CONTROLLER_DB_PATH_ENV,
        default_filename=DEFAULT_CONTROLLER_DB_FILENAME,
        deployment_mode=mode,
        volume_mount_path=volume_mount_path,
    )
    messaging = resolve_sqlite_path(
        env,
        env_var=OPENCLAW_MESSAGING_DB_PATH_ENV,
        default_filename=DEFAULT_MESSAGING_DB_FILENAME,
        deployment_mode=mode,
        volume_mount_path=volume_mount_path,
    )
    context = resolve_sqlite_path(
        env,
        env_var=OPENCLAW_CONTEXT_STORE_PATH_ENV,
        default_filename=DEFAULT_CONTEXT_STORE_FILENAME,
        deployment_mode=mode,
        volume_mount_path=volume_mount_path,
    )
    return OpenClawDeploymentPaths(
        deployment_mode=mode,
        volume_mount_path=volume_mount_path,
        controller_db_path=controller,
        messaging_db_path=messaging,
        context_store_path=context,
    )


def resolve_context_store_path(
    db_path: str | os.PathLike[str] | None,
    *,
    environ: Mapping[str, str] | None = None,
    required: bool | None = None,
) -> Path | None:
    env = dict(os.environ if environ is None else environ)
    mode = resolve_deployment_mode(env)
    volume_mount_path = _resolve_volume_mount_path(env, mode)
    resolved = resolve_sqlite_path(
        env,
        env_var=OPENCLAW_CONTEXT_STORE_PATH_ENV,
        default_filename=DEFAULT_CONTEXT_STORE_FILENAME,
        deployment_mode=mode,
        volume_mount_path=volume_mount_path,
        explicit_value=db_path,
        required=required,
    )
    return resolved


def resolve_sqlite_path(
    environ: Mapping[str, str],
    *,
    env_var: str,
    default_filename: str,
    deployment_mode: str,
    volume_mount_path: Path | None,
    explicit_value: str | os.PathLike[str] | None = None,
    required: bool | None = None,
) -> Path | None:
    strict_mode = deployment_mode in _STRICT_MODES
    path_text = _configured_path_text(explicit_value, environ.get(env_var))
    if path_text is None:
        if volume_mount_path is not None:
            candidate = volume_mount_path / DEFAULT_DB_SUBDIR / default_filename
        elif required is False:
            return None
        elif strict_mode:
            raise OpenClawConfigError(
                f"{env_var} is required in {deployment_mode} mode unless "
                f"{RAILWAY_VOLUME_MOUNT_PATH_ENV} is set"
            )
        else:
            candidate = Path.cwd() / ".openclaw" / default_filename
    else:
        if _is_in_memory_sqlite_path(path_text):
            raise OpenClawConfigError(
                f"{env_var} must point to a persistent SQLite file, not {path_text!r}"
            )
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            if strict_mode:
                raise OpenClawConfigError(
                    f"{env_var} must be an absolute path in {deployment_mode} mode"
                )
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
    if strict_mode:
        volume_backed = False
        if deployment_mode == "railway":
            if volume_mount_path is None:
                raise OpenClawConfigError(
                    f"{RAILWAY_VOLUME_MOUNT_PATH_ENV} is required in railway mode"
                )
            try:
                candidate.relative_to(volume_mount_path)
                volume_backed = True
            except ValueError as exc:
                raise OpenClawConfigError(
                    f"{env_var} must be stored under {volume_mount_path}"
                ) from exc
        if not volume_backed and _is_ephemeral_path(candidate):
            raise OpenClawConfigError(
                f"{env_var} must not use an ephemeral path: {candidate}"
            )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists() and candidate.is_dir():
        raise OpenClawConfigError(f"{env_var} must be a file path, not a directory")
    return candidate


def redact_nats_url(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    if parsed.scheme and host:
        return f"{parsed.scheme}://{host}{port}"
    return None


def _configured_path_text(
    explicit_value: str | os.PathLike[str] | None,
    env_value: str | None,
) -> str | None:
    for value in (explicit_value, env_value):
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _resolve_volume_mount_path(
    environ: Mapping[str, str],
    deployment_mode: str,
) -> Path | None:
    raw = str(environ.get(RAILWAY_VOLUME_MOUNT_PATH_ENV, "")).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise OpenClawConfigError(
            f"{RAILWAY_VOLUME_MOUNT_PATH_ENV} must be an absolute path"
        )
    if deployment_mode == "railway" and not path.exists():
        raise OpenClawConfigError(
            f"{RAILWAY_VOLUME_MOUNT_PATH_ENV} does not exist: {path}"
        )
    return path.resolve()


def _is_in_memory_sqlite_path(value: str) -> bool:
    text = value.strip().lower()
    return text == ":memory:" or text.startswith("file::memory:")


def _is_ephemeral_path(path: Path) -> bool:
    ephemeral_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
        Path("/var/tmp").resolve(),
        Path("/dev/shm").resolve(),
    }
    for root in ephemeral_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
