"""Git history integration: populate `files.git_blob_oid`,
`files.git_committed_at`, `files.git_author` on each reindex."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.git_meta import resolver_for
from code_index.pipeline import reindex


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not on PATH")


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test Author"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test Author"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
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
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", msg],
        check=True,
        capture_output=True,
    )


def test_resolver_disabled_on_non_git_repo(tmp_path: Path):
    meta = resolver_for(tmp_path)
    assert meta.enabled is False
    assert meta.blob_oid("any.py") is None
    assert meta.commit_info("any.py") == (None, None)


def test_resolver_populates_blob_oids_on_git_repo(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    _commit_all(tmp_path, "initial")

    meta = resolver_for(tmp_path)
    assert meta.enabled is True
    assert meta.blob_oid("pkg/mod.py") is not None
    ts, author = meta.commit_info("pkg/mod.py")
    assert ts is not None and ts > 0
    assert author == "Test Author"


def test_reindex_populates_git_columns(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    _commit_all(tmp_path, "initial commit")

    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        reindex(conn, config, paths=None, event_source="init")
        row = conn.execute(
            """
            SELECT git_blob_oid, git_committed_at, git_author
              FROM files WHERE file_path = 'pkg/mod.py'
            """
        ).fetchone()
        assert row is not None
        assert row["git_blob_oid"] is not None
        assert row["git_committed_at"] is not None
        assert row["git_author"] == "Test Author"
    finally:
        db_mod.close(conn)


def test_untracked_files_leave_git_columns_null(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "tracked.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    _commit_all(tmp_path, "initial")
    # Now create an untracked file — committed_at must be NULL for it.
    (tmp_path / "pkg" / "untracked.py").write_text(
        "def g():\n    return 2\n", encoding="utf-8"
    )
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        reindex(conn, config, paths=None, event_source="init")
        tracked = conn.execute(
            "SELECT git_blob_oid FROM files WHERE file_path = 'pkg/tracked.py'"
        ).fetchone()
        untracked = conn.execute(
            "SELECT git_blob_oid, git_committed_at FROM files WHERE file_path = 'pkg/untracked.py'"
        ).fetchone()
        assert tracked["git_blob_oid"] is not None
        assert untracked["git_blob_oid"] is None
        assert untracked["git_committed_at"] is None
    finally:
        db_mod.close(conn)


def test_doctor_surfaces_git_summary(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    import json, sys

    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parent.parent)
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "doctor",
            "--root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "git" in payload
    assert payload["git"]["available"] is True
    assert payload["git"]["tracked_files"] >= 1
