"""Watch / ignore hardening — pure-function filter on relative posix paths."""

from __future__ import annotations

import pytest

from code_index.commands.watch_cmd import should_skip_watch_event


@pytest.mark.parametrize(
    "path, expected",
    [
        # Source files we DO want to reindex.
        ("code_index/pipeline.py", False),
        ("tests/test_pipeline.py", False),
        ("docs/claude-code.md", False),
        ("pkg/sub/mod.py", False),
    ],
)
def test_regular_source_files_pass_filter(path, expected):
    skip, _ = should_skip_watch_event(path)
    assert skip is expected


@pytest.mark.parametrize(
    "path",
    [
        # Index self-writes.
        ".code_index/index.db",
        ".code_index/index.db-wal",
        ".code_index/index.db-shm",
        # Claude layer.
        ".claude/CLAUDE.md",
        ".claude/skills/code-index/SKILL.md",
        # Git internals.
        ".git/HEAD",
        ".git/refs/heads/main",
        # Caches we must never reindex.
        ".pytest_cache/nodeids",
        ".ruff_cache/0.14.5/1930230194260213178",
        ".mypy_cache/3.12/cache.json",
        "__pycache__/module.cpython-312.pyc",
        "code_index/__pycache__/db.cpython-312.pyc",
        # Virtualenvs.
        ".venv/lib/site-packages/foo.py",
        "venv/bin/python",
        # Vendored/build dirs.
        "node_modules/pkg/index.js",
        "dist/code_index-0.1.0.whl",
        "build/lib/foo.py",
        "target/debug/app.exe",
        # Editor/OS junk.
        "src/.#edit-lock.py",
        "src/#autosave#.py",
        "src/foo.py~",
        "src/.~foo.py",
        ".DS_Store",
        "Thumbs.db",
        "src/.foo.py.swp",
        # Binary / compiled artifacts.
        "bin/tool.exe",
        "obj/main.o",
        "images/logo.png",
        "assets/video.mp4",
        "data/checkpoint.parquet",
        "dist/app.jar",
        "cache.sqlite-wal",
    ],
)
def test_junk_paths_are_skipped(path):
    skip, reason = should_skip_watch_event(path)
    assert skip is True, f"{path!r} should be filtered out; got skip={skip}"
    assert reason, "skip must carry a reason string"


def test_empty_and_absolute_paths_are_skipped():
    assert should_skip_watch_event("")[0] is True
    assert should_skip_watch_event("/abs/posix/path.py")[0] is True
    assert should_skip_watch_event("\\abs\\windows\\path.py")[0] is True


def test_reason_prefixes_are_stable():
    """The filter reason tags are part of the debug contract. Stabilise them."""
    # Binary extension reasons name the extension for grep-ability.
    _, reason = should_skip_watch_event("a/b.pyc")
    assert reason == "binary_ext:.pyc"
    # Blocked-directory reasons name the offending segment.
    _, reason = should_skip_watch_event(".code_index/index.db")
    # NOTE: .code_index/ hits the blocked-dir short-circuit before binary
    # extension checks because the directory segment is scanned first.
    assert reason.startswith("blocked_dir:.code_index") or reason.startswith(
        "binary_ext:"
    )
