"""Stable host identity for the OpenClaw host daemon."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HOST_ID_PATTERN = re.compile(r"^host_[0-9a-f]{32}$")


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


def load_or_create_host_identity(path: Path) -> HostIdentity:
    identity_path = path.expanduser()
    if identity_path.is_file():
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{identity_path}: host identity must be a JSON object")
        return HostIdentity(
            host_id=_coerce_host_id(payload.get("host_id"), source=identity_path)
        )

    identity = HostIdentity(host_id=_new_host_id())
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(identity.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return identity
