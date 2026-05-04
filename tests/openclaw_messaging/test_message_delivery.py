from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_index.openclaw_messaging.routes import MessagingRouter
from code_index.openclaw_messaging.routes import Principal
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.notifications import notification_rules
from code_index.openclaw_messaging.notifications import should_notify


SIGNING_SECRET = "test-secret"


def _store(tmp_path: Path) -> MessagingStore:
    return MessagingStore(tmp_path / "messages.db", signing_secret=SIGNING_SECRET)


def test_one_human_message_has_multiple_acknowledgeable_deliveries(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
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
    store = _store(tmp_path)
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
    store = _store(tmp_path)
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
        assert command["status"] == "pending"
        assert store.verify_command_ref(command) is True
        assert delivery["recipient_kind"] == "host"
        assert delivery["metadata"]["command_id"] == command["command_id"]
    finally:
        store.close()


def test_command_ref_verification_rejects_expired_tampered_and_inconsistent_refs(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
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
        command = result["command_ref"]

        assert store.verify_command_ref(command) is True

        mismatched_outer = dict(command)
        mismatched_outer["command_id"] = "cmd_wrong"
        assert store.verify_command_ref(mismatched_outer) is False

        expired_result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Expired command.",
            message_type="command",
            command_type="assign_task",
            target_scope={"kind": "task", "task_id": "task-123"},
            expires_at="2000-01-01T00:00:00+00:00",
        )
        assert store.verify_command_ref(expired_result["command_ref"]) is False

        store.conn.execute(
            "UPDATE openclaw_command_refs SET status = 'cancelled' WHERE command_id = ?",
            (command["command_id"],),
        )
        store.conn.commit()
        cancelled = store.get_command_ref_for_message(command["message_id"])
        assert cancelled is not None
        assert store.verify_command_ref(cancelled) is False

        store.conn.execute(
            "UPDATE openclaw_command_refs SET status = 'pending' WHERE command_id = ?",
            (command["command_id"],),
        )
        store.conn.execute(
            "UPDATE openclaw_messages SET body = 'changed body' WHERE message_id = ?",
            (command["message_id"],),
        )
        store.conn.commit()
        body_changed = store.get_command_ref_for_message(command["message_id"])
        assert body_changed is not None
        assert store.verify_command_ref(body_changed) is False

        tampered_payload = dict(command)
        signed = json.loads(tampered_payload["signed_payload"])
        signed["payload"]["message_id"] = "msg_wrong"
        tampered_payload["signed_payload"] = json.dumps(signed)
        assert store.verify_command_ref(tampered_payload) is False
    finally:
        store.close()


def test_store_requires_explicit_signing_secret(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        MessagingStore(tmp_path / "messages.db")  # type: ignore[call-arg]


def test_route_rejects_body_principal_but_allows_trusted_context_principal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    router = MessagingRouter(store)
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

        rejected = router.handle(
            "POST",
            "/messages",
            {
                "room_id": room["room_id"],
                "sender_kind": "human",
                "sender_id": "operator-1",
                "body": "Assign this task to host-a.",
                "message_type": "command",
                "command_type": "assign_task",
                "target_scope": {"kind": "task", "task_id": "task-123"},
            },
        )
        assert rejected.status_code == 403
        assert store.list_messages(room["room_id"]) == []

        self_asserted = router.handle(
            "POST",
            "/messages",
            {
                "principal": {
                    "principal_id": "operator-1",
                    "scopes": ["message:write", "command:write"],
                },
                "room_id": room["room_id"],
                "sender_kind": "human",
                "sender_id": "operator-1",
                "body": "Assign this task to host-a.",
                "message_type": "command",
                "command_type": "assign_task",
                "target_scope": {"kind": "task", "task_id": "task-123"},
            },
        )
        assert self_asserted.status_code == 403
        assert store.list_messages(room["room_id"]) == []

        accepted = router.handle(
            "POST",
            "/messages",
            {
                "room_id": room["room_id"],
                "sender_kind": "human",
                "sender_id": "operator-1",
                "body": "Assign this task to host-a.",
                "message_type": "command",
                "command_type": "assign_task",
                "target_scope": {"kind": "task", "task_id": "task-123"},
            },
            principal=Principal(
                principal_id="operator-1",
                scopes=frozenset({"message:write", "command:write"}),
            ),
        )
        assert accepted.status_code == 201
        assert accepted.body["command_ref"] is not None
    finally:
        store.close()


def test_ack_delivery_is_monotonic_after_acked(tmp_path: Path) -> None:
    store = _store(tmp_path)
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
        result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Status?",
            target_scope={"kind": "host", "host_id": "host-a"},
        )
        message_id = result["message"]["message_id"]

        acked = store.ack_delivery(
            message_id=message_id,
            recipient_kind="host",
            recipient_id="host-a",
            status="acked",
        )
        late_delivered = store.ack_delivery(
            message_id=message_id,
            recipient_kind="host",
            recipient_id="host-a",
            status="delivered",
        )

        assert acked["delivery_status"] == "acked"
        assert late_delivered["delivery_status"] == "acked"
        assert late_delivered["acked_at"] == acked["acked_at"]
    finally:
        store.close()


def test_adapter_deliveries_keep_distinct_platform_targets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        room = store.create_room(
            room_kind="task",
            display_name="Task",
            task_id="task-1",
            metadata={
                "default_delivery_targets": [
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1001",
                    },
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1002",
                    },
                ]
            },
        )

        result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Notify both Telegram rooms.",
            target_scope={"kind": "task", "task_id": "task-1"},
        )

        deliveries = store.list_deliveries(result["message"]["message_id"])
        assert sorted(
            delivery["metadata"]["platform_room_id"] for delivery in deliveries
        ) == ["-1001", "-1002"]
        assert len({delivery["delivery_key"] for delivery in deliveries}) == 2
    finally:
        store.close()


def test_ack_duplicate_adapter_targets_requires_exact_delivery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        room = store.create_room(
            room_kind="task",
            display_name="Task",
            task_id="task-1",
            metadata={
                "default_delivery_targets": [
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1001",
                    },
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1002",
                    },
                ]
            },
        )
        result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Notify both Telegram rooms.",
            target_scope={"kind": "task", "task_id": "task-1"},
        )
        message_id = result["message"]["message_id"]
        deliveries = store.list_deliveries(message_id)
        first = next(
            delivery
            for delivery in deliveries
            if delivery["metadata"]["platform_room_id"] == "-1001"
        )
        second = next(
            delivery
            for delivery in deliveries
            if delivery["metadata"]["platform_room_id"] == "-1002"
        )

        with pytest.raises(ValueError, match="ambiguous delivery acknowledgement"):
            store.ack_delivery(
                message_id=message_id,
                recipient_kind="adapter",
                recipient_id="telegram",
            )

        acked_by_key = store.ack_delivery(
            message_id=message_id,
            delivery_key=second["delivery_key"],
        )
        acked_by_id = store.ack_delivery(
            message_id=message_id,
            delivery_id=first["delivery_id"],
        )
        statuses = {
            delivery["metadata"]["platform_room_id"]: delivery["delivery_status"]
            for delivery in store.list_deliveries(message_id)
        }

        assert acked_by_key["delivery_id"] == second["delivery_id"]
        assert acked_by_id["delivery_id"] == first["delivery_id"]
        assert statuses == {"-1001": "acked", "-1002": "acked"}
    finally:
        store.close()


def test_route_ack_duplicate_adapter_targets_requires_exact_delivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    router = MessagingRouter(store)
    try:
        room = store.create_room(
            room_kind="task",
            display_name="Task",
            task_id="task-1",
            metadata={
                "default_delivery_targets": [
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1001",
                    },
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1002",
                    },
                ]
            },
        )
        result = store.create_message(
            room_id=room["room_id"],
            sender_kind="human",
            sender_id="operator-1",
            body="Notify both Telegram rooms.",
            target_scope={"kind": "task", "task_id": "task-1"},
        )
        message_id = result["message"]["message_id"]
        target = store.list_deliveries(message_id)[0]

        ambiguous = router.handle(
            "POST",
            f"/messages/{message_id}/ack",
            {"recipient_kind": "adapter", "recipient_id": "telegram"},
        )
        exact = router.handle(
            "POST",
            f"/messages/{message_id}/ack",
            {"delivery_key": target["delivery_key"]},
        )

        assert ambiguous.status_code == 400
        assert exact.status_code == 200
        assert exact.body["delivery"]["delivery_id"] == target["delivery_id"]
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
