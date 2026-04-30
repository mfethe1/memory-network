"""Focused coverage for agent-adapter command resolution."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from code_index import agent_providers
from code_index.commands import agent_adapter_cmd
from code_index.commands.agent_adapter_cmd import (
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
        "claude -p {provider_prompt}",
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
        "--max-ralph-iterations 1 < {provider_prompt_file}",
        "kimi",
    )


def test_resolve_agent_command_explicit_provider_overrides_env_command(monkeypatch):
    monkeypatch.setenv("CODE_INDEX_AGENT_COMMAND", "custom-agent {task_json}")
    assert resolve_agent_command(provider="codex") == (
        "codex exec -C {root} -s workspace-write --json "
        "-o {last_message} - < {provider_prompt_file}",
        "codex",
    )
    assert resolve_agent_command(provider="kimi")[1] == "kimi"


def test_resolve_agent_command_rejects_unknown_provider(monkeypatch):
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    with pytest.raises(ValueError, match="unknown agent provider"):
        resolve_agent_command(provider="unknown")


def test_agent_provider_registry_exposes_commands_and_capabilities():
    assert agent_providers.provider_choices() == ["custom", "claude", "codex", "kimi"]
    assert agent_providers.provider_display_name("kimi") == "Kimi"
    assert (
        agent_providers.provider_command_template("codex")
        == "codex exec -C {root} -s workspace-write --json "
        "-o {last_message} - < {provider_prompt_file}"
    )
    assert agent_providers.provider_has_capability(
        "custom", agent_providers.CAPABILITY_CUSTOM_COMMAND
    )
    assert agent_providers.provider_has_capability(
        "kimi", agent_providers.CAPABILITY_MCP_CONFIG_FILE
    )
    payload = {
        provider["id"]: provider
        for provider in agent_providers.provider_registry_payload()
    }
    assert payload["claude"]["display_name"] == "Claude"
    assert payload["custom"]["command_preset"] is None


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
