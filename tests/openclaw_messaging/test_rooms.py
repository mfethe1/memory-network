from __future__ import annotations

from pathlib import Path

from code_index.openclaw_messaging.store import MessagingStore


def test_room_kinds_and_task_projection_include_swarm_participants(
    tmp_path: Path,
) -> None:
    store = MessagingStore(tmp_path / "messages.db")
    try:
        for kind in ("fleet", "repo", "run", "host", "swarm"):
            store.create_room(room_kind=kind, display_name=f"{kind} room")

        task_room = store.create_room(
            room_kind="task",
            display_name="Task 123",
            task_id="task-123",
            metadata={
                "swarm": {
                    "lead_run": {
                        "run_id": "run-lead",
                        "agent_name": "Kimi Swarm Lead",
                    },
                    "child_runs": [
                        {
                            "run_id": "run-impl",
                            "agent_name": "Kimi Implementer",
                            "role": "implementer",
                            "title": "Implementer",
                        },
                        {
                            "run_id": "run-review",
                            "agent_name": "Kimi Reviewer",
                            "role": "reviewer",
                            "title": "Reviewer",
                        },
                    ],
                }
            },
        )

        rooms = store.list_rooms()
        projection = store.get_room_projection(task_room["room_id"])

        assert {room["room_kind"] for room in rooms} == {
            "fleet",
            "repo",
            "task",
            "run",
            "host",
            "swarm",
        }
        assert projection["room"]["room_id"] == task_room["room_id"]
        assert projection["participants"] == [
            {
                "participant_kind": "run",
                "participant_id": "run-lead",
                "display_name": "Kimi Swarm Lead",
                "role": "swarm_lead",
                "title": "Swarm Lead",
            },
            {
                "participant_kind": "run",
                "participant_id": "run-impl",
                "display_name": "Kimi Implementer",
                "role": "implementer",
                "title": "Implementer",
            },
            {
                "participant_kind": "run",
                "participant_id": "run-review",
                "display_name": "Kimi Reviewer",
                "role": "reviewer",
                "title": "Reviewer",
            },
        ]
    finally:
        store.close()


def test_target_preview_expands_supported_target_kinds(tmp_path: Path) -> None:
    store = MessagingStore(tmp_path / "messages.db")
    try:
        store.create_room(
            room_kind="fleet",
            display_name="Fleet",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "host", "recipient_id": "host-a"},
                    {"recipient_kind": "host", "recipient_id": "host-b"},
                ]
            },
        )
        store.create_room(
            room_kind="task",
            display_name="Task 123",
            task_id="task-123",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "run", "recipient_id": "run-lead"},
                    {"recipient_kind": "run", "recipient_id": "run-child"},
                ],
                "notification_targets": [
                    {
                        "recipient_kind": "adapter",
                        "recipient_id": "telegram",
                        "platform_room_id": "-1001",
                    }
                ],
            },
        )
        store.create_room(
            room_kind="swarm",
            display_name="Swarm 1",
            task_id="task-123",
            metadata={
                "default_delivery_targets": [
                    {"recipient_kind": "run", "recipient_id": "run-lead"},
                    {"recipient_kind": "run", "recipient_id": "run-child"},
                ]
            },
        )

        assert store.preview_target({"kind": "host", "host_id": "host-z"})[
            "recipients"
        ] == [{"recipient_kind": "host", "recipient_id": "host-z"}]
        assert store.preview_target({"kind": "run", "run_id": "run-z"})[
            "recipients"
        ] == [{"recipient_kind": "run", "recipient_id": "run-z"}]
        assert store.preview_target({"kind": "fleet"})["recipients"] == [
            {"recipient_kind": "host", "recipient_id": "host-a"},
            {"recipient_kind": "host", "recipient_id": "host-b"},
        ]
        assert store.preview_target({"kind": "task", "task_id": "task-123"}) == {
            "target_scope": {"kind": "task", "task_id": "task-123"},
            "recipients": [
                {"recipient_kind": "run", "recipient_id": "run-lead"},
                {"recipient_kind": "run", "recipient_id": "run-child"},
                {
                    "recipient_kind": "adapter",
                    "recipient_id": "telegram",
                    "platform_room_id": "-1001",
                },
            ],
        }
        assert store.preview_target({"kind": "swarm", "task_id": "task-123"})[
            "recipients"
        ] == [
            {"recipient_kind": "run", "recipient_id": "run-lead"},
            {"recipient_kind": "run", "recipient_id": "run-child"},
        ]
    finally:
        store.close()
