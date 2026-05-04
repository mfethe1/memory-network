"""Notification rules for high-signal OpenClaw room events."""

from __future__ import annotations

from typing import Any, Mapping

from code_index.openclaw_messaging.models import NOTIFICATION_EVENTS


DEFAULT_NOTIFICATION_RULES: dict[str, dict[str, Any]] = {
    "needs_attention": {"severity": "warning", "notify": True},
    "blocked": {"severity": "warning", "notify": True},
    "failed": {"severity": "critical", "notify": True},
    "completed": {"severity": "info", "notify": True},
    "lease_conflict": {"severity": "critical", "notify": True},
    "verification_blocked": {"severity": "warning", "notify": True},
}


def notification_rules() -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in DEFAULT_NOTIFICATION_RULES.items()}


def should_notify(event_type: str, *, policy: Mapping[str, Any] | None = None) -> bool:
    event = str(event_type or "").strip().lower()
    if event not in NOTIFICATION_EVENTS:
        return False
    if policy and event in policy:
        return bool(policy[event])
    return bool(DEFAULT_NOTIFICATION_RULES[event]["notify"])


def notification_metadata(event_type: str) -> dict[str, Any]:
    event = str(event_type or "").strip().lower()
    if event not in DEFAULT_NOTIFICATION_RULES:
        return {"event_type": event, "notify": False}
    out = dict(DEFAULT_NOTIFICATION_RULES[event])
    out["event_type"] = event
    return out
