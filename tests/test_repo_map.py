"""Acceptance tests for `code_index repo-map`."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import reindex


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_fixture_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "lowlevel.py").write_text(
        "def deep_target(x):\n    return x + 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "midlevel.py").write_text(
        textwrap.dedent(
            """
            from pkg.lowlevel import deep_target

            def wrapper(x):
                return deep_target(x) * 2

            def another_caller(x):
                return deep_target(x) + wrapper(x)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "toplevel.py").write_text(
        textwrap.dedent(
            """
            from pkg.midlevel import wrapper, another_caller

            class Orchestrator:
                def run(self, x):
                    return wrapper(x) + another_caller(x)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_things.py").write_text(
        textwrap.dedent(
            """
            from pkg.lowlevel import deep_target

            def test_deep_target():
                assert deep_target(3) == 4
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _run_cmd(tmp_path: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "repo-map",
            "--root",
            str(tmp_path),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_repo_map_json_returns_ranked_symbols(tmp_path: Path):
    _write_fixture_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    proc = _run_cmd(tmp_path, "--format", "json", "--limit", "10")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    symbols = payload["symbols"]
    assert isinstance(symbols, list)
    assert len(symbols) > 0
    # Required JSON keys per the plan's stable contract.
    required = {
        "canonical_name",
        "kind",
        "def_file",
        "def_line",
        "signature",
        "in_degree",
        "test_count",
        "score",
    }
    for entry in symbols:
        assert required.issubset(entry.keys())
    # Ordered by descending score.
    scores = [e["score"] for e in symbols]
    assert scores == sorted(scores, reverse=True)


def test_repo_map_budget_tokens_shrinks_output(tmp_path: Path):
    _write_fixture_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    unbounded = _run_cmd(tmp_path, "--format", "json", "--limit", "100")
    assert unbounded.returncode == 0
    bounded = _run_cmd(
        tmp_path, "--format", "json", "--limit", "100", "--budget-tokens", "200"
    )
    assert bounded.returncode == 0
    assert len(bounded.stdout) < len(unbounded.stdout)
    bounded_payload = json.loads(bounded.stdout)
    # Rough token heuristic: chars/4 must fit within budget.
    assert len(bounded.stdout) / 4 <= 200 or len(bounded_payload["symbols"]) == 0


def test_repo_map_excludes_test_file_symbols(tmp_path: Path):
    _write_fixture_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    proc = _run_cmd(tmp_path, "--format", "json", "--limit", "10")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    top10 = payload["symbols"][:10]
    for entry in top10:
        def_file = entry["def_file"] or ""
        assert "tests/" not in def_file and not def_file.startswith("tests/") or False
        # Stricter: none of the top 10 should have def_file under tests/.
        assert not def_file.startswith("tests/")
        assert "/tests/" not in "/" + def_file
        # And no canonical_name should be a test symbol.
        assert not entry["canonical_name"].startswith("tests.")
