from __future__ import annotations

import json
from pathlib import Path

from code_index.agent_adapters import cursor


def test_cursor_adapter_dry_run_without_credentials(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    task = {
        "run_id": "local-run-1",
        "agent_name": "Cursor",
        "message": "Summarize the adapter.",
        "selected_paths": ["code_index/agent_adapters/cursor.py"],
    }

    assert cursor.should_use_dry_run({}, requested="auto") is True

    events = cursor.dry_run_events(
        task,
        reason="cursor_runtime_unavailable",
    )

    assert [event["event_type"] for event in events] == [
        "status",
        "read",
        "decision",
        "status",
    ]
    assert events[0]["status"] == "working"
    assert events[-1]["status"] == "completed"
    assert events[-1]["payload"]["fallback"] == "dry-run"
    assert events[-1]["payload"]["provider_run_refs"] == {
        "cursor_agent_id": "cursor-dry-run-local-run-1",
        "cursor_run_id": "cursor-dry-run-local-run-1",
    }


def test_cursor_adapter_builds_sidecar_run_command():
    command = cursor.build_sidecar_command(
        root="E:/Projects/hackathon/memory-claude-openclaw-m1",
        task_json=".code_index/agent-tasks/local-run-1.json",
        provider_prompt_file=".code_index/agent-runs/local-run-1/provider-prompt.txt",
        mcp_config_file=".code_index/agent-runs/local-run-1/mcp.json",
    )

    assert command == [
        "cursor-agent-sidecar",
        "run",
        "--root",
        "E:/Projects/hackathon/memory-claude-openclaw-m1",
        "--task-json",
        ".code_index/agent-tasks/local-run-1.json",
        "--provider-prompt-file",
        ".code_index/agent-runs/local-run-1/provider-prompt.txt",
        "--mcp-config-file",
        ".code_index/agent-runs/local-run-1/mcp.json",
    ]


def test_cursor_adapter_normalizes_recorded_stream_events():
    lines = [
        json.dumps(
            {
                "provider": "cursor",
                "event": "run.started",
                "cursor_agent_id": "agent-123",
                "cursor_run_id": "run-123",
                "message": "Cursor accepted prompt.",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "agent_id": "agent-123",
                "run_id": "run-123",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Reading the adapter."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {
                                "file_path": "code_index/agent_adapters/cursor.py"
                            },
                        },
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "agent_id": "agent-123",
                "run_id": "run-123",
                "call_id": "tool-2",
                "name": "Edit",
                "status": "completed",
                "args": {"file_path": "code_index/agent_adapters/cursor.py"},
                "result": "ok",
            }
        ),
        json.dumps(
            {
                "provider": "cursor",
                "event": "run.completed",
                "cursor_agent_id": "agent-123",
                "cursor_run_id": "run-123",
                "message": "Finished.",
            }
        ),
    ]

    events = list(cursor.normalize_stream_records(lines, local_run_id="local-run-2"))

    assert [event["event_type"] for event in events] == [
        "status",
        "decision",
        "read",
        "edit",
        "status",
    ]
    assert events[0]["status"] == "working"
    assert events[-1]["status"] == "completed"
    assert events[1]["message"] == "Reading the adapter."
    assert events[2]["file_path"] == "code_index/agent_adapters/cursor.py"
    assert events[3]["file_path"] == "code_index/agent_adapters/cursor.py"
    assert events[-1]["payload"]["provider_run_refs"] == {
        "cursor_agent_id": "agent-123",
        "cursor_run_id": "run-123",
    }


def test_cursor_adapter_emits_cancelled_terminal_status_once():
    lines = [
        json.dumps(
            {
                "provider": "cursor",
                "event": "run.cancelled",
                "cursor_agent_id": "agent-123",
                "cursor_run_id": "run-123",
                "message": "Cancelled by OpenClaw.",
            }
        ),
        json.dumps(
            {
                "type": "status",
                "agent_id": "agent-123",
                "run_id": "run-123",
                "status": "CANCELLED",
                "message": "SDK observed cancellation.",
            }
        ),
        json.dumps(
            {
                "provider": "cursor",
                "event": "run.completed",
                "cursor_agent_id": "agent-123",
                "cursor_run_id": "run-123",
                "message": "Late terminal event should be suppressed.",
            }
        ),
    ]

    events = list(cursor.normalize_stream_records(lines, local_run_id="local-run-3"))
    terminal_events = [
        event
        for event in events
        if event.get("status") in {"completed", "failed", "cancelled", "canceled"}
    ]

    assert [event["status"] for event in terminal_events] == ["cancelled"]
    assert terminal_events[0]["message"] == "Cancelled by OpenClaw."


def test_cursor_sidecar_sdk_version_is_pinned():
    package_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "cursor-agent-sidecar"
        / "package.json"
    )

    package = json.loads(package_path.read_text(encoding="utf-8"))
    sdk_version = package["dependencies"][cursor.CURSOR_SDK_PACKAGE]

    assert sdk_version == cursor.PINNED_CURSOR_SDK_VERSION
    assert cursor.is_exact_sdk_pin(sdk_version)
