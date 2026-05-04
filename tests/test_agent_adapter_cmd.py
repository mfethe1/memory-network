"""Focused coverage for agent-adapter command resolution."""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

from code_index import agent_providers
from code_index.commands import agent_adapter_cmd
from code_index.commands.agent_adapter_cmd import (
    _build_provider_prompt,
    _format_command,
    _parse_output_event,
    resolve_agent_command,
)


def test_resolve_agent_command_prefers_explicit_command(monkeypatch):
    monkeypatch.setenv("CODE_INDEX_AGENT_PROVIDER", "claude")
    command, provider = resolve_agent_command("custom-agent {message}")
    assert command == "custom-agent {message}"
    assert provider == "custom"


def test_resolve_agent_command_uses_provider_presets(monkeypatch):
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    assert resolve_agent_command(provider="claude") == (
        "claude -p --output-format stream-json "
        "--mcp-config {mcp_config_file} < {provider_prompt_file}",
        "claude",
    )
    assert resolve_agent_command(provider="codex") == (
        "codex exec -C {root} -s workspace-write --json "
        "-o {last_message} - < {provider_prompt_file}",
        "codex",
    )
    assert resolve_agent_command(provider="kimi") == (
        "kimi --work-dir {root} --mcp-config-file {mcp_config_file} "
        "--print --output-format stream-json --thinking "
        "--max-ralph-iterations -1 --max-steps-per-turn 200 "
        "< {provider_prompt_file}",
        "kimi",
    )
    assert resolve_agent_command(provider="opencode") == (
        "opencode run --dir {root} --format json "
        "--file {task_json} {provider_prompt}",
        "opencode",
    )
    assert resolve_agent_command(provider="cursor") == (
        "cursor-agent-sidecar run --root {root} --task-json {task_json} "
        "--provider-prompt-file {provider_prompt_file} "
        "--mcp-config-file {mcp_config_file}",
        "cursor",
    )
    assert resolve_agent_command(provider="goose") == (
        "goose run --instructions {provider_prompt_file} --no-session",
        "goose",
    )
    assert resolve_agent_command(provider="aider") == (
        "aider --yes-always --message-file {provider_prompt_file} "
        "{selected_paths}",
        "aider",
    )
    assert resolve_agent_command(provider="openhands") == (
        "openhands --headless --json -f {provider_prompt_file}",
        "openhands",
    )


def test_resolve_agent_command_explicit_provider_overrides_env_command(monkeypatch):
    monkeypatch.setenv("CODE_INDEX_AGENT_COMMAND", "custom-agent {task_json}")
    assert resolve_agent_command(provider="codex") == (
        "codex exec -C {root} -s workspace-write --json "
        "-o {last_message} - < {provider_prompt_file}",
        "codex",
    )
    assert resolve_agent_command(provider="kimi")[1] == "kimi"
    assert resolve_agent_command(provider="opencode")[1] == "opencode"
    assert resolve_agent_command(provider="cursor")[1] == "cursor"
    assert resolve_agent_command(provider="goose")[1] == "goose"
    assert resolve_agent_command(provider="aider")[1] == "aider"
    assert resolve_agent_command(provider="openhands")[1] == "openhands"


def test_resolve_agent_command_rejects_unknown_provider(monkeypatch):
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    with pytest.raises(ValueError, match="unknown agent provider"):
        resolve_agent_command(provider="unknown")


def test_agent_provider_registry_exposes_commands_and_capabilities():
    assert agent_providers.provider_choices() == [
        "custom",
        "claude",
        "codex",
        "kimi",
        "opencode",
        "cursor",
        "goose",
        "aider",
        "openhands",
    ]
    assert agent_providers.provider_choices(include_custom=False) == [
        "claude",
        "codex",
        "kimi",
        "opencode",
        "cursor",
        "goose",
        "aider",
        "openhands",
    ]
    assert agent_providers.is_known_provider(" CLAUDE ")
    assert agent_providers.require_provider("codex").display_name == "Codex"
    assert agent_providers.provider_display_name("kimi") == "Kimi"
    assert (
        agent_providers.provider_command_template("codex")
        == "codex exec -C {root} -s workspace-write --json "
        "-o {last_message} - < {provider_prompt_file}"
    )
    assert (
        agent_providers.provider_command_template("opencode")
        == "opencode run --dir {root} --format json "
        "--file {task_json} {provider_prompt}"
    )
    assert (
        agent_providers.provider_command_template("cursor")
        == "cursor-agent-sidecar run --root {root} --task-json {task_json} "
        "--provider-prompt-file {provider_prompt_file} "
        "--mcp-config-file {mcp_config_file}"
    )
    assert agent_providers.provider_has_capability(
        "custom", agent_providers.CAPABILITY_CUSTOM_COMMAND
    )
    assert agent_providers.provider_has_capability(
        "claude", agent_providers.CAPABILITY_TASK_RUN
    )
    assert agent_providers.provider_has_capability(
        "kimi", agent_providers.CAPABILITY_MCP_CONFIG_FILE
    )
    assert agent_providers.provider_has_capability(
        "opencode", agent_providers.CAPABILITY_TASK_JSON_FILE
    )
    assert agent_providers.provider_has_capability(
        "cursor", agent_providers.CAPABILITY_PROVIDER_EVENT_PARSER
    )
    assert agent_providers.provider_has_capability(
        "goose", agent_providers.CAPABILITY_GENERIC_TEXT_PARSER
    )
    assert agent_providers.provider_has_capability(
        "aider", agent_providers.CAPABILITY_FRESH_SESSION
    )
    assert agent_providers.provider_has_capability(
        "openhands", agent_providers.CAPABILITY_JSON_OUTPUT
    )
    payload = {
        provider["id"]: provider
        for provider in agent_providers.provider_registry_payload()
    }
    assert payload["claude"]["display_name"] == "Claude"
    assert payload["custom"]["command_preset"] is None
    assert payload["kimi"]["command_preset"].startswith("kimi --work-dir")
    assert payload["opencode"]["display_name"] == "OpenCode"
    assert payload["cursor"]["display_name"] == "Cursor"
    assert "task_run" in payload["aider"]["capabilities"]
    assert "generic_text_parser" in payload["goose"]["capabilities"]
    assert "provider_event_parser" in payload["openhands"]["capabilities"]


def test_agent_provider_registry_loads_optional_json_specs(tmp_path, monkeypatch):
    spec_path = tmp_path / "agent-providers.json"
    spec_path.write_text(
        """
        {
          "providers": [
            {
              "id": "example",
              "display_name": "Example Agent",
              "command_preset": "example-agent --message-file {provider_prompt_file}",
              "capabilities": ["provider_prompt_file", "json_output"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    monkeypatch.setenv(agent_providers.PROVIDER_SPECS_ENV_VAR, str(spec_path))
    importlib.reload(agent_providers)
    agent_adapter_cmd.PROVIDER_COMMANDS = agent_providers.PROVIDER_COMMANDS
    try:
        assert agent_providers.provider_choices() == [
            "custom",
            "claude",
            "codex",
            "kimi",
            "opencode",
            "cursor",
            "goose",
            "aider",
            "openhands",
            "example",
        ]
        assert agent_providers.provider_display_name("example") == "Example Agent"
        assert (
            agent_providers.provider_command_template("example")
            == "example-agent --message-file {provider_prompt_file}"
        )
        assert resolve_agent_command(provider="example") == (
            "example-agent --message-file {provider_prompt_file}",
            "example",
        )
        assert agent_providers.provider_has_capability(
            "example", agent_providers.CAPABILITY_PROVIDER_PROMPT_FILE
        )
        assert agent_providers.provider_has_capability(
            "example", agent_providers.CAPABILITY_COMMAND_PRESET
        )
        payload = {
            provider["id"]: provider
            for provider in agent_providers.provider_registry_payload()
        }
        assert payload["example"]["display_name"] == "Example Agent"
    finally:
        monkeypatch.delenv(agent_providers.PROVIDER_SPECS_ENV_VAR, raising=False)
        importlib.reload(agent_providers)
        agent_adapter_cmd.PROVIDER_COMMANDS = agent_providers.PROVIDER_COMMANDS


def test_agent_adapter_cli_lists_provider_registry(capsys):
    from code_index.cli import main

    rc = main(["agent-adapter", "--list-providers", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["kind"] == "code_index_agent_provider_registry"
    providers = {provider["id"]: provider for provider in payload["providers"]}
    assert providers["codex"]["display_name"] == "Codex"
    assert providers["opencode"]["command_preset"].startswith("opencode run")
    assert providers["cursor"]["command_preset"].startswith("cursor-agent-sidecar run")
    assert providers["openhands"]["command_preset"].startswith("openhands --headless")


def test_parse_output_event_accepts_json_event_line():
    parsed = _parse_output_event(
        '{"event_type":"edit","file_path":"pkg/a.py","message":"patched file","payload":{"phase":"impl"}}',
        stream_name="stdout",
        command_label="agent {task_json}",
    )
    assert parsed["event_type"] == "edit"
    assert parsed["file_path"] == "pkg/a.py"
    assert parsed["message"] == "patched file"
    assert parsed["payload"]["phase"] == "impl"
    assert parsed["payload"]["structured"] is True


def test_parse_output_event_accepts_prefixed_status_line():
    parsed = _parse_output_event(
        "STATUS working reading the graph",
        stream_name="stderr",
        command_label="agent {task_json}",
    )
    assert parsed["event_type"] == "status"
    assert parsed["status"] == "working"
    assert parsed["message"] == "reading the graph"
    assert parsed["payload"]["stream"] == "stderr"


def test_parse_output_event_falls_back_to_tool_line():
    parsed = _parse_output_event(
        "ordinary provider output",
        stream_name="stdout",
        command_label="agent {task_json}",
    )
    assert parsed["event_type"] == "tool"
    assert parsed["message"] == "ordinary provider output"
    assert parsed["payload"]["structured"] is False


def test_parse_output_event_accepts_codex_jsonl_agent_message():
    parsed = _parse_output_event(
        '{"type":"item.completed","item":{"type":"agent_message","text":"Changed pkg/a.py\\nTests passed."}}',
        stream_name="stdout",
        command_label="codex exec --json",
    )
    assert parsed["event_type"] == "decision"
    assert parsed["message"] == "Changed pkg/a.py\nTests passed."
    assert parsed["payload"]["provider_event"] == "item.completed"


def test_parse_output_event_accepts_kimi_stream_json_assistant_message():
    parsed = _parse_output_event(
        (
            '{"role":"assistant","content":[{"type":"think","think":"private"},'
            '{"type":"text","text":"Changed pkg/a.py\\nTests passed."}]}'
        ),
        stream_name="stdout",
        command_label="kimi --print --output-format stream-json",
    )
    assert parsed["event_type"] == "decision"
    assert parsed["message"] == "Changed pkg/a.py\nTests passed."
    assert parsed["payload"]["provider_event"] == "kimi.message"
    assert parsed["payload"]["role"] == "assistant"


def test_parse_output_event_accepts_kimi_stream_json_tool_call():
    parsed = _parse_output_event(
        (
            '{"role":"assistant","content":"","tool_calls":[{"id":"tc_1",'
            '"type":"function","function":{"name":"Read",'
            '"arguments":"{\\"file_path\\":\\"pkg/a.py\\"}"}}]}'
        ),
        stream_name="stdout",
        command_label="kimi --print --output-format stream-json",
    )
    assert parsed["event_type"] == "read"
    assert parsed["file_path"] == "pkg/a.py"
    assert "Read" in parsed["message"]
    assert parsed["payload"]["provider_event"] == "kimi.message"


def test_parse_output_event_accepts_claude_stream_json_assistant_text():
    parsed = _parse_output_event(
        (
            '{"type":"assistant","session_id":"session_1","message":'
            '{"role":"assistant","content":[{"type":"text",'
            '"text":"Changed pkg/a.py\\nTests passed."}]}}'
        ),
        stream_name="stdout",
        command_label="claude -p --output-format stream-json",
    )
    assert parsed["event_type"] == "decision"
    assert parsed["message"] == "Changed pkg/a.py\nTests passed."
    assert parsed["payload"]["provider_event"] == "claude.assistant"
    assert parsed["payload"]["session_id"] == "session_1"


def test_parse_output_event_accepts_claude_stream_json_tool_use():
    parsed = _parse_output_event(
        (
            '{"type":"assistant","session_id":"session_1","message":'
            '{"role":"assistant","content":[{"type":"tool_use","name":"Edit",'
            '"input":{"file_path":"pkg/a.py"}}]}}'
        ),
        stream_name="stdout",
        command_label="claude -p --output-format stream-json",
    )
    assert parsed["event_type"] == "edit"
    assert parsed["file_path"] == "pkg/a.py"
    assert "Edit" in parsed["message"]
    assert parsed["payload"]["provider_event"] == "claude.assistant"


def test_parse_output_event_accepts_opencode_json_text():
    parsed = _parse_output_event(
        (
            '{"type":"text","timestamp":1767036064268,'
            '"sessionID":"ses_123","part":{"type":"text",'
            '"text":"Changed pkg/a.py\\nTests passed."}}'
        ),
        stream_name="stdout",
        command_label="opencode run --format json",
    )
    assert parsed["event_type"] == "decision"
    assert parsed["message"] == "Changed pkg/a.py\nTests passed."
    assert parsed["payload"]["provider_event"] == "opencode.text"
    assert parsed["payload"]["session_id"] == "ses_123"


def test_parse_output_event_accepts_opencode_json_tool_use():
    parsed = _parse_output_event(
        (
            '{"type":"tool_use","sessionID":"ses_123","part":{"type":"tool",'
            '"tool":"write","state":{"status":"completed",'
            '"input":{"file_path":"pkg/a.py"},"title":"Write pkg/a.py",'
            '"output":"ok","metadata":{"exit":0}}}}'
        ),
        stream_name="stdout",
        command_label="opencode run --format json",
    )
    assert parsed["event_type"] == "edit"
    assert parsed["file_path"] == "pkg/a.py"
    assert "OpenCode used write" in parsed["message"]
    assert parsed["payload"]["provider_event"] == "opencode.tool_use"


def test_parse_output_event_accepts_opencode_json_error():
    parsed = _parse_output_event(
        (
            '{"type":"error","sessionID":"ses_123",'
            '"error":{"name":"APIError","data":{"message":"Rate limit exceeded"}}}'
        ),
        stream_name="stdout",
        command_label="opencode run --format json",
    )
    assert parsed["event_type"] == "status"
    assert parsed["status"] == "failed"
    assert "Rate limit exceeded" in parsed["message"]
    assert parsed["payload"]["provider_event"] == "opencode.error"


def test_parse_output_event_accepts_cursor_sidecar_assistant_message():
    parsed = _parse_output_event(
        (
            '{"provider":"cursor","event":"assistant.message","role":"assistant",'
            '"run_id":"run_123","message":"Changed pkg/a.py\\nTests passed."}'
        ),
        stream_name="stdout",
        command_label="cursor-agent-sidecar run",
    )
    assert parsed["event_type"] == "decision"
    assert parsed["message"] == "Changed pkg/a.py\nTests passed."
    assert parsed["payload"]["provider_event"] == "cursor.assistant.message"
    assert parsed["payload"]["run_id"] == "run_123"


def test_parse_output_event_accepts_cursor_sidecar_tool_call():
    parsed = _parse_output_event(
        (
            '{"provider":"cursor","event":"tool.call","tool_name":"Edit",'
            '"arguments":{"file_path":"pkg/a.py"}}'
        ),
        stream_name="stdout",
        command_label="cursor-agent-sidecar run",
    )
    assert parsed["event_type"] == "edit"
    assert parsed["file_path"] == "pkg/a.py"
    assert "Cursor requested tool call" in parsed["message"]
    assert parsed["payload"]["provider_event"] == "cursor.tool.call"


def test_parse_output_event_accepts_openhands_json_action():
    parsed = _parse_output_event(
        '{"type":"action","action":"write","path":"pkg/a.py","content":"Creating file"}',
        stream_name="stdout",
        command_label="openhands --headless --json",
    )
    assert parsed["event_type"] == "edit"
    assert parsed["file_path"] == "pkg/a.py"
    assert parsed["message"] == "Creating file"
    assert parsed["payload"]["provider_event"] == "openhands.action"


def test_parse_output_event_accepts_openhands_json_result():
    parsed = _parse_output_event(
        '{"type":"result","success":true,"content":"Task completed"}',
        stream_name="stdout",
        command_label="openhands --headless --json",
    )
    assert parsed["event_type"] == "status"
    assert parsed["status"] == "completed"
    assert parsed["message"] == "Task completed"
    assert parsed["payload"]["provider_event"] == "openhands.result"


def test_format_command_provider_prompt_mentions_graph_context(tmp_path: Path):
    task_path = tmp_path / "task.json"
    task = {
        "run_id": "run-123",
        "message": "reduce this file",
        "selected_paths": ["pkg/a.py"],
        "selected_nodes": ["file:pkg/a.py"],
        "parent_run_id": "parent-1",
        "graph_context": {
            "selected_nodes": [{"path": "pkg/a.py", "care_level": "high"}],
            "related_nodes": [{"path": "pkg/b.py"}],
        },
        "collaboration": {
            "mailbox": {
                "global_events_jsonl": ".code_index/agent-runs/events.jsonl",
                "run_events_jsonl": ".code_index/agent-runs/run-123/events.jsonl",
            },
            "active_peer_runs": [{"run_id": "peer-1"}],
            "overlapping_file_events": [{"file_path": "pkg/a.py"}],
        },
    }

    formatted = _format_command(
        "agent {provider_prompt_raw} --out {last_message_raw}",
        task,
        root=tmp_path,
        task_json_path=task_path,
        last_message_path=tmp_path / "last-message.txt",
        provider_prompt_path=tmp_path / "provider-prompt.txt",
    )

    assert "graph_context" in formatted
    assert "context_packet.graph_context" in formatted
    assert "pkg/a.py" in formatted
    assert "pkg/b.py" in formatted
    assert "parent-1" in formatted
    assert ".code_index/agent-runs/events.jsonl" in formatted
    assert "Active peer runs: 1" in formatted
    assert "last-message.txt" in formatted


def _task_for_provider_prompt(
    *,
    edit_policy: str = "review_before_edit",
    selected_symbols: list[dict] | None = None,
) -> dict:
    return {
        "run_id": "run-123",
        "message": "review this",
        "selected_paths": ["mod.py"],
        "edit_policy": edit_policy,
        "selected_symbols": selected_symbols or [],
        "graph_context": {},
        "collaboration": {},
    }


def test_provider_prompt_review_before_edit_policy_adds_instruction(
    tmp_path: Path,
):
    prompt = _build_provider_prompt(
        _task_for_provider_prompt(edit_policy="review_before_edit"),
        root=tmp_path,
        task_json_path=tmp_path / "task.json",
    )

    assert "propose edits" in prompt.lower()


def test_provider_prompt_direct_edit_policy_does_not_add_review_instruction(
    tmp_path: Path,
):
    prompt = _build_provider_prompt(
        _task_for_provider_prompt(edit_policy="direct_edit"),
        root=tmp_path,
        task_json_path=tmp_path / "task.json",
    )

    assert "propose edits" not in prompt.lower()


def test_provider_prompt_selected_symbols_adds_find_symbol_instruction(
    tmp_path: Path,
):
    prompt = _build_provider_prompt(
        _task_for_provider_prompt(
            selected_symbols=[
                {
                    "symbol_uid": "u1",
                    "canonical_name": "mod.fn",
                    "kind": "function",
                    "def_file": "mod.py",
                    "def_line": 5,
                }
            ]
        ),
        root=tmp_path,
        task_json_path=tmp_path / "task.json",
    )

    assert "find_symbol" in prompt
    assert "mod.fn" in prompt


def test_format_command_codex_pipes_prompt_file(tmp_path: Path):
    task_path = tmp_path / "task.json"
    prompt_path = tmp_path / "provider-prompt.txt"
    formatted = _format_command(
        "codex exec -C {root} --json -o {last_message} - < {provider_prompt_file}",
        {
            "run_id": "run-123",
            "message": "reduce this file",
            "selected_paths": ["pkg/a.py"],
        },
        root=tmp_path,
        task_json_path=task_path,
        last_message_path=tmp_path / "last-message.txt",
        provider_prompt_path=prompt_path,
    )

    assert "<" in formatted
    assert "provider-prompt.txt" in formatted
    assert "reduce this file" not in formatted


def test_format_command_kimi_uses_prompt_and_mcp_files(tmp_path: Path):
    task_path = tmp_path / "task.json"
    prompt_path = tmp_path / "provider-prompt.txt"
    mcp_path = tmp_path / "mcp.json"
    formatted = _format_command(
        "kimi --work-dir {root} --mcp-config-file {mcp_config_file} "
        "--print --output-format stream-json < {provider_prompt_file}",
        {
            "run_id": "run-123",
            "message": "reduce this file",
            "selected_paths": ["pkg/a.py"],
        },
        root=tmp_path,
        task_json_path=task_path,
        last_message_path=tmp_path / "last-message.txt",
        provider_prompt_path=prompt_path,
        mcp_config_path=mcp_path,
    )

    assert "kimi --work-dir" in formatted
    assert "provider-prompt.txt" in formatted
    assert "mcp.json" in formatted
    assert "reduce this file" not in formatted


def test_run_command_callback_failure_terminates_process_and_reports_failure(
    tmp_path: Path, monkeypatch
):
    events: list[dict] = []

    def fake_post_json(_callback: str, payload: dict) -> dict:
        events.append(payload)
        return {"ok": True}

    monkeypatch.setattr(agent_adapter_cmd, "_post_json", fake_post_json)

    script_path = tmp_path / "sleeping_agent.py"
    started_path = tmp_path / "started.txt"
    finished_path = tmp_path / "finished.txt"
    script_path.write_text(
        (
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            "Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
            "time.sleep(20)\n"
            "Path(sys.argv[2]).write_text('finished', encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )
    task_json_path = tmp_path / "task.json"
    task_json_path.write_text("{}", encoding="utf-8")

    def broken_callback(_pid: int, _command_label: str) -> None:
        raise RuntimeError("registry unavailable")

    exit_code, result = agent_adapter_cmd._run_command(
        {"run_id": "run-callback-failure", "message": "callback failure"},
        callback="http://callback.invalid/events",
        command=f'"{sys.executable}" "{script_path}" "{started_path}" "{finished_path}"',
        root=tmp_path,
        task_json_path=task_json_path,
        process_started_callback=broken_callback,
    )

    time.sleep(0.2)
    assert exit_code != 0
    assert result["status"] == "failed"
    assert "process registry callback failed" in result["error"]
    assert "registry unavailable" in result["error"]
    assert not finished_path.exists()
    assert any(
        event["event_type"] == "status"
        and event["payload"].get("process_callback") == "failed"
        for event in events
    )


def test_run_command_sets_utf8_stdio_for_python_backed_providers(
    tmp_path: Path, monkeypatch
):
    events: list[dict] = []

    def fake_post_json(_callback: str, payload: dict) -> dict:
        events.append(payload)
        return {"ok": True}

    monkeypatch.setattr(agent_adapter_cmd, "_post_json", fake_post_json)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)

    script_path = tmp_path / "unicode_agent.py"
    script_path.write_text(
        (
            "import os\n"
            "import sys\n"
            "if os.environ.get('PYTHONUTF8') != '1':\n"
            "    print('missing PYTHONUTF8', file=sys.stderr)\n"
            "    sys.exit(3)\n"
            "if os.environ.get('PYTHONIOENCODING') != 'utf-8':\n"
            "    print('missing PYTHONIOENCODING', file=sys.stderr)\n"
            "    sys.exit(4)\n"
            "if os.environ.get('PYTHONUNBUFFERED') != '1':\n"
            "    print('missing PYTHONUNBUFFERED', file=sys.stderr)\n"
            "    sys.exit(5)\n"
            "print('DECISION unicode minus: \\u2212', flush=True)\n"
        ),
        encoding="utf-8",
    )
    task_json_path = tmp_path / "task.json"
    task_json_path.write_text("{}", encoding="utf-8")

    exit_code, result = agent_adapter_cmd._run_command(
        {"run_id": "run-unicode", "message": "unicode output"},
        callback="http://callback.invalid/events",
        command=f'"{sys.executable}" "{script_path}"',
        root=tmp_path,
        task_json_path=task_json_path,
    )

    assert exit_code == 0
    assert result["status"] == "completed"
    assert any(
        event["event_type"] == "decision"
        and "unicode minus: -" in event["message"].replace("\u2212", "-")
        for event in events
    )


def test_run_command_compacts_loguru_handler_tracebacks(tmp_path: Path, monkeypatch):
    events: list[dict] = []

    def fake_post_json(_callback: str, payload: dict) -> dict:
        events.append(payload)
        return {"ok": True}

    monkeypatch.setattr(agent_adapter_cmd, "_post_json", fake_post_json)
    script_path = tmp_path / "noisy_agent.py"
    script_path.write_text(
        (
            "import sys\n"
            "lines = [\n"
            "    '--- Logging error in Loguru Handler #1 ---',\n"
            "    \"Record was: {'message': 'Created new session'}\",\n"
            "    'Traceback (most recent call last):',\n"
            "    '  File \"loguru/_handler.py\", line 206, in emit',\n"
            "    '  File \"loguru/_file_sink.py\", line 276, in _terminate_file',\n"
            "    \"PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'C:\\\\Users\\\\mfeth\\\\.kimi\\\\logs\\\\kimi.log' -> 'C:\\\\Users\\\\mfeth\\\\.kimi\\\\logs\\\\kimi.2026-04-30.log'\",\n"
            "    '--- End of logging error ---',\n"
            "]\n"
            "for line in lines:\n"
            "    print(line, file=sys.stderr, flush=True)\n"
        ),
        encoding="utf-8",
    )
    task_json_path = tmp_path / "task.json"
    task_json_path.write_text("{}", encoding="utf-8")

    exit_code, result = agent_adapter_cmd._run_command(
        {"run_id": "run-noisy", "message": "noise"},
        callback="http://callback.invalid/events",
        command=f'"{sys.executable}" "{script_path}"',
        root=tmp_path,
        task_json_path=task_json_path,
    )

    assert exit_code == 0
    assert result["status"] == "completed"
    noise_events = [
        event
        for event in events
        if event["payload"].get("provider_noise") == "loguru_handler_error"
    ]
    assert len(noise_events) == 1
    assert "Loguru" in noise_events[0]["message"]
    assert noise_events[0]["payload"]["suppressed_lines"] == 7
    assert not any(
        "Record was:" in event["message"] or "loguru/_handler.py" in event["message"]
        for event in events
    )


def test_kimi_provider_uses_isolated_share_dir_seeded_from_existing_config(
    tmp_path: Path, monkeypatch
):
    events: list[dict] = []

    def fake_post_json(_callback: str, payload: dict) -> dict:
        events.append(payload)
        return {"ok": True}

    source_share = tmp_path / "source-kimi"
    (source_share / "credentials").mkdir(parents=True)
    (source_share / "config.toml").write_text("model = 'kimi-k2.6'\n", encoding="utf-8")
    (source_share / "device_id").write_text("device-id", encoding="utf-8")
    (source_share / "credentials" / "tokens.json").write_text(
        '{"access_token":"token"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KIMI_SHARE_DIR", str(source_share))
    monkeypatch.setattr(agent_adapter_cmd, "_post_json", fake_post_json)

    script_path = tmp_path / "inspect_env.py"
    script_path.write_text(
        (
            "import json, os\n"
            "from pathlib import Path\n"
            "share = Path(os.environ['KIMI_SHARE_DIR'])\n"
            "print(json.dumps({\n"
            "    'event_type': 'note',\n"
            "    'message': json.dumps({\n"
            "        'share': str(share),\n"
            "        'has_config': (share / 'config.toml').exists(),\n"
            "        'has_credentials': (share / 'credentials' / 'tokens.json').exists(),\n"
            "    }),\n"
            "}))\n"
        ),
        encoding="utf-8",
    )
    task_json_path = tmp_path / "task.json"
    task_json_path.write_text("{}", encoding="utf-8")

    exit_code, result = agent_adapter_cmd._run_command(
        {"run_id": "run-kimi-env", "message": "env"},
        callback="http://callback.invalid/events",
        command=f'"{sys.executable}" "{script_path}"',
        root=tmp_path,
        task_json_path=task_json_path,
        provider="kimi",
    )

    assert exit_code == 0
    assert result["status"] == "completed"
    note = next(event for event in events if event["event_type"] == "note")
    observed = json.loads(note["message"])
    assert Path(observed["share"]) != source_share
    assert observed["has_config"] is True
    assert observed["has_credentials"] is True
    assert not Path(observed["share"]).exists()
