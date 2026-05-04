from __future__ import annotations

from pathlib import Path

from code_index.openclaw_messaging.adapter_registry import AdapterRegistry
from code_index.openclaw_messaging.routes import MessagingRouter
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.telegram import TelegramAdapter
from code_index.openclaw_messaging.telegram import handle_telegram_webhook
from code_index.openclaw_messaging.telegram import poll_telegram_updates

SIGNING_SECRET = "test-secret"
TELEGRAM_SECRET = "telegram-secret"


class FakeTelegramHttpClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, object]]] = []

    def __call__(
        self,
        url: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.requests.append((url, dict(payload)))
        if not self.responses:
            raise AssertionError("unexpected Telegram poll request")
        return dict(self.responses.pop(0))


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
            "reply_to_message": {"message_id": 99},
        },
    }


def test_telegram_reply_creates_same_canonical_envelope_as_web_ui_message(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        room = store.create_room(room_kind="task", display_name="Task", task_id="task-1")
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
        )
        store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write",),
            display_name="Operator",
        )

        web_message = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Please check this.",
            message_type="chat",
            target_scope={"kind": "task", "task_id": "task-1"},
        )["message"]
        telegram_message = handle_telegram_webhook(
            store,
            _telegram_update(text="Please check this."),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )["message"]

        comparable_keys = (
            "room_id",
            "sender_kind",
            "sender_id",
            "message_type",
            "body",
            "target_scope",
        )
        assert {key: web_message[key] for key in comparable_keys} == {
            key: telegram_message[key] for key in comparable_keys
        }
        assert telegram_message["adapter_id"] == "telegram"
        assert telegram_message["platform_ref"]["platform_message_id"] == "200"
    finally:
        store.close()


def test_platform_room_mapping_upsert_is_idempotent_for_default_thread(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        first_room = store.create_room(room_kind="task", display_name="First")
        second_room = store.create_room(room_kind="task", display_name="Second")
        thread_room = store.create_room(room_kind="task", display_name="Thread")

        first = store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=first_room["room_id"],
        )
        second = store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=second_room["room_id"],
        )
        thread = store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            platform_thread_id="99",
            room_id=thread_room["room_id"],
        )

        assert second["mapping_id"] == first["mapping_id"]
        assert thread["mapping_id"] != second["mapping_id"]
        assert store.find_platform_room_mapping(
            adapter_id="telegram",
            platform_room_id="-100123",
        )["room_id"] == second_room["room_id"]
        assert store.find_platform_room_mapping(
            adapter_id="telegram",
            platform_room_id="-100123",
            platform_thread_id="99",
        )["room_id"] == thread_room["room_id"]
        assert len(store.list_platform_room_mappings()) == 2
    finally:
        store.close()


def test_telegram_webhook_requires_valid_secret_before_ingest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        store.set_adapter_command_promotion("telegram", enabled=True)
        room = store.create_room(
            room_kind="task",
            display_name="Task",
            task_id="task-1",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"}
                ]
            },
        )
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        for provided in (None, "forged"):
            try:
                handle_telegram_webhook(
                    store,
                    _telegram_update(text="/assign task-1 host-a"),
                    secret_token=TELEGRAM_SECRET,
                    provided_secret_token=provided,
                )
            except PermissionError:
                pass
            else:
                raise AssertionError("forged Telegram webhook was accepted")

        valid = handle_telegram_webhook(
            store,
            _telegram_update(text="/assign task-1 host-a"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )

        assert store.list_messages(room["room_id"]) == [valid["message"]]
        assert valid["command_ref"] is not None
    finally:
        store.close()


def test_telegram_route_uses_secret_token_header(tmp_path: Path) -> None:
    store = _store(tmp_path)
    router = MessagingRouter(store, telegram_secret_token=TELEGRAM_SECRET)
    try:
        AdapterRegistry(store).register_defaults()
        room = store.create_room(room_kind="task", display_name="Task", task_id="task-1")
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
        )
        store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write",),
            display_name="Operator",
        )

        forged = router.handle(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(text="Please check this."),
        )
        valid = router.handle(
            "POST",
            "/adapters/telegram/webhook",
            _telegram_update(text="Please check this."),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

        assert forged.status_code == 403
        assert valid.status_code == 201
        assert len(store.list_messages(room["room_id"])) == 1
    finally:
        store.close()


def test_replayed_telegram_update_does_not_duplicate_commands_or_deliveries(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        store.set_adapter_command_promotion("telegram", enabled=True)
        room = store.create_room(
            room_kind="task",
            display_name="Task",
            task_id="task-1",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"},
                    {"recipient_kind": "run", "recipient_id": "run-a"},
                ]
            },
        )
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        first = handle_telegram_webhook(
            store,
            _telegram_update(text="/assign task-1 host-a"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )
        second = handle_telegram_webhook(
            store,
            _telegram_update(text="/assign task-1 host-a"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )

        message_id = first["message"]["message_id"]
        assert second["message"]["message_id"] == message_id
        assert first["created"] is True
        assert second["created"] is False
        assert len(store.list_messages(room["room_id"])) == 1
        assert len(store.list_deliveries(message_id)) == 2
        assert len(store.list_command_refs()) == 1
        assert store.verify_command_ref(store.list_command_refs()[0]) is True
    finally:
        store.close()


def test_telegram_host_alias_mentions_are_routing_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        store.set_adapter_command_promotion("telegram", enabled=True)
        room = store.create_room(
            room_kind="task",
            display_name="Task",
            task_id="task-1",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"}
                ]
            },
        )
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
            route_policy={
                "command_promotion": {
                    "enabled": True,
                    "allowed_command_types": ["assign_task"],
                    "allowed_target_kinds": ["task"],
                }
            },
        )
        store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write", "command:write"),
            display_name="Operator",
        )

        chat = handle_telegram_webhook(
            store,
            _telegram_update(text="@rosie please check my email"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )
        command = handle_telegram_webhook(
            store,
            _telegram_update(
                update_id=101,
                message_id=201,
                text="/assign task-1 @lenny repair the inbox test",
            ),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )

        assert chat["message"]["body"] == "@rosie please check my email"
        assert chat["message"]["metadata"]["routing"]["host_alias"] == "rosie"
        assert command["message"]["body"] == (
            "/assign task-1 @lenny repair the inbox test"
        )
        assert command["message"]["target_scope"] == {
            "kind": "task",
            "task_id": "task-1",
        }
        assert command["command_ref"]["task_id"] == "task-1"
        assert command["message"]["metadata"]["routing"]["host_alias"] == "lenny"
        assert command["message"]["metadata"]["assignment"]["message"] == (
            "repair the inbox test"
        )
    finally:
        store.close()


def test_untagged_telegram_message_promotes_to_one_claimable_command_ref(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        room = store.create_room(
            room_kind="fleet",
            display_name="Fleet",
            metadata={
                "assignment": {
                    "repo_root": r"E:\Projects\repo-a",
                    "provider": "codex",
                }
            },
        )
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
        )

        first = handle_telegram_webhook(
            store,
            _telegram_update(text="please check my email"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )
        replay = handle_telegram_webhook(
            store,
            _telegram_update(text="please check my email"),
            secret_token=TELEGRAM_SECRET,
            provided_secret_token=TELEGRAM_SECRET,
        )
        message_id = first["message"]["message_id"]

        promoted = store.promote_message_to_assign_task_command_ref(message_id)
        promoted_again = store.promote_message_to_assign_task_command_ref(message_id)

        assert first["created"] is True
        assert replay["created"] is False
        assert first["command_ref"] is None
        assert first["message"]["message_type"] == "chat"
        assert first["message"]["metadata"]["claimable_work"]["status"] == "pending"
        assert promoted["created"] is True
        assert promoted_again["created"] is False
        assert promoted_again["command_ref"]["command_id"] == (
            promoted["command_ref"]["command_id"]
        )
        assert promoted["command_ref"]["command_type"] == "assign_task"
        assert promoted["command_ref"]["task_id"] == f"telegram-msg:{message_id}"
        assert len(store.list_messages(room["room_id"])) == 1
        assert len(store.list_command_refs()) == 1
        assert store.verify_command_ref(promoted["command_ref"]) is True
    finally:
        store.close()


def test_telegram_adapter_renders_outbound_notification_payload() -> None:
    adapter = TelegramAdapter()

    rendered = adapter.render_outbound(
        {
            "message": {
                "body": "Task completed.",
                "message_type": "alert",
                "metadata": {"severity": "completed"},
            },
            "delivery": {
                "recipient_kind": "adapter",
                "recipient_id": "telegram",
                "metadata": {"platform_room_id": "-100123"},
            },
        }
    )

    assert rendered == {
        "method": "sendMessage",
        "payload": {
            "chat_id": "-100123",
            "text": "[completed] Task completed.",
            "disable_web_page_preview": True,
        },
    }


def test_telegram_long_poll_reuses_ingest_path_and_persists_next_offset(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        AdapterRegistry(store).register_defaults()
        room = store.create_room(
            room_kind="fleet",
            display_name="Fleet",
            metadata={
                "assignment": {
                    "repo_root": r"E:\Projects\repo-a",
                    "provider": "codex",
                }
            },
        )
        store.map_platform_room(
            adapter_id="telegram",
            platform_room_id="-100123",
            room_id=room["room_id"],
        )
        store.link_external_identity(
            adapter_id="telegram",
            platform_user_id="42",
            openclaw_identity_id="operator-1",
            scopes=("message:write",),
            display_name="Operator",
        )
        transport = FakeTelegramHttpClient(
            [
                {
                    "ok": True,
                    "result": [
                        _telegram_update(
                            update_id=105,
                            message_id=205,
                            text="@lenny please check my email",
                        )
                    ],
                },
                {"ok": True, "result": []},
            ]
        )

        first = poll_telegram_updates(
            store,
            bot_token="test-bot-token",
            http_client=transport,
            offset=100,
            persist_update_offset=True,
        )
        second = poll_telegram_updates(
            store,
            bot_token="test-bot-token",
            http_client=transport,
            persist_update_offset=True,
        )

        assert transport.requests[0] == (
            "https://api.telegram.org/bottest-bot-token/getUpdates",
            {"offset": 100, "timeout": 0},
        )
        assert transport.requests[1] == (
            "https://api.telegram.org/bottest-bot-token/getUpdates",
            {"offset": 106, "timeout": 0},
        )
        assert first["results"][0]["message"]["body"] == "@lenny please check my email"
        assert first["results"][0]["message"]["metadata"]["routing"]["host_alias"] == (
            "lenny"
        )
        assert first["next_update_offset"] == 106
        assert first["cursor"]["cursor_value"] == "106"
        assert second["requested_offset"] == 106
        assert second["results"] == []
        assert store.get_adapter_cursor(
            adapter_id="telegram",
            cursor_key="telegram:getUpdates",
        )["cursor_value"] == "106"
        assert len(store.list_messages(room["room_id"])) == 1
    finally:
        store.close()
