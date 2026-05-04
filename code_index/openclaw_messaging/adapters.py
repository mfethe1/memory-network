"""Adapter contract for OpenClaw external messaging surfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from code_index.openclaw_messaging.models import MessageDraft


@dataclass(frozen=True)
class AdapterCapabilities:
    threads: bool = False
    edits: bool = False
    reactions: bool = False
    attachments: bool = False
    rich_text: bool = False
    outbound_notifications: bool = True
    command_promotion: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "threads": self.threads,
            "edits": self.edits,
            "reactions": self.reactions,
            "attachments": self.attachments,
            "rich_text": self.rich_text,
            "outbound_notifications": self.outbound_notifications,
            "command_promotion": self.command_promotion,
        }


@dataclass(frozen=True)
class AdapterHealth:
    status: str = "active"
    detail: str | None = None
    checked_at: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class DeliveryAck:
    delivery_status: str
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MessagingAdapter(ABC):
    adapter_id: str
    adapter_type: str
    display_name: str
    command_promotion_enabled: bool = False

    @abstractmethod
    def normalize_inbound(self, payload: Mapping[str, Any], **kwargs: Any) -> MessageDraft:
        """Convert a platform event into a canonical OpenClaw message draft."""

    @abstractmethod
    def render_outbound(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        """Render an OpenClaw delivery into platform-native send payloads."""

    def acknowledge_delivery(self, payload: Mapping[str, Any]) -> DeliveryAck:
        status = str(payload.get("delivery_status") or "delivered").strip().lower()
        error = payload.get("error")
        return DeliveryAck(
            delivery_status=status,
            error=str(error) if error else None,
            metadata={
                str(key): value
                for key, value in payload.items()
                if key not in {"delivery_status", "error"}
            },
        )

    def health(self) -> AdapterHealth:
        return AdapterHealth(status="active")

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities()


class StubMessagingAdapter(MessagingAdapter):
    """Policy-only adapter registration for platforms implemented later."""

    def __init__(
        self,
        *,
        adapter_id: str,
        adapter_type: str,
        display_name: str,
        capabilities: AdapterCapabilities | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.adapter_type = adapter_type
        self.display_name = display_name
        self._capabilities = capabilities or AdapterCapabilities()

    def normalize_inbound(self, payload: Mapping[str, Any], **kwargs: Any) -> MessageDraft:
        raise NotImplementedError(f"{self.adapter_type} inbound normalization is a stub")

    def render_outbound(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_type": self.adapter_type,
            "delivery": dict(delivery),
            "stub": True,
        }

    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities
