"""Git hook installer: idempotent writes, core.hooksPath wiring, uninstall."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_index.commands.install_hooks_cmd import _HOOK_NAMES, install
from code_index import config as cfg_mod


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="git is not available on PATH"
)


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )


def test_non_git_repo_returns_warning_and_writes_nothing(tmp_path: Path):
    # Make sure the .code_index dir exists so config works, but no .git.
    (tmp_path / ".code_index").mkdir()
    report = install(tmp_path.resolve())
    assert report["is_git_repo"] is False
    assert report["hooks_written"] == []
    assert any("Not a git repository" in w for w in report["warnings"])
    assert not (tmp_path / ".code_index" / "hooks").exists()


def test_install_writes_hooks_and_sets_hookspath(tmp_path: Path):
    _init_repo(tmp_path)
    report = install(tmp_path.resolve())
    assert report["is_git_repo"] is True
    assert set(report["hooks_written"]) == set(_HOOK_NAMES)
    assert report["new_hooks_path"] == ".code_index/hooks"

    hooks_dir = tmp_path / ".code_index" / "hooks"
    for name in _HOOK_NAMES:
        p = hooks_dir / name
        assert p.is_file(), name
        body = p.read_text(encoding="utf-8")
        assert "#!/usr/bin/env bash" in body
        assert "python -m code_index update" in body

    # core.hooksPath is wired.
    res = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--local", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == ".code_index/hooks"


def test_install_is_idempotent(tmp_path: Path):
    _init_repo(tmp_path)
    r1 = install(tmp_path.resolve())
    hooks_dir = tmp_path / ".code_index" / "hooks"
    snapshot = {n: (hooks_dir / n).read_bytes() for n in _HOOK_NAMES}
    r2 = install(tmp_path.resolve())
    assert set(r2["hooks_written"]) == set(_HOOK_NAMES)
    for name in _HOOK_NAMES:
        assert (hooks_dir / name).read_bytes() == snapshot[name], name
    assert r2["new_hooks_path"] == r1["new_hooks_path"] == ".code_index/hooks"


def test_uninstall_removes_hooks_and_clears_path(tmp_path: Path):
    _init_repo(tmp_path)
    install(tmp_path.resolve())
    report = install(tmp_path.resolve(), uninstall=True)
    assert set(report["hooks_removed"]) == set(_HOOK_NAMES)
    assert report["new_hooks_path"] is None
    hooks_dir = tmp_path / ".code_index" / "hooks"
    for name in _HOOK_NAMES:
        assert not (hooks_dir / name).exists(), name
    # core.hooksPath unset.
    res = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--local", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0  # git config --get returns non-zero when unset


def test_cli_install_hooks_json_output(tmp_path: Path):
    _init_repo(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "install-hooks",
            "--root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["is_git_repo"] is True
    assert set(payload["hooks_written"]) == set(_HOOK_NAMES)
