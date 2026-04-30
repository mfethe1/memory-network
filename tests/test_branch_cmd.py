"""Tests for `code_index branch` subcommands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from code_index.cli import main


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not on PATH")


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test Author"],
        check=True,
        capture_output=True,
    )


def _commit_all(tmp_path: Path, msg: str) -> None:
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-A"],
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test Author"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test Author"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", msg],
        check=True,
        capture_output=True,
        env=env,
    )


def _tiny_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        textwrap.dedent(
            """
            def hello() -> str:
                return "hello"


            class Thing:
                def do(self) -> int:
                    return 42
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _setup_branched_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _tiny_repo(tmp_path)
    _commit_all(tmp_path, "initial commit")
    # create feature branch with a change
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
        check=True,
        capture_output=True,
    )
    (tmp_path / "pkg" / "b.py").write_text("def new_func(): return 1\n", encoding="utf-8")
    _commit_all(tmp_path, "add b.py")


def test_branch_list_shows_current_and_branches(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["current"] == "feature"
    names = {b["name"] for b in payload["branches"]}
    assert "main" in names
    assert "feature" in names
    assert any(b["current"] for b in payload["branches"] if b["name"] == "feature")


def test_branch_diff_shows_changed_files(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "diff", "main"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["base_branch"] == "main"
    assert payload["target_branch"] == "feature"
    assert any(f["path"] == "pkg/b.py" and f["status"] == "added" for f in payload["changed_files"])


def test_branch_files_enriches_with_chunks(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "files", "main"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["base_branch"] == "main"
    files = payload["files"]
    assert any(f["path"] == "pkg/b.py" for f in files)
    bpy = next(f for f in files if f["path"] == "pkg/b.py")
    assert bpy["status"] == "added"
    assert "symbol_count" in bpy
    assert isinstance(bpy["chunks"], list)


def test_branch_impact_shows_summary(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "impact", "main"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["base_branch"] == "main"
    assert "summary" in payload
    assert isinstance(payload["summary"]["changed_files"], int)
    assert isinstance(payload["summary"]["commits"], int)
    assert isinstance(payload["changed_files"], list)
    assert isinstance(payload["commits"], list)


def test_branch_compare_two_refs(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "compare", "main", "feature"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["base_branch"] == "main"
    assert payload["target_branch"] == "feature"
    assert any(f["path"] == "pkg/b.py" for f in payload["changed_files"])


def test_branch_diff_requires_target(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "diff"])
    assert rc == 2
    assert "requires a target" in capsys.readouterr().out


def test_branch_compare_requires_two_refs(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(["branch", "--root", str(tmp_path), "--json", "compare", "main"])
    assert rc == 2
    assert "two refs" in capsys.readouterr().out


def test_branch_diff_invalid_ref_raises(tmp_path: Path):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    with pytest.raises(RuntimeError):
        main(["branch", "--root", str(tmp_path), "--json", "diff", "nonexistent"])


def test_branch_files_invalid_ref_raises(tmp_path: Path):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    with pytest.raises(RuntimeError):
        main(["branch", "--root", str(tmp_path), "--json", "files", "nonexistent"])


def test_branch_impact_invalid_ref_raises(tmp_path: Path):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    with pytest.raises(RuntimeError):
        main(["branch", "--root", str(tmp_path), "--json", "impact", "nonexistent"])


def test_branch_compare_invalid_ref_raises(tmp_path: Path):
    _setup_branched_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    with pytest.raises(RuntimeError):
        main(["branch", "--root", str(tmp_path), "--json", "compare", "nonexistent", "main"])


def test_branch_without_index_returns_error(tmp_path: Path, capsys: pytest.CaptureFixture):
    _setup_branched_repo(tmp_path)
    rc = main(["branch", "--root", str(tmp_path), "--json", "list"])
    assert rc == 2
    assert "no index" in capsys.readouterr().out
