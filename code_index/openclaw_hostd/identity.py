"""Stable host identity for the OpenClaw host daemon."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HOST_ID_PATTERN = re.compile(r"^host_[0-9a-f]{32}$")
IDENTITY_CREATE_TIMEOUT_SECONDS = 5.0
IDENTITY_CREATE_RETRY_SECONDS = 0.01


@dataclass(frozen=True)
class HostIdentity:
    host_id: str

    def to_dict(self) -> dict[str, str]:
        return {"host_id": self.host_id}


def _new_host_id() -> str:
    return f"host_{uuid.uuid4().hex}"


def _coerce_host_id(value: Any, *, source: Path) -> str:
    if not isinstance(value, str) or not HOST_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{source}: host_id must match {HOST_ID_PATTERN.pattern}")
    return value


def _read_host_identity(path: Path) -> HostIdentity:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: host identity must be a JSON object")
    return HostIdentity(host_id=_coerce_host_id(payload.get("host_id"), source=path))


def _write_host_identity_atomic(path: Path, identity: HostIdentity) -> None:
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(identity.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _wait_for_identity_retry(
    deadline: float,
    *,
    path: Path,
    cause: BaseException,
) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError(
            f"timed out waiting for readable host identity: {path}"
        ) from cause
    time.sleep(IDENTITY_CREATE_RETRY_SECONDS)


def _read_existing_host_identity(
    path: Path,
    *,
    lock_path: Path,
    deadline: float,
) -> tuple[HostIdentity | None, bool]:
    try:
        file_present = path.is_file()
    except OSError as exc:
        _wait_for_identity_retry(deadline, path=path, cause=exc)
        return None, True

    if not file_present:
        return None, False

    try:
        return _read_host_identity(path), True
    except (OSError, json.JSONDecodeError) as exc:
        _wait_for_identity_retry(deadline, path=path, cause=exc)
        return None, True
    except ValueError as exc:
        if not lock_path.exists():
            raise
        _wait_for_identity_retry(deadline, path=path, cause=exc)
        return None, True


def load_or_create_host_identity(path: Path) -> HostIdentity:
    identity_path = path.expanduser()
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = identity_path.with_suffix(identity_path.suffix + ".lock")
    deadline = time.monotonic() + IDENTITY_CREATE_TIMEOUT_SECONDS

    while True:
        identity, file_present = _read_existing_host_identity(
            identity_path,
            lock_path=lock_path,
            deadline=deadline,
        )
        if identity is not None:
            return identity
        if file_present:
            continue

        lock_fd: int | None = None
        try:
            lock_fd = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for host identity lock: {lock_path}"
                )
            time.sleep(IDENTITY_CREATE_RETRY_SECONDS)
            continue

        try:
            identity, file_present = _read_existing_host_identity(
                identity_path,
                lock_path=lock_path,
                deadline=deadline,
            )
            if identity is not None:
                return identity
            if file_present:
                continue

            identity = HostIdentity(host_id=_new_host_id())
            _write_host_identity_atomic(identity_path, identity)
            return identity
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
