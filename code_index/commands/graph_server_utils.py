"""Small shared helpers for the live graph server."""

from __future__ import annotations

import hmac
import json
from typing import Any


GRAPH_TOKEN_ENV_VAR = "CODE_INDEX_GRAPH_TOKEN"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")


def _validate_bearer(auth_header: str | None, expected: str) -> bool:
    if not auth_header or not expected:
        return False
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip(), expected.strip())


def _validate_token(value: str | None, expected: str) -> bool:
    if not value or not expected:
        return False
    return hmac.compare_digest(value.strip(), expected.strip())


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out
