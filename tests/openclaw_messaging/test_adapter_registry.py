from __future__ import annotations

from pathlib import Path

from code_index.openclaw_messaging.adapter_registry import AdapterRegistry
from code_index.openclaw_messaging.store import MessagingStore


SIGNING_SECRET = "test-secret"


def _store(tmp_path: Path) -> MessagingStore:
    return MessagingStore(tmp_path / "messages.db", signing_secret=SIGNING_SECRET)


def test_default_external_adapters_register_without_command_promotion(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        registry = AdapterRegistry(store)
        registry.register_defaults()

        adapters = {adapter["adapter_id"]: adapter for adapter in store.list_adapters()}

        assert {
            "slack",
            "discord",
            "matrix",
            "email",
            "webhook",
        } <= set(adapters)
        assert adapters["slack"]["adapter_type"] == "slack"
        assert adapters["discord"]["capabilities"]["threads"] is True
        for adapter_id in ("slack", "discord", "matrix", "email", "webhook"):
            assert adapters[adapter_id]["command_promotion_enabled"] is False
    finally:
        store.close()


def test_slack_or_discord_inbound_cannot_create_command_until_identity_and_policy_allow(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        room = store.create_room(
            room_kind="task",
            display_name="Task 123",
            task_id="task-123",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"}
                ]
            },
        )

        blocked = store.ingest_adapter_message(
            adapter_id="slack",
            platform_user_id="U123",
            room_id=room["room_id"],
            body="/assign task-123 host-a",
            message_type="command",
            command_type="assign_task",
            platform_ref={
                "platform_room_id": "C123",
                "platform_event_id": "event-1",
            },
            target_scope={"kind": "task", "task_id": "task-123"},
        )

        assert blocked["message"]["message_type"] == "chat"
        assert blocked["message"]["metadata"]["command_promotion"] == "blocked"
        assert store.get_command_ref_for_message(blocked["message"]["message_id"]) is None

        store.set_adapter_command_promotion("slack", enabled=True)
        store.link_external_identity(
            adapter_id="slack",
            platform_user_id="U123",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )
        still_blocked = store.ingest_adapter_message(
            adapter_id="slack",
            platform_user_id="U123",
            room_id=room["room_id"],
            body="/assign task-123 host-a",
            message_type="command",
            command_type="assign_task",
            platform_ref={
                "platform_room_id": "C123",
                "platform_event_id": "event-2",
            },
            target_scope={"kind": "task", "task_id": "task-123"},
        )
        assert still_blocked["message"]["message_type"] == "chat"
        assert store.get_command_ref_for_message(
            still_blocked["message"]["message_id"]
        ) is None

        store.map_platform_room(
            adapter_id="slack",
            platform_room_id="C123",
            room_id=room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        promoted = store.ingest_adapter_message(
            adapter_id="slack",
            platform_user_id="U123",
            room_id=room["room_id"],
            body="/assign task-123 host-a",
            message_type="command",
            command_type="assign_task",
            platform_ref={
                "platform_room_id": "C123",
                "platform_event_id": "event-3",
            },
            target_scope={"kind": "task", "task_id": "task-123"},
        )

        command = store.get_command_ref_for_message(
            promoted["message"]["message_id"]
        )
        assert promoted["message"]["sender_id"] == "operator-1"
        assert promoted["message"]["message_type"] == "command"
        assert command is not None
        assert store.verify_command_ref(command) is True
    finally:
        store.close()


def test_generic_webhook_can_create_inbound_message_but_not_command(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        room = store.create_room(room_kind="fleet", display_name="Fleet")

        result = store.ingest_adapter_message(
            adapter_id="webhook",
            platform_user_id="external-script",
            room_id=room["room_id"],
            body='{"action":"cancel"}',
            message_type="command",
            command_type="cancel",
            platform_ref={
                "platform_room_id": "incoming",
                "platform_event_id": "webhook-1",
            },
            target_scope={"kind": "fleet"},
        )

        assert result["message"]["message_type"] == "chat"
        assert result["message"]["adapter_id"] == "webhook"
        assert store.get_command_ref_for_message(result["message"]["message_id"]) is None
        assert len(store.list_messages(room["room_id"])) == 1
    finally:
        store.close()
