"""Adapter registry setup for OpenClaw Messaging Service."""

from __future__ import annotations

from dataclasses import dataclass

from code_index.openclaw_messaging.adapters import AdapterCapabilities
from code_index.openclaw_messaging.adapters import StubMessagingAdapter
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.telegram import TelegramAdapter


@dataclass(frozen=True)
class AdapterRegistration:
    adapter_id: str
    adapter_type: str
    display_name: str
    capabilities: dict[str, bool]
    command_promotion_enabled: bool = False


class AdapterRegistry:
    def __init__(self, store: MessagingStore) -> None:
        self.store = store

    def register_defaults(self) -> list[dict[str, object]]:
        return [
            self.register(registration)
            for registration in default_adapter_registrations()
        ]

    def register(self, registration: AdapterRegistration) -> dict[str, object]:
        return self.store.register_adapter(
            adapter_id=registration.adapter_id,
            adapter_type=registration.adapter_type,
            display_name=registration.display_name,
            capabilities=registration.capabilities,
            command_promotion_enabled=registration.command_promotion_enabled,
        )


def default_adapter_registrations() -> list[AdapterRegistration]:
    web_capabilities = AdapterCapabilities(
        threads=True,
        edits=True,
        reactions=True,
        attachments=True,
        rich_text=True,
        command_promotion=True,
    ).to_dict()
    telegram = TelegramAdapter()
    external_threaded = AdapterCapabilities(
        threads=True,
        edits=False,
        reactions=True,
        attachments=True,
        rich_text=True,
    )
    email = AdapterCapabilities(
        threads=True,
        attachments=True,
        rich_text=False,
    )
    webhook = AdapterCapabilities(
        outbound_notifications=True,
        command_promotion=False,
    )
    stubs = [
        StubMessagingAdapter(
            adapter_id="slack",
            adapter_type="slack",
            display_name="Slack",
            capabilities=external_threaded,
        ),
        StubMessagingAdapter(
            adapter_id="discord",
            adapter_type="discord",
            display_name="Discord",
            capabilities=external_threaded,
        ),
        StubMessagingAdapter(
            adapter_id="matrix",
            adapter_type="matrix",
            display_name="Matrix",
            capabilities=external_threaded,
        ),
        StubMessagingAdapter(
            adapter_id="email",
            adapter_type="email",
            display_name="Email",
            capabilities=email,
        ),
        StubMessagingAdapter(
            adapter_id="webhook",
            adapter_type="webhook",
            display_name="Generic Webhook",
            capabilities=webhook,
        ),
    ]
    registrations = [
        AdapterRegistration(
            adapter_id="web",
            adapter_type="web",
            display_name="OpenClaw Web UI",
            capabilities=web_capabilities,
            command_promotion_enabled=True,
        ),
        AdapterRegistration(
            adapter_id=telegram.adapter_id,
            adapter_type=telegram.adapter_type,
            display_name=telegram.display_name,
            capabilities=telegram.capabilities().to_dict(),
            command_promotion_enabled=False,
        ),
    ]
    registrations.extend(
        AdapterRegistration(
            adapter_id=stub.adapter_id,
            adapter_type=stub.adapter_type,
            display_name=stub.display_name,
            capabilities=stub.capabilities().to_dict(),
            command_promotion_enabled=False,
        )
        for stub in stubs
    )
    return registrations
