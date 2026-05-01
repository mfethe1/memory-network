"""Webhook adapter for graph-submitted agent tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from code_index import agent_providers


AGENT_COMMAND_ENV_VAR = "CODE_INDEX_AGENT_COMMAND"
AGENT_PROVIDER_ENV_VAR = "CODE_INDEX_AGENT_PROVIDER"
KIMI_ISOLATE_SHARE_ENV_VAR = "CODE_INDEX_KIMI_ISOLATE_SHARE_DIR"
KIMI_SHARE_DIR_ENV_VAR = "KIMI_SHARE_DIR"
PROVIDER_COMMANDS = agent_providers.PROVIDER_COMMANDS
STRUCTURED_EVENT_TYPES = {
    "read",
    "edit",
    "test",
    "tool",
    "navigate",
    "note",
    "decision",
    "status",
}
FILE_EVENT_TYPES = {"read", "edit", "test", "navigate"}
STRUCTURED_PREFIX_RE = re.compile(
    r"^(READ|EDIT|TEST|TOOL|NAVIGATE|NOTE|DECISION|STATUS)(?:\s+(.+))?$",
    re.IGNORECASE,
)
JSON_PREFIX_RE = re.compile(r"^(?:CODE_INDEX_EVENT|code_index_event)\s*[: ]\s*(\{.*\})$")
LOGURU_HANDLER_ERROR_START_RE = re.compile(
    r"^--- Logging error in Loguru Handler #\d+ ---$"
)
LOGURU_HANDLER_ERROR_END = "--- End of logging error ---"
WINDOWS_SHELL_METACHARS = ("<", ">", "|", "&")
KIMI_SHARE_SEED_FILES = ("config.toml", "device_id", "mcp.json")
KIMI_SHARE_SEED_DIRS = ("credentials", "plugins", "bin")


def _read_task(raw: str | None) -> dict[str, Any]:
    if raw:
        path = Path(raw[1:] if raw.startswith("@") else raw)
        text = path.read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"task JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("task JSON must be an object")
    return payload


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("CODE_INDEX_GRAPH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - user supplied local callback.
        return json.loads(response.read().decode("utf-8"))


def _callback_from_task(task: dict[str, Any]) -> str | None:
    callback = task.get("callback")
    if not isinstance(callback, dict):
        return None
    value = callback.get("agent_events_url")
    return str(value) if value else None


def _event_payload(
    task: dict[str, Any],
    *,
    event_type: str,
    message: str,
    file_path: str | None = None,
    symbol_path: str | None = None,
    status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "run_id": task.get("run_id"),
        "agent_name": task.get("agent_name") or "Agent Adapter",
        "event_type": event_type,
        "message": message,
        "payload": payload or {},
    }
    if file_path:
        out["file_path"] = file_path
    if symbol_path:
        out["symbol_path"] = symbol_path
    if status:
        out["status"] = status
    return out


def _shell_quote(value: object) -> str:
    text = str(value)
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def _shell_join(values: list[str]) -> str:
    return " ".join(_shell_quote(value) for value in values)


def _strip_outer_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _popen_command(formatted: str) -> tuple[str | list[str], bool]:
    if os.name != "nt" or any(marker in formatted for marker in WINDOWS_SHELL_METACHARS):
        return formatted, True
    try:
        parts = shlex.split(formatted, posix=False)
    except ValueError:
        return formatted, True
    if not parts:
        return formatted, True
    return [_strip_outer_quotes(part) for part in parts], False


class _FormatValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _task_root(task: dict[str, Any], root_hint: str | None = None) -> Path:
    value = root_hint or task.get("root") or os.getcwd()
    return Path(str(value)).expanduser().resolve()


def _safe_run_id(task: dict[str, Any]) -> str:
    text = str(task.get("run_id") or "task")
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


def _materialize_task_json(
    task: dict[str, Any],
    *,
    root: Path,
    existing_path: str | None = None,
) -> tuple[Path, bool]:
    if existing_path:
        path = Path(existing_path[1:] if existing_path.startswith("@") else existing_path)
        if path.exists():
            return path.resolve(), False

    try:
        task_dir = root / ".code_index" / "agent-tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / f"{_safe_run_id(task)}.json"
        path.write_text(json.dumps(task, indent=2), encoding="utf-8")
        return path, False
    except OSError:
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, suffix=".json"
        )
        with tmp:
            json.dump(task, tmp, indent=2)
        return Path(tmp.name), True


def _task_run_dir(root: Path, task: dict[str, Any]) -> Path:
    path = root / ".code_index" / "agent-runs" / _safe_run_id(task)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_last_message_path(root: Path, task: dict[str, Any]) -> Path:
    return _task_run_dir(root, task) / "last-message.txt"


def _task_provider_prompt_path(root: Path, task: dict[str, Any]) -> Path:
    return _task_run_dir(root, task) / "provider-prompt.txt"


def _task_mcp_config_path(root: Path, task: dict[str, Any]) -> Path:
    return _task_run_dir(root, task) / "mcp.json"


def _build_mcp_config(root: Path) -> dict[str, Any]:
    return {
        "mcpServers": {
            "code-index": {
                "command": sys.executable,
                "args": ["-m", "code_index", "mcp-serve", "--root", str(root)],
            }
        }
    }


def _build_provider_prompt(
    task: dict[str, Any],
    *,
    root: Path,
    task_json_path: Path,
) -> str:
    selected_paths = _string_list(task.get("selected_paths"))
    message = str(task.get("message") or "")
    run_id = str(task.get("run_id") or "")
    graph_context = (
        task.get("graph_context") if isinstance(task.get("graph_context"), dict) else {}
    )
    selected_graph_paths = [
        str(node.get("path"))
        for node in graph_context.get("selected_nodes", [])
        if isinstance(node, dict) and node.get("path")
    ][:8]
    related_graph_paths = [
        str(node.get("path"))
        for node in graph_context.get("related_nodes", [])
        if isinstance(node, dict) and node.get("path")
    ][:12]
    collaboration = (
        task.get("collaboration")
        if isinstance(task.get("collaboration"), dict)
        else {}
    )
    mailbox = (
        collaboration.get("mailbox")
        if isinstance(collaboration.get("mailbox"), dict)
        else {}
    )
    active_peer_runs = (
        collaboration.get("active_peer_runs")
        if isinstance(collaboration.get("active_peer_runs"), list)
        else []
    )
    overlapping_events = (
        collaboration.get("overlapping_file_events")
        if isinstance(collaboration.get("overlapping_file_events"), list)
        else []
    )
    overlapping_claims = (
        collaboration.get("overlapping_file_claims")
        if isinstance(collaboration.get("overlapping_file_claims"), list)
        else []
    )
    primary_targets = selected_graph_paths or selected_paths
    primary_target = primary_targets[0] if primary_targets else "none"
    parent_run_id = str(task.get("parent_run_id") or "").strip()
    continuation = (
        f"\nContinuation: this follows run {parent_run_id}. Use run_context.recent_events "
        "from the task JSON to preserve session continuity."
        if parent_run_id
        else ""
    )
    graph_hint = (
        f"Primary selected file: {primary_target}\n"
        f"Selected graph files: {', '.join(primary_targets) or 'none'}\n"
        f"Related graph files: {', '.join(related_graph_paths) or 'none'}"
    )
    collaboration_hint = (
        "Collaboration feed: "
        f"{mailbox.get('global_events_jsonl') or 'not available'}\n"
        "This run event feed: "
        f"{mailbox.get('run_events_jsonl') or 'not available'}\n"
        f"Active peer runs: {len(active_peer_runs)}; "
        f"overlapping file events: {len(overlapping_events)}; "
        f"overlapping file claims: {len(overlapping_claims)}"
    )
    return (
        "You are running from the code_index repo graph UI.\n"
        f"{graph_hint}\n"
        f"{collaboration_hint}\n"
        f"Repository root: {root}\n"
        f"Run id: {run_id}\n"
        f"Task JSON: {task_json_path}\n\n"
        f"User request: {message}\n\n"
        "Important: when the user says 'this file' or does not name a path, "
        "they mean the primary selected file above. Do not ask for a file path "
        "when Primary selected file is not 'none'.\n"
        "Before editing, read the task JSON. Its graph_context and "
        "context_packet.graph_context identify selected_nodes, related_nodes, "
        "edges, care_level, recent_edits, and summaries. Treat high or critical "
        "care files conservatively and inspect connected files first.\n"
        "Also read task.collaboration before editing. It lists active peer "
        "runs, file claims, recent peer events, overlapping file events, and the shared "
        "JSONL feeds under .code_index/agent-runs. If another run overlaps "
        "your selected file, emit a decision check-in before changing it and "
        "avoid overwriting work in progress.\n"
        "Keep the graph UI current by emitting CODE_INDEX_EVENT JSON lines or "
        "posting to callback.agent_events_url with this run_id when you read, "
        "edit, test, navigate, make a decision, or finish.\n"
        "If a code-index MCP server is available, use it for repo-map, symbol, "
        "context, and impact lookups instead of broad manual scanning.\n"
        "If the request asks for a code change, make the edit in the workspace, "
        "run the most relevant verification you can, and end with changed files, "
        "tests run, and any risk or blocker."
        + continuation
    )


def _format_command(
    command: str,
    task: dict[str, Any],
    *,
    root: Path,
    task_json_path: Path,
    last_message_path: Path | None = None,
    provider_prompt_path: Path | None = None,
    mcp_config_path: Path | None = None,
) -> str:
    selected_paths = _string_list(task.get("selected_paths"))
    selected_nodes = _string_list(task.get("selected_nodes"))
    message = str(task.get("message") or "")
    run_id = str(task.get("run_id") or "")
    last_message = last_message_path or _task_last_message_path(root, task)
    prompt_path = provider_prompt_path or _task_provider_prompt_path(root, task)
    mcp_path = mcp_config_path or _task_mcp_config_path(root, task)
    provider_prompt = _build_provider_prompt(
        task,
        root=root,
        task_json_path=task_json_path,
    )
    values = _FormatValues(
        {
            "message": _shell_quote(message),
            "message_raw": message,
            "provider_prompt": _shell_quote(provider_prompt),
            "provider_prompt_raw": provider_prompt,
            "run_id": _shell_quote(run_id),
            "run_id_raw": run_id,
            "root": _shell_quote(root),
            "root_raw": str(root),
            "task_json": _shell_quote(task_json_path),
            "task_json_raw": str(task_json_path),
            "last_message": _shell_quote(last_message),
            "last_message_raw": str(last_message),
            "provider_prompt_file": _shell_quote(prompt_path),
            "provider_prompt_file_raw": str(prompt_path),
            "mcp_config_file": _shell_quote(mcp_path),
            "mcp_config_file_raw": str(mcp_path),
            "selected_paths": _shell_join(selected_paths),
            "selected_paths_raw": " ".join(selected_paths),
            "selected_nodes": _shell_join(selected_nodes),
            "selected_nodes_raw": " ".join(selected_nodes),
        }
    )
    return command.format_map(values)


def resolve_agent_command(
    command: str | None = None,
    provider: str | None = None,
) -> tuple[str | None, str]:
    command_value = command
    if command_value:
        provider_value = agent_providers.normalize_provider_id(provider)
        return (
            command_value,
            provider_value if provider_value in PROVIDER_COMMANDS else "custom",
        )
    provider_value = agent_providers.normalize_provider_id(provider)
    if provider_value and provider_value != "custom":
        preset = PROVIDER_COMMANDS.get(provider_value)
        if not preset:
            agent_providers.require_provider(provider_value)
            raise ValueError(f"agent provider has no command preset: {provider_value}")
        return preset, provider_value
    command_value = os.environ.get(AGENT_COMMAND_ENV_VAR)
    if command_value:
        return command_value, "custom"
    provider_value = agent_providers.normalize_provider_id(
        os.environ.get(AGENT_PROVIDER_ENV_VAR)
    )
    if provider_value == "custom":
        return None, "custom"
    preset = PROVIDER_COMMANDS.get(provider_value)
    if not preset:
        agent_providers.require_provider(provider_value)
        raise ValueError(f"agent provider has no command preset: {provider_value}")
    return preset, provider_value


def _post_status(
    callback: str,
    task: dict[str, Any],
    *,
    message: str,
    status: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return _post_json(
        callback,
        _event_payload(
            task,
            event_type="status",
            message=message,
            status=status,
            payload=payload,
        ),
    )


def _run_dry_run(
    task: dict[str, Any],
    *,
    callback: str,
    event_delay: float = 0.0,
    fail: bool = False,
) -> tuple[int, dict[str, Any]]:
    selected_paths = _string_list(task.get("selected_paths"))
    delay = max(0.0, float(event_delay or 0.0))
    status = "failed" if fail else "completed"
    events: list[dict[str, Any]] = []
    events.append(
        _post_status(
            callback,
            task,
            message="Dry-run adapter accepted task.",
            status="working",
            payload={"adapter": "dry-run"},
        )
    )
    if delay:
        time.sleep(delay)
    for path in selected_paths:
        events.append(
            _post_json(
                callback,
                _event_payload(
                    task,
                    event_type="read",
                    file_path=path,
                    message=f"Dry-run adapter inspected {path}.",
                    payload={"adapter": "dry-run"},
                ),
            )
        )
        if delay:
            time.sleep(delay)
    events.append(
        _post_json(
            callback,
            _event_payload(
                task,
                event_type="test",
                file_path=selected_paths[0] if selected_paths else None,
                message="Dry-run adapter completed placeholder verification.",
                payload={"adapter": "dry-run", "ok": not fail},
            ),
        )
    )
    if delay:
        time.sleep(delay)
    events.append(
        _post_status(
            callback,
            task,
            message=(
                "Dry-run adapter completed task."
                if not fail
                else "Dry-run adapter marked task failed."
            ),
            status=status,
            payload={"adapter": "dry-run"},
        )
    )
    return 0, {
        "ok": True,
        "status": status,
        "run_id": task.get("run_id"),
        "events_sent": len(events),
        "responses": events,
    }


def _read_stream(
    stream_name: str,
    pipe: Any,
    output: "queue.Queue[tuple[str, str]]",
) -> None:
    try:
        for line in iter(pipe.readline, ""):
            output.put((stream_name, line.rstrip("\r\n")))
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _json_output_event(line: str) -> dict[str, Any] | None:
    text = line.strip()
    match = JSON_PREFIX_RE.match(text)
    if match:
        text = match.group(1)
    elif not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event_type") or payload.get("type") or "").strip().lower()
    if event_type not in STRUCTURED_EVENT_TYPES:
        return _provider_jsonl_output_event(payload)
    event_payload = payload.get("payload") or {}
    if not isinstance(event_payload, dict):
        event_payload = {"value": event_payload}
    return {
        "event_type": event_type,
        "message": str(payload.get("message") or line),
        "file_path": payload.get("file_path") or payload.get("file"),
        "symbol_path": payload.get("symbol_path") or payload.get("symbol"),
        "status": payload.get("status"),
        "payload": event_payload,
    }


def _split_path_message(rest: str) -> tuple[str | None, str]:
    text = rest.strip()
    if not text:
        return None, ""
    if " - " in text:
        path, message = text.split(" - ", 1)
        return path.strip() or None, message.strip()
    first, sep, tail = text.partition(" ")
    return first.strip() or None, tail.strip() if sep else ""


def _prefixed_output_event(line: str) -> dict[str, Any] | None:
    match = STRUCTURED_PREFIX_RE.match(line.strip())
    if not match:
        return None
    event_type = match.group(1).lower()
    rest = (match.group(2) or "").strip()
    file_path = None
    status = None
    if event_type in FILE_EVENT_TYPES:
        file_path, message = _split_path_message(rest)
        message = message or f"{event_type} {file_path or ''}".strip()
    elif event_type == "status":
        status, _, message = rest.partition(" ")
        status = status.strip().lower() or None
        message = message.strip() or (f"status {status}" if status else "status")
    else:
        message = rest or event_type
    return {
        "event_type": event_type,
        "message": message,
        "file_path": file_path,
        "symbol_path": None,
        "status": status,
        "payload": {},
    }


def _text_from_item(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        if parts:
            return "\n".join(parts)
    return ""


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return str(value)


def _safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return payload if isinstance(payload, dict) else {"value": payload}
    return {}


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(tool_call.get("name") or tool_call.get("tool_name") or "tool")


def _tool_call_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function")
    if isinstance(function, dict) and "arguments" in function:
        return _safe_json_dict(function.get("arguments"))
    for key in ("arguments", "args", "input"):
        if key in tool_call:
            return _safe_json_dict(tool_call.get(key))
    return {}


def _tool_call_file_path(tool_calls: list[dict[str, Any]]) -> str | None:
    path_keys = ("file_path", "path", "filepath", "target_file", "filename")
    for tool_call in tool_calls:
        args = _tool_call_args(tool_call)
        for key in path_keys:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _tool_call_event_type(tool_calls: list[dict[str, Any]]) -> str:
    names = " ".join(_tool_call_name(call).lower() for call in tool_calls)
    commands = " ".join(
        str(_tool_call_args(call).get("command") or "").lower()
        for call in tool_calls
    )
    if any(token in names for token in ("edit", "write", "replace", "patch")):
        return "edit"
    if any(token in names for token in ("read", "open", "view")):
        return "read"
    if any(token in commands for token in ("pytest", "test", "npm test", "ruff", "mypy")):
        return "test"
    if any(token in names for token in ("grep", "glob", "search", "list", "find")):
        return "navigate"
    return "tool"


def _provider_jsonl_output_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    return _codex_jsonl_output_event(payload) or _kimi_jsonl_output_event(payload)


def _codex_jsonl_output_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(payload.get("type") or "").strip()
    if event_type == "thread.started":
        thread_id = str(payload.get("thread_id") or "").strip()
        return {
            "event_type": "status",
            "message": (
                f"Codex session started: {thread_id}"
                if thread_id
                else "Codex session started."
            ),
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {"provider_event": event_type, "thread_id": thread_id},
        }
    if event_type == "turn.completed":
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "event_type": "status",
            "message": "Codex turn completed.",
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {"provider_event": event_type, "usage": usage},
        }
    if event_type != "item.completed" or not isinstance(payload.get("item"), dict):
        return None

    item = payload["item"]
    item_type = str(item.get("type") or "").strip()
    text = _text_from_item(item).strip()
    if item_type == "agent_message" and text:
        return {
            "event_type": "decision",
            "message": text,
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {"provider_event": event_type, "item_type": item_type},
        }
    if item_type == "reasoning" and text:
        return {
            "event_type": "tool",
            "message": f"[codex thinking] {text}",
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {"provider_event": event_type, "item_type": item_type},
        }
    if item_type == "command_execution":
        command = item.get("command")
        command_text = (
            " ".join(str(part) for part in command)
            if isinstance(command, list)
            else str(command or "").strip()
        )
        output = _text_from_item(item).strip()
        message = f"$ {command_text}" if command_text else "Codex ran a command."
        if output:
            message = f"{message}\n{output}"
        return {
            "event_type": "tool",
            "message": message,
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {
                "provider_event": event_type,
                "item_type": item_type,
                "command": command_text,
                "exit_code": item.get("exit_code"),
            },
        }
    if text:
        return {
            "event_type": "tool",
            "message": text,
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {"provider_event": event_type, "item_type": item_type},
        }
    return None


def _kimi_jsonl_output_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    role = str(payload.get("role") or "").strip().lower()
    if not role:
        return None
    tool_calls = payload.get("tool_calls")
    tool_call_items = [
        item for item in tool_calls if isinstance(item, dict)
    ] if isinstance(tool_calls, list) else []
    content = _content_text(payload.get("content")).strip()
    provider_payload: dict[str, Any] = {
        "provider_event": "kimi.message",
        "role": role,
    }
    if tool_call_items:
        provider_payload["tool_calls"] = [
            {
                "id": str(item.get("id") or ""),
                "name": _tool_call_name(item),
                "arguments": _tool_call_args(item),
            }
            for item in tool_call_items
        ]

    if role == "assistant":
        if tool_call_items:
            names = ", ".join(_tool_call_name(item) for item in tool_call_items)
            detail = f"Kimi requested tool call: {names}" if names else "Kimi requested a tool call."
            if content:
                detail = f"{content}\n{detail}"
            return {
                "event_type": _tool_call_event_type(tool_call_items),
                "message": detail,
                "file_path": _tool_call_file_path(tool_call_items),
                "symbol_path": None,
                "status": None,
                "payload": provider_payload,
            }
        if content:
            return {
                "event_type": "decision",
                "message": content,
                "file_path": None,
                "symbol_path": None,
                "status": None,
                "payload": provider_payload,
            }
    if role == "tool":
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        if tool_call_id:
            provider_payload["tool_call_id"] = tool_call_id
        return {
            "event_type": "tool",
            "message": content or "Kimi tool returned.",
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": provider_payload,
        }
    if role == "user" and content:
        return {
            "event_type": "note",
            "message": content,
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": provider_payload,
        }
    return None


def _parse_output_event(
    line: str,
    *,
    stream_name: str,
    command_label: str,
) -> dict[str, Any]:
    parsed = _json_output_event(line) or _prefixed_output_event(line)
    if parsed is None:
        parsed = {
            "event_type": "tool",
            "message": line,
            "file_path": None,
            "symbol_path": None,
            "status": None,
            "payload": {},
        }
    event_payload = dict(parsed.get("payload") or {})
    event_payload.update(
        {
            "adapter": "command",
            "stream": stream_name,
            "command": command_label,
            "structured": parsed["event_type"] != "tool" or bool(parsed.get("status")),
            "raw_line": line,
        }
    )
    return {
        "event_type": parsed["event_type"],
        "message": parsed["message"],
        "file_path": parsed.get("file_path"),
        "symbol_path": parsed.get("symbol_path"),
        "status": parsed.get("status"),
        "payload": event_payload,
    }


def _compact_loguru_handler_event(
    lines: list[str],
    *,
    stream_name: str,
    command_label: str,
) -> dict[str, Any]:
    text = "\n".join(lines)
    if "PermissionError: [WinError 32]" in text and "kimi.log" in text:
        reason = "windows_kimi_log_rotation_lock"
        message = (
            "Suppressed Loguru handler noise: Windows file locking blocked "
            "Kimi log rotation."
        )
    else:
        reason = "loguru_handler_error"
        message = "Suppressed Loguru handler noise from provider stderr."
    return {
        "event_type": "tool",
        "message": message,
        "file_path": None,
        "symbol_path": None,
        "status": None,
        "payload": {
            "adapter": "command",
            "stream": stream_name,
            "command": command_label,
            "structured": False,
            "provider_noise": "loguru_handler_error",
            "noise_reason": reason,
            "suppressed_lines": len(lines),
        },
    }


def _consume_loguru_handler_line(
    state: dict[str, Any],
    *,
    line: str,
    stream_name: str,
    command_label: str,
) -> tuple[bool, dict[str, Any] | None]:
    pending = state.get("_pending_loguru_handler_error")
    if isinstance(pending, dict):
        lines = pending.setdefault("lines", [])
        if isinstance(lines, list):
            lines.append(line)
        else:
            lines = [line]
            pending["lines"] = lines
        if line.strip() == LOGURU_HANDLER_ERROR_END or len(lines) >= 200:
            state.pop("_pending_loguru_handler_error", None)
            return True, _compact_loguru_handler_event(
                [str(item) for item in lines],
                stream_name=str(pending.get("stream_name") or stream_name),
                command_label=command_label,
            )
        return True, None

    if LOGURU_HANDLER_ERROR_START_RE.match(line.strip()):
        state["_pending_loguru_handler_error"] = {
            "stream_name": stream_name,
            "lines": [line],
        }
        return True, None
    return False, None


def _drain_output_events(
    output: "queue.Queue[tuple[str, str]]",
    *,
    callback: str,
    task: dict[str, Any],
    command_label: str,
    max_events: int,
    events_sent: int,
    state: dict[str, Any] | None = None,
) -> tuple[int, int]:
    omitted = 0
    while True:
        try:
            stream_name, line = output.get_nowait()
        except queue.Empty:
            break
        if not line:
            continue
        if state is not None:
            consumed, compact_event = _consume_loguru_handler_line(
                state,
                line=line,
                stream_name=stream_name,
                command_label=command_label,
            )
            if consumed:
                if compact_event is not None:
                    if events_sent >= max_events:
                        omitted += int(
                            compact_event["payload"].get("suppressed_lines") or 1
                        )
                    else:
                        _post_json(
                            callback,
                            _event_payload(
                                task,
                                event_type=compact_event["event_type"],
                                message=compact_event["message"],
                                file_path=compact_event.get("file_path"),
                                symbol_path=compact_event.get("symbol_path"),
                                status=compact_event.get("status"),
                                payload=compact_event["payload"],
                            ),
                        )
                        events_sent += 1
                continue
        if events_sent >= max_events:
            omitted += 1
            continue
        parsed = _parse_output_event(
            line,
            stream_name=stream_name,
            command_label=command_label,
        )
        if state is not None and parsed["event_type"] == "decision":
            state["last_decision_message"] = str(parsed["message"] or "").strip()
        _post_json(
            callback,
            _event_payload(
                task,
                event_type=parsed["event_type"],
                message=parsed["message"],
                file_path=parsed.get("file_path"),
                symbol_path=parsed.get("symbol_path"),
                status=parsed.get("status"),
                payload=parsed["payload"],
            ),
        )
        events_sent += 1
    return events_sent, omitted


def _terminate_process_tree(process: subprocess.Popen[str], *, force: bool) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except OSError:
        pass


def _wait_after_interrupt(process: subprocess.Popen[str], *, force: bool) -> int | None:
    try:
        return process.wait(timeout=5 if force else 2)
    except subprocess.TimeoutExpired:
        if not force:
            _terminate_process_tree(process, force=True)
            return _wait_after_interrupt(process, force=True)
    except OSError:
        return process.poll()
    return process.poll()


def _normal_task_path(path: Any) -> str | None:
    if not path:
        return None
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text or None


def _task_candidate_paths(task: dict[str, Any], root: Path, *, limit: int = 80) -> list[Path]:
    paths: list[str] = []

    def add(value: Any) -> None:
        path = _normal_task_path(value)
        if path and path not in paths:
            paths.append(path)

    for path in _string_list(task.get("selected_paths")):
        add(path)
    node = task.get("node") if isinstance(task.get("node"), dict) else {}
    add(node.get("path"))
    graph_context = (
        task.get("graph_context") if isinstance(task.get("graph_context"), dict) else {}
    )
    for key in ("selected_nodes", "related_nodes"):
        for item in graph_context.get(key, []):
            if isinstance(item, dict):
                add(item.get("path"))
    context_packet = (
        task.get("context_packet") if isinstance(task.get("context_packet"), dict) else {}
    )
    for item in context_packet.get("selected_paths", []):
        if isinstance(item, dict):
            add(item.get("path"))
        else:
            add(item)

    out: list[Path] = []
    for rel_path in paths[:limit]:
        full_path = (root / rel_path).resolve()
        try:
            full_path.relative_to(root)
        except ValueError:
            continue
        out.append(full_path)
    return out


def _file_fingerprint(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    digest = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha1": digest.hexdigest(),
    }


def _file_fingerprints(paths: list[Path]) -> dict[Path, dict[str, Any] | None]:
    return {path: _file_fingerprint(path) for path in paths}


def _changed_paths(
    before: dict[Path, dict[str, Any] | None],
    after: dict[Path, dict[str, Any] | None],
    *,
    root: Path,
) -> list[str]:
    changed: list[str] = []
    for path, before_value in before.items():
        if before_value == after.get(path):
            continue
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            continue
        changed.append(rel_path)
    return changed


def _read_text_if_present(path: Path, *, max_chars: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _env_flag_enabled(
    env: dict[str, str],
    name: str,
    *,
    default: bool = True,
) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _copy_kimi_share_seed(source: Path, target: Path) -> None:
    if not source.exists():
        return
    for filename in KIMI_SHARE_SEED_FILES:
        source_path = source / filename
        if source_path.is_file():
            try:
                shutil.copy2(source_path, target / filename)
            except OSError:
                pass
    for dirname in KIMI_SHARE_SEED_DIRS:
        source_path = source / dirname
        if source_path.is_dir():
            try:
                shutil.copytree(source_path, target / dirname)
            except OSError:
                pass


def _prepare_isolated_kimi_share_dir(
    env: dict[str, str],
    *,
    task: dict[str, Any] | None,
) -> Path:
    run_id = _safe_run_id(task or {})
    target = Path(tempfile.mkdtemp(prefix=f"code-index-kimi-{run_id}-"))
    source = Path(env.get(KIMI_SHARE_DIR_ENV_VAR) or (Path.home() / ".kimi")).expanduser()
    _copy_kimi_share_seed(source, target)
    env[KIMI_SHARE_DIR_ENV_VAR] = str(target)
    return target


def _cleanup_agent_command_env(path: Path | None) -> None:
    if path is None:
        return
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _agent_command_env(
    *,
    provider: str = "custom",
    task: dict[str, Any] | None = None,
) -> tuple[dict[str, str], Path | None]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    cleanup_dir = None
    if (
        agent_providers.normalize_provider_id(provider) == "kimi"
        and _env_flag_enabled(env, KIMI_ISOLATE_SHARE_ENV_VAR, default=True)
    ):
        cleanup_dir = _prepare_isolated_kimi_share_dir(env, task=task)
    return env, cleanup_dir


def _run_command(
    task: dict[str, Any],
    *,
    callback: str,
    command: str,
    root: Path,
    task_json_path: Path,
    provider: str = "custom",
    command_timeout: float | None = None,
    max_output_events: int = 400,
    cancel_event: threading.Event | None = None,
    process_started_callback: Callable[[int, str], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    selected_paths = _string_list(task.get("selected_paths"))
    last_message_path = _task_last_message_path(root, task)
    provider_prompt_path = _task_provider_prompt_path(root, task)
    mcp_config_path = _task_mcp_config_path(root, task)
    try:
        last_message_path.unlink()
    except OSError:
        pass
    provider_prompt = _build_provider_prompt(
        task,
        root=root,
        task_json_path=task_json_path,
    )
    provider_prompt_path.write_text(provider_prompt, encoding="utf-8")
    mcp_config_path.write_text(
        json.dumps(_build_mcp_config(root), indent=2) + "\n",
        encoding="utf-8",
    )
    formatted = _format_command(
        command,
        task,
        root=root,
        task_json_path=task_json_path,
        last_message_path=last_message_path,
        provider_prompt_path=provider_prompt_path,
        mcp_config_path=mcp_config_path,
    )
    command_label = command
    candidate_paths = _task_candidate_paths(task, root)
    before_fingerprints = _file_fingerprints(candidate_paths)
    responses: list[dict[str, Any]] = []
    events_sent = 0
    omitted_output_events = 0
    stream_state: dict[str, Any] = {}
    responses.append(
        _post_status(
            callback,
            task,
            message="Command adapter launching process.",
            status="working",
            payload={
                "adapter": "command",
                "command": command_label,
                "cwd": str(root),
                "task_json": str(task_json_path),
                "last_message": str(last_message_path),
                "provider_prompt": str(provider_prompt_path),
                "mcp_config": str(mcp_config_path),
            },
        )
    )
    events_sent += 1
    for path in selected_paths:
        responses.append(
            _post_json(
                callback,
                _event_payload(
                    task,
                    event_type="read",
                    file_path=path,
                    message=f"Command adapter selected {path}.",
                    payload={"adapter": "command"},
                ),
            )
        )
        events_sent += 1

    if cancel_event is not None and cancel_event.is_set():
        responses.append(
            _post_status(
                callback,
                task,
                message="Command adapter cancelled before launch.",
                status="cancelled",
                payload={"adapter": "command", "cancelled_before_launch": True},
            )
        )
        return 1, {
            "ok": False,
            "status": "cancelled",
            "run_id": task.get("run_id"),
            "events_sent": events_sent + 1,
            "responses": responses,
            "cancelled": True,
        }

    command_env, env_cleanup_dir = _agent_command_env(provider=provider, task=task)
    try:
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        popen_command, use_shell = _popen_command(formatted)
        process = subprocess.Popen(  # noqa: S603,S602 - configured local adapter command.
            popen_command,
            cwd=str(root),
            shell=use_shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=command_env,
            **popen_kwargs,
        )
    except OSError as exc:
        _cleanup_agent_command_env(env_cleanup_dir)
        responses.append(
            _post_status(
                callback,
                task,
                message=f"Command adapter failed to launch: {exc}",
                status="failed",
                payload={"adapter": "command", "error": str(exc)},
            )
        )
        return 1, {
            "ok": False,
            "status": "failed",
            "run_id": task.get("run_id"),
            "events_sent": events_sent + 1,
            "responses": responses,
            "error": str(exc),
        }

    if process_started_callback is not None:
        try:
            process_started_callback(process.pid, command_label)
        except Exception as exc:
            _terminate_process_tree(process, force=False)
            return_code = _wait_after_interrupt(process, force=False)
            error_type = type(exc).__name__
            error = f"{error_type}: {exc}"
            message = f"Command adapter process registry callback failed: {error}"
            try:
                responses.append(
                    _post_status(
                        callback,
                        task,
                        message=message,
                        status="failed",
                        payload={
                            "adapter": "command",
                            "process_callback": "failed",
                            "pid": process.pid,
                            "error_type": error_type,
                            "error": str(exc),
                        },
                    )
                )
                events_sent += 1
            except OSError:
                pass
            _cleanup_agent_command_env(env_cleanup_dir)
            return 1, {
                "ok": False,
                "status": "failed",
                "run_id": task.get("run_id"),
                "events_sent": events_sent,
                "responses": responses,
                "error": message,
                "process_exit_code": return_code,
            }

    _post_json(
        callback,
        _event_payload(
            task,
            event_type="tool",
            message=f"Command adapter started process {process.pid}.",
            payload={
                "adapter": "command",
                "pid": process.pid,
                "command": command_label,
            },
        ),
    )
    events_sent += 1
    output: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(
            target=_read_stream,
            args=("stdout", process.stdout, output),
            daemon=True,
        ),
        threading.Thread(
            target=_read_stream,
            args=("stderr", process.stderr, output),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    timeout_seconds = (
        float(command_timeout)
        if command_timeout is not None and float(command_timeout) > 0
        else None
    )
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
    timed_out = False
    cancelled = False
    try:
        while True:
            events_sent, omitted = _drain_output_events(
                output,
                callback=callback,
                task=task,
                command_label=command_label,
                max_events=max(0, int(max_output_events)),
                events_sent=events_sent,
                state=stream_state,
            )
            omitted_output_events += omitted
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate_process_tree(process, force=False)
                break
            if process.poll() is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(process, force=True)
                break
            time.sleep(0.1)
        return_code = (
            _wait_after_interrupt(process, force=timed_out)
            if cancelled or timed_out
            else process.wait(timeout=5)
        )
    finally:
        for thread in threads:
            thread.join(timeout=1)
        events_sent, omitted = _drain_output_events(
            output,
            callback=callback,
            task=task,
            command_label=command_label,
            max_events=max(0, int(max_output_events)),
            events_sent=events_sent,
            state=stream_state,
        )
        omitted_output_events += omitted

    if omitted_output_events:
        _post_json(
            callback,
            _event_payload(
                task,
                event_type="tool",
                message=f"Command adapter suppressed {omitted_output_events} output events.",
                payload={
                    "adapter": "command",
                    "omitted_output_events": omitted_output_events,
                },
            ),
        )
        events_sent += 1

    after_fingerprints = _file_fingerprints(candidate_paths)
    changed_files = _changed_paths(
        before_fingerprints,
        after_fingerprints,
        root=root,
    )
    for path in changed_files:
        _post_json(
            callback,
            _event_payload(
                task,
                event_type="edit",
                file_path=path,
                message=f"Command adapter detected edits to {path}.",
                payload={
                    "adapter": "command",
                    "detected_by": "file_fingerprint",
                },
            ),
        )
        events_sent += 1

    last_decision = str(stream_state.get("last_decision_message") or "").strip()
    if last_decision and not _read_text_if_present(last_message_path):
        try:
            last_message_path.write_text(last_decision + "\n", encoding="utf-8")
        except OSError:
            pass
    final_message = _read_text_if_present(last_message_path)
    if final_message and final_message != stream_state.get("last_decision_message"):
        _post_json(
            callback,
            _event_payload(
                task,
                event_type="decision",
                message=final_message,
                payload={
                    "adapter": "command",
                    "final": True,
                    "output_last_message": str(last_message_path),
                },
            ),
        )
        events_sent += 1

    if cancelled:
        status = "cancelled"
        message = "Command adapter cancelled task and interrupted the process."
        exit_code = 1
    elif timed_out:
        status = "failed"
        message = f"Command adapter timed out after {timeout_seconds:g} seconds."
        exit_code = 1
    elif return_code == 0:
        status = "completed"
        message = "Command adapter completed task."
        exit_code = 0
    else:
        status = "failed"
        message = f"Command adapter failed with exit code {return_code}."
        exit_code = 1

    responses.append(
        _post_status(
            callback,
            task,
            message=message,
            status=status,
            payload={
                "adapter": "command",
                "command": command_label,
                "exit_code": return_code,
                "cancelled": cancelled,
                "timed_out": timed_out,
                "omitted_output_events": omitted_output_events,
                "changed_files": changed_files,
                "final_message_path": str(last_message_path),
            },
        )
    )
    events_sent += 1
    _cleanup_agent_command_env(env_cleanup_dir)
    return exit_code, {
        "ok": status == "completed",
        "status": status,
        "run_id": task.get("run_id"),
        "events_sent": events_sent,
        "responses": responses,
        "command": command_label,
        "process_exit_code": return_code,
        "cancelled": cancelled,
        "timed_out": timed_out,
        "omitted_output_events": omitted_output_events,
        "changed_files": changed_files,
        "final_message_path": str(last_message_path),
    }


def run_task(
    task: dict[str, Any],
    *,
    callback_url: str | None = None,
    mode: str = "auto",
    command: str | None = None,
    provider: str | None = None,
    root_hint: str | None = None,
    cwd: str | None = None,
    task_json_path: str | None = None,
    event_delay: float = 0.0,
    fail: bool = False,
    command_timeout: float | None = None,
    max_output_events: int = 400,
    cancel_event: threading.Event | None = None,
    process_started_callback: Callable[[int, str], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    callback = callback_url or _callback_from_task(task)
    if not callback:
        return 2, {"ok": False, "error": "callback agent_events_url is required"}
    if not task.get("run_id"):
        return 2, {"ok": False, "error": "task run_id is required"}

    try:
        command_value, command_provider = resolve_agent_command(command, provider)
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc)}
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = "command" if command_value else "dry-run"
    if resolved_mode not in {"dry-run", "command"}:
        return 2, {"ok": False, "error": f"unknown adapter mode: {mode}"}
    if resolved_mode == "dry-run":
        return _run_dry_run(
            task,
            callback=callback,
            event_delay=event_delay,
            fail=fail,
        )
    if not command_value:
        return 2, {"ok": False, "error": "command mode requires --command, --provider, CODE_INDEX_AGENT_COMMAND, or CODE_INDEX_AGENT_PROVIDER"}

    root = _task_root(task, cwd or root_hint)
    materialized_path, cleanup = _materialize_task_json(
        task,
        root=root,
        existing_path=task_json_path,
    )
    try:
        exit_code, result = _run_command(
            task,
            callback=callback,
            command=command_value,
            root=root,
            task_json_path=materialized_path,
            provider=command_provider,
            command_timeout=command_timeout,
            max_output_events=max_output_events,
            cancel_event=cancel_event,
            process_started_callback=process_started_callback,
        )
        if result.get("command"):
            result["provider"] = command_provider
        return exit_code, result
    finally:
        if cleanup:
            try:
                materialized_path.unlink()
            except OSError:
                pass


def _print_result(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(
        f"agent-adapter: {payload['status']} "
        f"run={payload.get('run_id') or 'unknown'} "
        f"events={payload['events_sent']}"
    )


def run(args: argparse.Namespace) -> int:
    try:
        task = _read_task(args.task_json)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        exit_code, result = run_task(
            task,
            callback_url=args.callback_url,
            mode=args.mode,
            command=args.command,
            provider=args.provider,
            root_hint=args.root,
            cwd=args.cwd,
            task_json_path=args.task_json,
            event_delay=args.event_delay,
            fail=args.fail,
            command_timeout=args.command_timeout,
            max_output_events=args.max_output_events,
        )
    except OSError as exc:
        print(f"error: callback post failed: {exc}", file=sys.stderr)
        return 1

    if exit_code == 2:
        print(f"error: {result.get('error')}", file=sys.stderr)
        return 2
    _print_result(args, result)
    return exit_code
