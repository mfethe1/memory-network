from __future__ import annotations

from pathlib import Path

from code_index.openclaw_messaging.routes import MessagingRouter
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.notifications import notification_rules
from code_index.openclaw_messaging.notifications import should_notify


def test_one_human_message_has_multiple_acknowledgeable_deliveries(
    tmp_path: Path,
) -> None:
    store = MessagingStore(tmp_path / "messages.db", signing_secret="test-secret")
    try:
        room = store.create_room(
            room_kind="task",
            display_name="Task 123",
            task_id="task-123",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"},
                    {"recipient_kind": "run", "recipient_id": "run-a"},
                ]
            },
        )

        result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Please inspect the failure.",
            target_scope={"kind": "task", "task_id": "task-123"},
        )
        message = result["message"]
        deliveries = store.list_deliveries(message["message_id"])

        assert len(store.list_messages(room["room_id"])) == 1
        assert [(d["recipient_kind"], d["recipient_id"]) for d in deliveries] == [
            ("host", "host-a"),
            ("run", "run-a"),
        ]
        assert {delivery["delivery_status"] for delivery in deliveries} == {"queued"}

        acked = store.ack_delivery(
            message_id=message["message_id"],
            recipient_kind="run",
            recipient_id="run-a",
        )

        assert acked["delivery_status"] == "acked"
        delivery_statuses = {
            (delivery["recipient_kind"], delivery["recipient_id"]): delivery[
                "delivery_status"
            ]
            for delivery in store.list_deliveries(message["message_id"])
        }
        assert delivery_statuses == {
            ("host", "host-a"): "queued",
            ("run", "run-a"): "acked",
        }
    finally:
        store.close()


def test_routes_cover_rooms_messages_ack_stream_and_preview(tmp_path: Path) -> None:
    store = MessagingStore(tmp_path / "messages.db")
    router = MessagingRouter(store)
    try:
        room = store.create_room(
            room_kind="host",
            display_name="Host A",
            host_id="host-a",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"}
                ]
            },
        )

        created = router.handle(
            "POST",
            "/messages",
            {
                "room_id": room["room_id"],
                "sender_kind": "human",
                "sender_id": "operator-1",
                "body": "Status?",
                "target_scope": {"kind": "host", "host_id": "host-a"},
            },
        )

        assert created.status_code == 201
        message_id = created.body["message"]["message_id"]
        assert router.handle("GET", "/rooms").body["rooms"][0]["room_id"] == (
            room["room_id"]
        )
        assert router.handle(
            "GET",
            f"/rooms/{room['room_id']}/messages",
        ).body["messages"][0]["message_id"] == message_id
        assert router.handle(
            "POST",
            f"/messages/{message_id}/ack",
            {"recipient_kind": "host", "recipient_id": "host-a"},
        ).body["delivery"]["delivery_status"] == "acked"
        assert router.handle("GET", "/messages/stream").body["events"][0][
            "message"
        ]["message_id"] == message_id
        assert router.handle(
            "POST",
            "/messages/preview",
            {"target_scope": {"kind": "host", "host_id": "host-a"}},
        ).body["preview"]["recipients"] == [
            {"recipient_kind": "host", "recipient_id": "host-a"}
        ]
    finally:
        store.close()


def test_mutating_message_gets_signed_command_ref_before_host_delivery(
    tmp_path: Path,
) -> None:
    store = MessagingStore(tmp_path / "messages.db", signing_secret="test-secret")
    try:
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

        result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Assign this task to host-a.",
            message_type="command",
            command_type="assign_task",
            target_scope={"kind": "task", "task_id": "task-123"},
        )
        message_id = result["message"]["message_id"]
        command = store.get_command_ref_for_message(message_id)
        delivery = store.list_deliveries(message_id)[0]

        assert command is not None
        assert command["message_id"] == message_id
        assert command["status"] == "signed"
        assert store.verify_command_ref(command) is True
        assert delivery["recipient_kind"] == "host"
        assert delivery["metadata"]["command_id"] == command["command_id"]
    finally:
        store.close()


def test_notification_rules_cover_high_signal_events() -> None:
    rules = notification_rules()

    assert set(rules) == {
        "needs_attention",
        "blocked",
        "failed",
        "completed",
        "lease_conflict",
        "verification_blocked",
    }
    assert should_notify("failed") is True
    assert should_notify("heartbeat") is False
