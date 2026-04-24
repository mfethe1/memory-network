from pathlib import Path

from code_index.ignore import IgnoreMatcher, build


def test_always_skip(tmp_path: Path):
    matcher = build(tmp_path)
    assert matcher.is_ignored(tmp_path / ".git" / "HEAD")
    assert matcher.is_ignored(tmp_path / ".code_index" / "index.db")
    assert matcher.is_ignored(tmp_path / "__pycache__" / "foo.pyc")
    assert matcher.is_ignored(tmp_path / "node_modules" / "pkg" / "index.js")


def test_gitignore_patterns(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n!keep.log\n", encoding="utf-8")
    matcher = build(tmp_path)
    assert matcher.is_ignored(tmp_path / "trace.log")
    assert matcher.is_ignored(tmp_path / "nested" / "error.log")
    assert matcher.is_ignored(tmp_path / "build", is_dir=True)
    # negation: the rule engine preserves the last-match semantics.
    (tmp_path / "keep.log").write_text("", encoding="utf-8")
    assert not matcher.is_ignored(tmp_path / "keep.log")


def test_hidden_files_skipped_by_default(tmp_path: Path):
    matcher = build(tmp_path)
    assert matcher.is_ignored(tmp_path / ".env")


def test_include_hidden_flag(tmp_path: Path):
    matcher = build(tmp_path, include_hidden=True)
    assert not matcher.is_ignored(tmp_path / "normal.py")
