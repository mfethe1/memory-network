"""Normalise incoming Agent Task chat payloads."""

from __future__ import annotations

from typing import Any, TypedDict


VALID_EDIT_POLICIES = {"review_before_edit", "apply_after_review", "direct_edit"}


class InvalidEditPolicy(ValueError):
    """Raised when an Agent Task payload contains an unsupported edit policy."""


class ChatSelectedSymbol(TypedDict):
    symbol_uid: str
    canonical_name: str
    kind: str
    def_file: str
    def_line: int


class ChatTaskContext(TypedDict):
    message: str
    selected_paths: list[str]
    selected_nodes: list[str]
    selected_symbols: list[ChatSelectedSymbol]
    edit_policy: str
    provider: str


def normalise_chat_task(payload: dict[str, Any]) -> ChatTaskContext:
    """Return the canonical chat/task context shape accepted by graph-server."""

    edit_policy = str(payload.get("edit_policy") or "review_before_edit").strip()
    if edit_policy not in VALID_EDIT_POLICIES:
        raise InvalidEditPolicy(
            f"edit_policy must be one of {sorted(VALID_EDIT_POLICIES)}, got {edit_policy!r}"
        )

    return {
        "message": str(payload.get("message") or payload.get("prompt") or "").strip(),
        "selected_paths": _unique_strings(payload.get("selected_paths")),
        "selected_nodes": _unique_strings(payload.get("selected_nodes")),
        "selected_symbols": _selected_symbols(payload.get("selected_symbols")),
        "edit_policy": edit_policy,
        "provider": str(payload.get("provider") or "").strip().lower(),
    }


def _unique_strings(value: Any) -> list[str]:
    if value is None:
        items: list[Any] = []
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _selected_symbols(value: Any) -> list[ChatSelectedSymbol]:
    if not isinstance(value, list):
        return []

    selected: list[ChatSelectedSymbol] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        selected.append(
            {
                "symbol_uid": str(item.get("symbol_uid") or "").strip(),
                "canonical_name": str(item.get("canonical_name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "def_file": str(item.get("def_file") or "").strip(),
                "def_line": _int_or_zero(item.get("def_line")),
            }
        )
    return selected


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
