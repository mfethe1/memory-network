from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.telegram import handle_telegram_webhook

SIGNING_SECRET = "test-secret"
TELEGRAM_SECRET = "telegram-secret"
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_openclaw_telegram.py"
)


def _load_bootstrap_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bootstrap_openclaw_telegram",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load bootstrap module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store(tmp_path: Path) -> MessagingStore:
    return MessagingStore(tmp_path / "messages.db", signing_secret=SIGNING_SECRET)


def _telegram_update(
    *,
    update_id: int = 100,
    message_id: int = 200,
    text: str = "Please check this.",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": -100123, "type": "group", "title": "OpenClaw"},
            "from": {"id": 42, "username": "operator", "first_name": "Operator"},
            "text": text,
        },
    }


def test_bootstrap_telegram_is_idempotent_and_promotes_assign_commands(
    tmp_path: Path,
) -> None:
    module = _load_bootstrap_module()
    store = _store(tmp_path)
    try:
        first = module.bootstrap_telegram(
            store,
            telegram_chat_id="-100123",
            telegram_user_id="42",
            openclaw_identity_id="operator-1",
            operator_display_name="Operator",
        )
        second = module.bootstrap_telegram(
            store,
            telegram_chat_id="-100123",
            telegram_user_id="42",
            openclaw_identity_id="operator-1",
            operator_display_name="Operator",
        )

        mapping = store.find_platform_room_mapping(
            adapter_id="telegram",
            platform_room_id="-100123",
        )
        identity = store.get_external_identity("telegram", "42")
        promoted = handle_telegram_webhook(
            store,
            _telegram_update(text="/assign task-123 @rosie repair the inbox test"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )

        assert first["room_created"] is True
        assert second["room_created"] is False
        assert first["room"]["room_id"] == module.DEFAULT_FLEET_ROOM_ID
        assert second["room"]["room_id"] == first["room"]["room_id"]
        assert second["mapping"]["mapping_id"] == first["mapping"]["mapping_id"]
        assert second["identity"]["identity_link_id"] == (
            first["identity"]["identity_link_id"]
        )
        assert len(store.list_adapters()) == 7
        assert len(store.list_rooms()) == 1
        assert len(store.list_platform_room_mappings()) == 1
        assert first["adapter"]["command_promotion_enabled"] is True
        assert mapping is not None
        assert mapping["metadata"]["room_role"] == "telegram_operator_group"
        assert mapping["route_policy"] == {
            "command_promotion": {
                "enabled": True,
                "allowed_command_types": ["assign_task"],
                "allowed_target_kinds": ["task"],
            },
            "routing": {"allowed_host_aliases": ["lenny", "rosie"]},
        }
        assert identity is not None
        assert identity["openclaw_identity_id"] == "operator-1"
        assert set(identity["scopes"]) == {"message:write", "command:write"}
        assert promoted["command_ref"] is not None
        assert promoted["command_ref"]["command_type"] == "assign_task"
        assert promoted["command_ref"]["task_id"] == "task-123"
        assert promoted["message"]["metadata"]["routing"]["host_alias"] == "rosie"
    finally:
        store.close()


def test_bootstrap_telegram_reuses_existing_mapped_fleet_room(tmp_path: Path) -> None:
    module = _load_bootstrap_module()
    store = _store(tmp_path)
    try:
        existing_room = store.create_room(
            room_kind="fleet",
            display_name="Existing Fleet",
            metadata={"assignment": {"provider": "codex"}},
        )
        existing_mapping = store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=existing_room["room_id"],
            route_policy={"command_promotion": {"enabled": False}},
            metadata={"room_role": "legacy"},
        )

        result = module.bootstrap_telegram(
            store,
            telegram_chat_id="-100123",
            telegram_user_id="42",
            openclaw_identity_id="operator-1",
        )

        mapping = store.find_platform_room_mapping(
            adapter_id="telegram",
            platform_room_id="-100123",
        )

        assert result["room_created"] is False
        assert result["room"]["room_id"] == existing_room["room_id"]
        assert len(store.list_rooms()) == 1
        assert mapping is not None
        assert mapping["mapping_id"] == existing_mapping["mapping_id"]
        assert mapping["room_id"] == existing_room["room_id"]
        assert mapping["metadata"]["room_role"] == "telegram_operator_group"
        assert mapping["route_policy"]["command_promotion"]["enabled"] is True
        assert mapping["route_policy"]["routing"]["allowed_host_aliases"] == [
            "lenny",
            "rosie",
        ]
    finally:
        store.close()
