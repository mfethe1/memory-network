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


def load_or_create_host_identity(path: Path) -> HostIdentity:
    identity_path = path.expanduser()
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = identity_path.with_suffix(identity_path.suffix + ".lock")
    deadline = time.monotonic() + IDENTITY_CREATE_TIMEOUT_SECONDS

    while True:
        if identity_path.is_file():
            return _read_host_identity(identity_path)

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
            if identity_path.is_file():
                return _read_host_identity(identity_path)
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
