"""Models for the minimal OpenClaw Fleet Controller API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


HOST_HEALTHY = "healthy"
HOST_STALE = "stale"
HOST_UNKNOWN = "unknown"
HOST_UNHEALTHY = "unhealthy"
HOST_HEALTH_VALUES = frozenset(
    {HOST_HEALTHY, HOST_STALE, HOST_UNKNOWN, HOST_UNHEALTHY}
)


@dataclass(frozen=True)
class RepoRoot:
    path: str
    exists: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepoRoot":
        return cls(
            path=_required_text(value.get("path"), "repo_root.path"),
            exists=bool(value.get("exists", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists}


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    display_name: str | None = None
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderCapability":
        provider_id = _required_text(value.get("id"), "provider.id")
        display_name = _optional_text(value.get("display_name"))
        capabilities = _string_tuple(value.get("capabilities"))
        return cls(
            provider_id=provider_id,
            display_name=display_name,
            capabilities=capabilities,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
        }

    def has_capabilities(self, required: tuple[str, ...]) -> bool:
        available = {capability.lower() for capability in self.capabilities}
        return all(capability.lower() in available for capability in required)


@dataclass(frozen=True)
class HostInventoryRecord:
    host_id: str
    repo_roots: tuple[RepoRoot, ...]
    providers: tuple[ProviderCapability, ...]
    heartbeat_interval_seconds: int = 10
    last_heartbeat_at: datetime | None = None
    status: str = HOST_HEALTHY
    metadata: Mapping[str, Any] | None = None

    @classmethod
    def from_heartbeat(
        cls,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> "HostInventoryRecord":
        capabilities = (
            payload.get("capabilities")
            if isinstance(payload.get("capabilities"), Mapping)
            else {}
        )
        repo_roots = tuple(
            RepoRoot.from_mapping(item)
            for item in _mapping_list(capabilities.get("repo_roots"))
        )
        providers = tuple(
            ProviderCapability.from_mapping(item)
            for item in _mapping_list(capabilities.get("providers"))
        )
        return cls(
            host_id=_required_text(payload.get("host_id"), "host_id"),
            repo_roots=repo_roots,
            providers=providers,
            heartbeat_interval_seconds=max(
                1,
                _int_or_default(payload.get("heartbeat_interval_seconds"), 10),
            ),
            last_heartbeat_at=_utc(now),
            status=_host_status(payload.get("status")),
            metadata={"heartbeat": dict(payload)},
        )

    def health_at(self, now: datetime) -> str:
        if self.last_heartbeat_at is None:
            return HOST_UNKNOWN
        if self.status != HOST_HEALTHY:
            return self.status
        stale_after = self.heartbeat_interval_seconds * 3
        if (_utc(now) - _utc(self.last_heartbeat_at)).total_seconds() > stale_after:
            return HOST_STALE
        return HOST_HEALTHY

    def supports_repo_root(self, repo_root: str) -> bool:
        requested = normalize_repo_root(repo_root)
        return any(
            root.exists and normalize_repo_root(root.path) == requested
            for root in self.repo_roots
        )

    def supports_provider(self, provider: str) -> bool:
        return self.provider_capability(provider) is not None

    def provider_capability(self, provider: str) -> ProviderCapability | None:
        requested = str(provider or "").strip().lower()
        for capability in self.providers:
            if capability.provider_id.lower() == requested:
                return capability
        return None

    def supports_provider_capabilities(
        self,
        provider: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        capability = self.provider_capability(provider)
        return capability is not None and capability.has_capabilities(
            required_capabilities
        )

    def to_projection(
        self,
        *,
        now: datetime,
        context_health: Mapping[str, Any] | None = None,
        handoff_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "health": self.health_at(now),
            "last_heartbeat_at": (
                _datetime_text(self.last_heartbeat_at)
                if self.last_heartbeat_at is not None
                else None
            ),
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "repo_roots": [root.to_dict() for root in self.repo_roots],
            "providers": [provider.to_dict() for provider in self.providers],
            "context_health": dict(context_health or {}),
            "handoff_state": dict(handoff_state or {}),
        }


@dataclass(frozen=True)
class AssignmentDetails:
    task_id: str
    repo_root: str
    provider: str
    message: str
    selected_paths: tuple[str, ...] = ()
    selected_nodes: tuple[str, ...] = ()
    required_provider_capabilities: tuple[str, ...] = ()
    node: Mapping[str, Any] | None = None
    agent_name: str | None = None


@dataclass(frozen=True)
class TaskAssignment:
    task_id: str
    host_id: str
    repo_root: str
    provider: str
    message_id: str
    delivery_id: str
    nats_subject: str
    repo_lease_id: str
    task_lease_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "host_id": self.host_id,
            "repo_root": self.repo_root,
            "provider": self.provider,
            "message_id": self.message_id,
            "delivery_id": self.delivery_id,
            "nats_subject": self.nats_subject,
            "repo_lease_id": self.repo_lease_id,
            "task_lease_id": self.task_lease_id,
        }


@dataclass(frozen=True)
class Rejection:
    reason: str
    message: str
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"reason": self.reason, "message": self.message}
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class AssignmentResult:
    status: str
    assignment: TaskAssignment | None
    rejection: Rejection | None
    room_message_update: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "assignment": (
                self.assignment.to_dict() if self.assignment is not None else None
            ),
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "room_message_update": dict(self.room_message_update),
        }


@dataclass(frozen=True)
class HandoffResult:
    status: str
    handoff: Mapping[str, Any] | None
    rejection: Rejection | None
    room_message_update: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "handoff": dict(self.handoff) if self.handoff is not None else None,
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "room_message_update": dict(self.room_message_update),
        }


def normalize_repo_root(value: str) -> str:
    text = _required_text(value, "repo_root")
    text = text.replace("\\", "/").rstrip("/")
    return text.lower()


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _host_status(value: object) -> str:
    status = (_optional_text(value) or HOST_HEALTHY).lower()
    if status in HOST_HEALTH_VALUES:
        return status
    return HOST_UNKNOWN


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: object) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
