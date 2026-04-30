"""Tests for `code_index workspace` subcommands."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from code_index.cli import main


def _tiny_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        textwrap.dedent(
            """
            def hello() -> str:
                return "hello"
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_workspace_init_creates_file(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "--json",
            "init",
            "--name",
            "myws",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["name"] == "myws"
    assert Path(payload["workspace"]).exists()


def test_workspace_init_refuses_existing(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    rc = main(["workspace", "--workspace-file", str(ws_file), "init"])
    assert rc == 2
    assert "already exists" in capsys.readouterr().out


def test_workspace_add_remove_list_cycle(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    repo = tmp_path / "repo1"
    repo.mkdir()
    _tiny_repo(repo)

    # init
    assert main(["workspace", "--workspace-file", str(ws_file), "--json", "init"]) == 0
    capsys.readouterr()

    # add
    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "--json",
            "add",
            "--path",
            str(repo),
            "--name",
            "repo1",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["added"]["name"] == "repo1"

    # list
    rc = main(["workspace", "--workspace-file", str(ws_file), "--json", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert any(m["name"] == "repo1" for m in payload["members"])

    # remove
    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "--json",
            "remove",
            "--name",
            "repo1",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["removed"] == "repo1"

    # list empty
    rc = main(["workspace", "--workspace-file", str(ws_file), "--json", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["members"] == []


def test_workspace_add_rejects_duplicate(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    repo = tmp_path / "repo1"
    repo.mkdir()
    _tiny_repo(repo)

    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    assert (
        main(
            [
                "workspace",
                "--workspace-file",
                str(ws_file),
                "add",
                "--path",
                str(repo),
                "--name",
                "repo1",
            ]
        )
        == 0
    )
    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "add",
            "--path",
            str(repo),
            "--name",
            "repo1",
        ]
    )
    assert rc == 2
    assert "already exists" in capsys.readouterr().out


def test_workspace_add_rejects_non_directory(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "add",
            "--path",
            str(tmp_path / "nope"),
        ]
    )
    assert rc == 2
    assert "not a directory" in capsys.readouterr().out


def test_workspace_remove_rejects_missing(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "remove",
            "--name",
            "missing",
        ]
    )
    assert rc == 2
    assert "not found" in capsys.readouterr().out


def test_workspace_status_shows_health(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    repo = tmp_path / "repo1"
    repo.mkdir()
    _tiny_repo(repo)
    assert main(["init", "--root", str(repo), "--json"]) == 0
    capsys.readouterr()

    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "workspace",
                "--workspace-file",
                str(ws_file),
                "add",
                "--path",
                str(repo),
                "--name",
                "repo1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    rc = main(["workspace", "--workspace-file", str(ws_file), "--json", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["workspace"] == str(ws_file)
    member = payload["members"][0]
    assert member["name"] == "repo1"
    assert member["has_index"] is True
    assert member["healthy"] is True
    assert isinstance(member["files"], int)
    assert isinstance(member["symbols"], int)


def test_workspace_query_searches_across_members(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    repo = tmp_path / "repo1"
    repo.mkdir()
    _tiny_repo(repo)
    assert main(["init", "--root", str(repo), "--json"]) == 0
    capsys.readouterr()

    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "workspace",
                "--workspace-file",
                str(ws_file),
                "add",
                "--path",
                str(repo),
                "--name",
                "repo1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "--json",
            "query",
            "--pattern",
            "hello",
            "--limit",
            "10",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["query"] == "hello"
    assert any(r.get("repo") == "repo1" for r in payload["results"])


def test_workspace_query_requires_pattern(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    rc = main(["workspace", "--workspace-file", str(ws_file), "--json", "query"])
    assert rc == 2
    assert "requires a pattern" in capsys.readouterr().out


def test_workspace_graph_aggregates_symbols(tmp_path: Path, capsys: pytest.CaptureFixture):
    ws_file = tmp_path / "workspace.json"
    repo = tmp_path / "repo1"
    repo.mkdir()
    _tiny_repo(repo)
    assert main(["init", "--root", str(repo), "--json"]) == 0
    capsys.readouterr()

    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "workspace",
                "--workspace-file",
                str(ws_file),
                "add",
                "--path",
                str(repo),
                "--name",
                "repo1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    rc = main(
        [
            "workspace",
            "--workspace-file",
            str(ws_file),
            "--json",
            "graph",
            "--limit",
            "10",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["workspace"] == tmp_path.name
    assert "repo1" in payload["members"]
    assert isinstance(payload["symbols"], list)
    assert any(s.get("repo") == "repo1" for s in payload["symbols"])


def test_workspace_graph_empty_workspace_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    ws_file = tmp_path / "workspace.json"
    assert main(["workspace", "--workspace-file", str(ws_file), "init"]) == 0
    rc = main(["workspace", "--workspace-file", str(ws_file), "graph"])
    assert rc == 2
    assert "no members" in capsys.readouterr().out
