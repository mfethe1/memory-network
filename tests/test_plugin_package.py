"""Regression coverage for the repo-local Code Index Agent plugin package."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "code-index-agent"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_installer_module():
    import importlib.util

    script = PLUGIN_ROOT / "scripts" / "install_plugin.py"
    spec = importlib.util.spec_from_file_location("install_plugin", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_launcher_module():
    import importlib.util

    script = PLUGIN_ROOT / "scripts" / "start_graph_server.py"
    spec = importlib.util.spec_from_file_location("start_graph_server", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_index_agent_plugin_manifest_is_publishable():
    manifest = _load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    manifest_text = json.dumps(manifest)

    assert manifest["name"] == "code-index-agent"
    assert manifest["version"] == "0.1.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "TODO" not in manifest_text
    assert manifest["interface"]["displayName"] == "Code Index Agent"
    assert manifest["interface"]["composerIcon"] == "./assets/icon.svg"
    assert manifest["interface"]["screenshots"] == ["./assets/graph-demo.svg"]
    assert (PLUGIN_ROOT / "assets" / "icon.svg").exists()
    assert (PLUGIN_ROOT / "assets" / "graph-demo.svg").exists()
    assert (
        PLUGIN_ROOT / "skills" / "code-index-agent" / "SKILL.md"
    ).exists()


def test_code_index_agent_plugin_mcp_and_marketplace_entries():
    mcp = _load_json(PLUGIN_ROOT / ".mcp.json")
    server = mcp["mcpServers"]["code-index"]
    assert server["command"] == "python"
    assert server["args"] == [
        "-m",
        "code_index",
        "mcp-serve",
        "--root",
        ".",
    ]

    marketplace = _load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    entry = entries["code-index-agent"]
    assert entry["source"]["path"] == "./plugins/code-index-agent"
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_INSTALL"
    assert entry["category"] == "Developer Tools"


def test_code_index_agent_graph_launcher_help():
    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "start_graph_server.py"),
            "--help",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "--agent-command" in result.stdout
    assert "--provider" in result.stdout
    assert "--scope" in result.stdout
    assert "--ensure-index" in result.stdout
    assert "graph-server" in result.stdout


def test_code_index_agent_graph_launcher_prepares_external_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_launcher_module()
    calls: list[tuple[list[str], str, dict[str, str]]] = []

    def fake_check_call(command, *, cwd, env):
        calls.append((command, cwd, env))

    monkeypatch.setattr(module.subprocess, "check_call", fake_check_call)
    env = module._with_source_pythonpath({})

    module._ensure_index(tmp_path, env)

    assert calls
    command, cwd, call_env = calls[0]
    assert command[:3] == [sys.executable, "-m", "code_index"]
    assert command[3] == "init"
    assert str(tmp_path) in command
    assert cwd == str(tmp_path)
    assert str(ROOT) in call_env["PYTHONPATH"]

    calls.clear()
    (tmp_path / ".code_index").mkdir()
    (tmp_path / ".code_index" / "index.db").write_text("", encoding="utf-8")
    module._ensure_index(tmp_path, env)
    assert calls == []

    module._ensure_index(tmp_path, env, refresh=True)
    assert calls[0][0][3:6] == ["update", "--root", str(tmp_path)]


def test_code_index_agent_graph_launcher_preflights_agent_command():
    ok = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "start_graph_server.py"),
            "--agent-command",
            f'"{sys.executable}"',
            "--check-only",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert ok.returncode == 0
    assert "found" in ok.stdout

    missing = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "start_graph_server.py"),
            "--agent-command",
            "definitely-missing-code-index-agent",
            "--check-only",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert missing.returncode == 2
    assert "requires executable" in missing.stderr


def test_code_index_agent_installer_writes_repo_local_config(tmp_path: Path):
    module = _load_installer_module()

    report = module.install(tmp_path, provider="codex", port="8899")
    assert report["graph_url"] == "http://127.0.0.1:8899/repo-graph.html"
    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert (tmp_path / ".code_index" / "agent-plugin.json").exists()
    assert (tmp_path / ".code_index" / "demo-agent-task.json").exists()
    assert (tmp_path / ".code_index" / "start-code-index-agent.ps1").exists()
    assert (tmp_path / ".code_index" / "start-code-index-agent.sh").exists()

    mcp = _load_json(tmp_path / ".mcp.json")
    assert mcp["mcpServers"]["code-index"]["args"] == [
        "-m",
        "code_index",
        "mcp-serve",
        "--root",
        ".",
    ]
    config = _load_json(tmp_path / ".code_index" / "agent-plugin.json")
    assert config["graph_server"]["provider"] == "codex"
    assert "--provider codex" in config["commands"]["start_graph"]
    assert "--ensure-index" in config["commands"]["start_graph"]
    assert config["mcp_server"]["env"]["PYTHONPATH"] == str(ROOT)
    assert mcp["mcpServers"]["code-index"]["env"]["PYTHONPATH"] == str(ROOT)
    demo_task = _load_json(tmp_path / ".code_index" / "demo-agent-task.json")
    assert (
        demo_task["callback"]["agent_events_url"]
        == "http://127.0.0.1:8899/api/agent-events"
    )


def test_code_index_agent_installer_default_port_matches_graph_server(tmp_path: Path):
    module = _load_installer_module()

    report = module.install(tmp_path, provider="custom", dry_run=True)

    assert report["graph_url"] == "http://127.0.0.1:8767/repo-graph.html"


def test_code_index_agent_installer_refuses_invalid_existing_json(tmp_path: Path):
    module = _load_installer_module()
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(ValueError, match="contains invalid JSON"):
        module.install(tmp_path, provider="codex")

    assert mcp_path.read_text(encoding="utf-8") == "{invalid json"


def test_code_index_agent_installer_cli_reports_invalid_existing_json(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text("{invalid json", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "install_plugin.py"),
            "--root",
            str(tmp_path),
            "--provider",
            "codex",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "contains invalid JSON" in result.stderr
