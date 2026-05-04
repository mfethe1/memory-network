"""Shared models for OpenClaw Messaging Service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


ROOM_KINDS = frozenset({"fleet", "repo", "task", "run", "host", "swarm"})
MESSAGE_TYPES = frozenset({"chat", "command", "event", "summary", "alert"})
SENDER_KINDS = frozenset({"human", "agent", "controller", "host", "system"})
RECIPIENT_KINDS = frozenset({"host", "run", "agent", "adapter", "web", "controller"})
DELIVERY_STATUSES = frozenset(
    {"queued", "delivered", "acked", "failed", "expired"}
)
ADAPTER_TYPES = frozenset(
    {"web", "telegram", "slack", "discord", "matrix", "email", "webhook", "cli"}
)
NOTIFICATION_EVENTS = frozenset(
    {
        "needs_attention",
        "blocked",
        "failed",
        "completed",
        "lease_conflict",
        "verification_blocked",
    }
)


@dataclass(frozen=True)
class MessageDraft:
    room_id: str
    sender_kind: str
    sender_id: str
    body: str
    message_type: str = "chat"
    target_scope: dict[str, Any] = field(default_factory=dict)
    context_handles: list[dict[str, Any]] = field(default_factory=list)
    adapter_id: str | None = None
    platform_ref: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    correlation_id: str | None = None
    parent_message_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    command_type: str | None = None
    platform_user_id: str | None = None


class MessagingError(ValueError):
    """Base error for invalid messaging input."""


def require_choice(value: str, *, choices: frozenset[str], field_name: str) -> str:
    text = str(value or "").strip().lower()
    if text not in choices:
        allowed = ", ".join(sorted(choices))
        raise MessagingError(f"{field_name} must be one of: {allowed}")
    return text


def normalize_recipient(recipient: Mapping[str, Any]) -> dict[str, Any]:
    kind = require_choice(
        str(recipient.get("recipient_kind") or ""),
        choices=RECIPIENT_KINDS,
        field_name="recipient_kind",
    )
    recipient_id = str(recipient.get("recipient_id") or "").strip()
    if not recipient_id:
        raise MessagingError("recipient_id is required")
    out = {"recipient_kind": kind, "recipient_id": recipient_id}
    for key, value in recipient.items():
        if key not in out and value is not None:
            out[str(key)] = value
    return out


def normalize_recipient_list(recipients: Any) -> list[dict[str, Any]]:
    if recipients is None:
        return []
    if not isinstance(recipients, list):
        raise MessagingError("recipients must be a list")
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in recipients:
        if not isinstance(item, Mapping):
            raise MessagingError("each recipient must be an object")
        recipient = normalize_recipient(item)
        extra = tuple(
            sorted((key, repr(value)) for key, value in recipient.items())
        )
        key = (
            recipient["recipient_kind"],
            recipient["recipient_id"],
            repr(extra),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(recipient)
    return out
