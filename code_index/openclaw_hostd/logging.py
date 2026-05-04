"""Redaction-safe logging helpers for the OpenClaw host daemon."""

from __future__ import annotations

import logging as py_logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
SECRET_FIELD_MARKERS = (
    "api_key",
    "auth",
    "credential",
    "password",
    "secret",
    "token",
)


def is_secret_field(name: str) -> bool:
    normalized = name.lower()
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)


def redact_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return REDACTED
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return REDACTED
    netloc = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        return REDACTED
    if port is not None:
        netloc = f"{netloc}:{port}"
    redacted = SplitResult(
        scheme=parts.scheme,
        netloc=netloc,
        path="",
        query="",
        fragment="",
    )
    return urlunsplit(redacted)


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if is_secret_field(str(key)):
            redacted[str(key)] = REDACTED
        elif isinstance(item, Mapping):
            redacted[str(key)] = redact_mapping(item)
        elif isinstance(item, list):
            redacted[str(key)] = [
                redact_mapping(entry) if isinstance(entry, Mapping) else entry
                for entry in item
            ]
        else:
            redacted[str(key)] = item
    return redacted


def get_logger(name: str = "code_index.openclaw_hostd") -> py_logging.Logger:
    return py_logging.getLogger(name)
