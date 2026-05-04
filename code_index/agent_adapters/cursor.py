"""Cursor SDK sidecar adapter helpers.

This module keeps the Python-facing contract small: command construction,
sidecar JSON parsing, and normalization into the local agent event schema.
The Node sidecar owns live Cursor SDK calls.
"""

from __future__ import annotations

import os
import re
import json
from collections.abc import Mapping
from typing import Any


PINNED_CURSOR_SDK_VERSION = "1.0.12"
CURSOR_SDK_PACKAGE = "@cursor/sdk"
DRY_RUN_ENV_VAR = "CODE_INDEX_CURSOR_DRY_RUN"
CURSOR_API_KEY_ENV_VAR = "CURSOR_API_KEY"
SIDECAR_BIN = "cursor-agent-sidecar"
SIDECAR_COMMANDS = frozenset(
    {"create", "run", "prompt", "stream", "wait", "cancel", "archive", "delete"}
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_ref(value: Any, *, default: str = "task") -> str:
    text = _text(value) or default
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


def _env_flag(value: str | None) -> bool | None:
    if value is None or not value.strip():
        return None
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _text(value)
        return [text] if text else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def should_use_dry_run(
    env: Mapping[str, str] | None = None,
    *,
    requested: str = "auto",
) -> bool:
    """Return whether the sidecar should avoid live Cursor calls."""

    mode = (requested or "auto").strip().lower()
    if mode in {"1", "true", "yes", "on", "dry-run", "dry_run"}:
        return True
    if mode in {"0", "false", "no", "off", "live"}:
        return False

    env_values = env if env is not None else os.environ
    override = _env_flag(env_values.get(DRY_RUN_ENV_VAR))
    if override is not None:
        return override
    return not bool(_text(env_values.get(CURSOR_API_KEY_ENV_VAR)))


def build_sidecar_command(
    command: str = "run",
    *,
    root: str,
    task_json: str | None = None,
    provider_prompt_file: str | None = None,
    mcp_config_file: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    runtime: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Build the argv contract used by the command adapter preset."""

    normalized = (command or "run").strip().lower()
    if normalized not in SIDECAR_COMMANDS:
        raise ValueError(f"unknown Cursor sidecar command: {command}")
    argv = [SIDECAR_BIN, normalized, "--root", root]
    optional_args = [
        ("--task-json", task_json),
        ("--provider-prompt-file", provider_prompt_file),
        ("--mcp-config-file", mcp_config_file),
        ("--agent-id", agent_id),
        ("--run-id", run_id),
        ("--model", model),
        ("--runtime", runtime),
    ]
    for flag, value in optional_args:
        if value:
            argv.extend([flag, value])
    if dry_run:
        argv.append("--dry-run")
    return argv


def _provider_run_refs(task: Mapping[str, Any]) -> dict[str, str]:
    cursor_ref = f"cursor-dry-run-{_safe_ref(task.get('run_id'))}"
    return {
        "cursor_agent_id": cursor_ref,
        "cursor_run_id": cursor_ref,
    }


def _event(
    task: Mapping[str, Any],
    *,
    event_type: str,
    message: str,
    status: str | None = None,
    file_path: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "run_id": task.get("run_id"),
        "agent_name": task.get("agent_name") or "Cursor",
        "event_type": event_type,
        "message": message,
        "payload": dict(payload or {}),
    }
    if status:
        out["status"] = status
    if file_path:
        out["file_path"] = file_path
    return out


def _provider_run_refs_from_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    agent_id = _text(
        payload.get("cursor_agent_id")
        or payload.get("agent_id")
        or payload.get("agentId")
    )
    run_id = _text(
        payload.get("cursor_run_id")
        or payload.get("run_id")
        or payload.get("runId")
    )
    refs: dict[str, str] = {}
    if agent_id:
        refs["cursor_agent_id"] = agent_id
    if run_id:
        refs["cursor_run_id"] = run_id
    return refs


def _local_event(
    *,
    local_run_id: str | None,
    event_type: str,
    message: str,
    provider_event: str,
    status: str | None = None,
    file_path: str | None = None,
    payload: Mapping[str, Any] | None = None,
    refs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    event_payload = {
        "adapter": "cursor",
        "provider_event": provider_event,
        "provider_run_refs": dict(refs or {}),
        **dict(payload or {}),
    }
    out: dict[str, Any] = {
        "run_id": local_run_id,
        "agent_name": "Cursor",
        "event_type": event_type,
        "message": message,
        "payload": event_payload,
    }
    if status:
        out["status"] = status
    if file_path:
        out["file_path"] = file_path
    return out


def parse_sidecar_json_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_args(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _tool_event_type(name: str, args: Mapping[str, Any]) -> str:
    tool = name.strip().lower()
    command = _text(args.get("command")).lower()
    if any(token in tool for token in ("edit", "write", "replace", "patch", "delete")):
        return "edit"
    if any(token in tool for token in ("read", "open", "view")):
        return "read"
    if any(token in command for token in ("pytest", "test", "npm test", "ruff", "mypy")):
        return "test"
    if any(token in tool for token in ("grep", "glob", "search", "list", "find", "ls")):
        return "navigate"
    return "tool"


def _tool_file_path(args: Mapping[str, Any]) -> str | None:
    for key in ("file_path", "path", "target_file", "target_path"):
        value = _text(args.get(key))
        if value:
            return value
    return None


def _normalize_tool_event(
    *,
    local_run_id: str | None,
    provider_event: str,
    refs: Mapping[str, str],
    name: str,
    args: Mapping[str, Any],
    status: str | None = None,
    result: Any = None,
    message: str | None = None,
) -> dict[str, Any]:
    event_type = _tool_event_type(name, args)
    status_text = f" {status}" if status else ""
    event_message = message or f"Cursor tool {name or 'tool'}{status_text}."
    payload = {
        "tool_name": name or "tool",
        "tool_args": dict(args),
    }
    if status:
        payload["tool_status"] = status
    if result is not None:
        payload["tool_result"] = result
    return _local_event(
        local_run_id=local_run_id,
        event_type=event_type,
        message=event_message,
        provider_event=provider_event,
        file_path=_tool_file_path(args),
        payload=payload,
        refs=refs,
    )


def _normalize_sidecar_event(
    payload: Mapping[str, Any],
    *,
    local_run_id: str | None,
) -> list[dict[str, Any]]:
    provider = _text(
        payload.get("provider") or payload.get("source") or payload.get("adapter")
    ).lower()
    if provider not in {"cursor", "cursor_sidecar", "cursor-sidecar"}:
        return []
    event_name = _text(payload.get("event") or payload.get("type")).lower()
    provider_event = f"cursor.{event_name or 'event'}"
    refs = _provider_run_refs_from_payload(payload)
    message = _text(payload.get("message") or payload.get("text"))
    status = _text(payload.get("status")).lower()

    if event_name in {"run.started", "started"}:
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="status",
                status=status or "working",
                message=message or "Cursor run started.",
                provider_event=provider_event,
                refs=refs,
            )
        ]
    if event_name in {"run.completed", "completed"}:
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="status",
                status="completed",
                message=message or "Cursor run completed.",
                provider_event=provider_event,
                refs=refs,
            )
        ]
    if event_name in {"run.failed", "failed", "error"}:
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="status",
                status="failed",
                message=message or "Cursor run failed.",
                provider_event=provider_event,
                refs=refs,
            )
        ]
    if event_name in {"run.cancelled", "run.canceled", "cancelled", "canceled"}:
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="status",
                status="cancelled",
                message=message or "Cursor run cancelled.",
                provider_event=provider_event,
                refs=refs,
            )
        ]
    if event_name in {"assistant", "assistant.message", "message"} and message:
        role = _text(payload.get("role")).lower()
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="decision" if role in {"", "assistant"} else "note",
                message=message,
                provider_event=provider_event,
                payload={"role": role} if role else None,
                refs=refs,
            )
        ]
    if event_name in {"tool.call", "tool_call"}:
        args = _tool_args(payload.get("arguments") or payload.get("args"))
        name = _text(payload.get("tool_name") or payload.get("name") or "tool")
        return [
            _normalize_tool_event(
                local_run_id=local_run_id,
                provider_event=provider_event,
                refs=refs,
                name=name,
                args=args,
                message=message or f"Cursor requested tool call: {name}",
            )
        ]
    if event_name in {"tool.result", "tool_result"}:
        name = _text(payload.get("tool_name") or payload.get("name") or "tool")
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="tool",
                message=message or f"Cursor tool returned: {name}",
                provider_event=provider_event,
                payload={
                    "tool_name": name,
                    "tool_result": payload.get("output", payload.get("result")),
                },
                refs=refs,
            )
        ]
    return []


_SDK_STATUS_MAP = {
    "CREATING": "working",
    "RUNNING": "working",
    "FINISHED": "completed",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
    "EXPIRED": "failed",
}


def _normalize_sdk_message(
    payload: Mapping[str, Any],
    *,
    local_run_id: str | None,
) -> list[dict[str, Any]]:
    message_type = _text(payload.get("type"))
    refs = _provider_run_refs_from_payload(payload)
    provider_event = f"cursor.sdk.{message_type or 'message'}"

    if message_type == "system":
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="status",
                status="working",
                message="Cursor SDK stream initialized.",
                provider_event=provider_event,
                payload={
                    "model": payload.get("model"),
                    "tools": payload.get("tools") if isinstance(payload.get("tools"), list) else [],
                },
                refs=refs,
            )
        ]
    if message_type == "status":
        sdk_status = _text(payload.get("status")).upper()
        local_status = _SDK_STATUS_MAP.get(sdk_status)
        event_name = {
            "completed": "run.completed",
            "failed": "run.failed",
            "cancelled": "run.cancelled",
        }.get(local_status or "", "run.status")
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="status",
                status=local_status or "working",
                message=_text(payload.get("message")) or f"Cursor status: {sdk_status or 'unknown'}.",
                provider_event=f"cursor.{event_name}",
                refs=refs,
            )
        ]
    if message_type == "assistant":
        message = payload.get("message") if isinstance(payload.get("message"), Mapping) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        events: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = _text(block.get("type"))
            if block_type == "text":
                text = _text(block.get("text"))
                if text:
                    events.append(
                        _local_event(
                            local_run_id=local_run_id,
                            event_type="decision",
                            message=text,
                            provider_event="cursor.assistant.message",
                            refs=refs,
                        )
                    )
            elif block_type == "tool_use":
                args = _tool_args(block.get("input"))
                name = _text(block.get("name") or "tool")
                events.append(
                    _normalize_tool_event(
                        local_run_id=local_run_id,
                        provider_event="cursor.tool.call",
                        refs=refs,
                        name=name,
                        args=args,
                        message=f"Cursor requested tool call: {name}",
                    )
                )
        return events
    if message_type == "tool_call":
        args = _tool_args(payload.get("args"))
        name = _text(payload.get("name") or "tool")
        return [
            _normalize_tool_event(
                local_run_id=local_run_id,
                provider_event="cursor.tool.call",
                refs=refs,
                name=name,
                args=args,
                status=_text(payload.get("status")).lower() or None,
                result=payload.get("result"),
            )
        ]
    if message_type == "thinking":
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="note",
                message=_text(payload.get("text")) or "Cursor is thinking.",
                provider_event=provider_event,
                refs=refs,
            )
        ]
    if message_type == "task":
        return [
            _local_event(
                local_run_id=local_run_id,
                event_type="note",
                message=_text(payload.get("text")) or "Cursor task update.",
                provider_event=provider_event,
                payload={"task_status": payload.get("status")},
                refs=refs,
            )
        ]
    return []


def normalize_cursor_record(
    record: str | Mapping[str, Any],
    *,
    local_run_id: str | None = None,
) -> list[dict[str, Any]]:
    payload = parse_sidecar_json_line(record) if isinstance(record, str) else dict(record)
    if not payload:
        return []
    return (
        _normalize_sidecar_event(payload, local_run_id=local_run_id)
        or _normalize_sdk_message(payload, local_run_id=local_run_id)
    )


_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}


def normalize_stream_records(
    records: list[str | Mapping[str, Any]],
    *,
    local_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize recorded Cursor JSON stream records into local adapter events."""

    events: list[dict[str, Any]] = []
    terminal_emitted = False
    for record in records:
        for event in normalize_cursor_record(record, local_run_id=local_run_id):
            status = _text(event.get("status")).lower()
            if status in _TERMINAL_STATUSES:
                if terminal_emitted:
                    continue
                terminal_emitted = True
            events.append(event)
    return events


def dry_run_events(
    task: Mapping[str, Any],
    *,
    reason: str = "cursor_runtime_unavailable",
) -> list[dict[str, Any]]:
    """Build deterministic local events for no-credentials/no-runtime runs."""

    refs = _provider_run_refs(task)
    payload = {
        "adapter": "cursor",
        "fallback": "dry-run",
        "reason": reason,
        "provider_run_refs": refs,
    }
    selected_paths = _string_list(task.get("selected_paths"))
    events = [
        _event(
            task,
            event_type="status",
            status="working",
            message="Cursor sidecar dry-run started.",
            payload=payload,
        )
    ]
    for path in selected_paths:
        events.append(
            _event(
                task,
                event_type="read",
                file_path=path,
                message=f"Cursor sidecar dry-run inspected {path}.",
                payload=payload,
            )
        )
    events.append(
        _event(
            task,
            event_type="decision",
            message="Cursor sidecar dry-run completed without contacting Cursor.",
            payload=payload,
        )
    )
    events.append(
        _event(
            task,
            event_type="status",
            status="completed",
            message="Cursor sidecar dry-run completed task.",
            payload=payload,
        )
    )
    return events


_EXACT_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def is_exact_sdk_pin(version: str | None) -> bool:
    return bool(version and _EXACT_SEMVER_RE.match(version.strip()))
