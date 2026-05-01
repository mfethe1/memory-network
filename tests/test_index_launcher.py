from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from code_index import index_launcher


ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    pid = 4321


def test_index_console_script_points_at_background_launcher():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["index"] == "code_index.index_launcher:main"


def test_index_launcher_starts_agent_plugin_in_background_from_current_scope(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    scope = repo / "packages" / "api"
    (repo / ".code_index").mkdir(parents=True)
    scope.mkdir(parents=True)
    monkeypatch.chdir(scope)

    launches: list[dict[str, object]] = []

    def fake_popen(command, **kwargs):
        launches.append({"command": command, "kwargs": kwargs})
        return FakeProcess()

    rc = index_launcher.main(
        ["--skip-provider-check"],
        popen=fake_popen,
        python_executable="python-test",
    )

    assert rc == 0
    assert launches
    command = launches[0]["command"]
    kwargs = launches[0]["kwargs"]
    assert command[:4] == ["python-test", "-m", "code_index", "agent-plugin"]
    assert command[4:8] == ["start", "--root", str(repo.resolve()), "--scope"]
    assert command[8] == "packages/api"
    assert "--ensure-index" in command
    assert "--skip-provider-check" in command
    assert kwargs["cwd"] == str(repo.resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["close_fds"] is True
    assert isinstance(kwargs["env"], dict)
    assert "PYTHONPATH" in kwargs["env"]
    assert (repo / ".code_index" / "graph-agent-companion.pid").read_text(
        encoding="utf-8"
    ) == "4321\n"

    output = capsys.readouterr().out
    assert "Graph Agent Companion launched in background" in output
    assert "http://127.0.0.1:8767/repo-graph.html" in output
    assert "graph-agent-companion.log" in output


def test_index_launcher_uses_detached_creation_flags_on_windows(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(index_launcher.os, "name", "nt")
    monkeypatch.setattr(
        index_launcher.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
        raising=False,
    )
    monkeypatch.setattr(
        index_launcher.subprocess,
        "DETACHED_PROCESS",
        0x00000008,
        raising=False,
    )

    kwargs = index_launcher._detached_kwargs(
        tmp_path,
        {},
        object(),
    )

    assert kwargs["creationflags"] == 0x00000208
    assert "start_new_session" not in kwargs


def test_index_launcher_uses_new_session_on_posix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(index_launcher.os, "name", "posix")

    kwargs = index_launcher._detached_kwargs(
        tmp_path,
        {},
        object(),
    )

    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
