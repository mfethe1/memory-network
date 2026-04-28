"""Pytest runner node-id emission for affected tests."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.pipeline import reindex


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_repo(tmp_path: Path, test_source: str) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_calc.py").write_text(
        textwrap.dedent(test_source).lstrip(), encoding="utf-8"
    )


def _index(tmp_path: Path) -> None:
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)


def _run_tests_cmd(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "tests",
            "add",
            "--root",
            str(tmp_path),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_pytest_runner_emits_plain_node_id(tmp_path: Path):
    _write_repo(
        tmp_path,
        """
        from pkg.calc import add

        def test_add_plain():
            assert add(1, 2) == 3
        """,
    )
    _index(tmp_path)

    proc = _run_tests_cmd(tmp_path, "--runner", "pytest")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["tests/test_calc.py::test_add_plain"]


def test_pytest_runner_expands_literal_parametrize_cases(tmp_path: Path):
    _write_repo(
        tmp_path,
        """
        import pytest
        from pkg.calc import add

        @pytest.mark.parametrize("a,b,expected", [(1, 2, 3), (0, 0, 0), (5, -2, 3)])
        def test_add_params(a, b, expected):
            assert add(a, b) == expected
        """,
    )
    _index(tmp_path)

    proc = _run_tests_cmd(tmp_path, "--runner", "pytest", "--runner-json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["runner"] == "pytest"
    assert payload["invocation"] == ["pytest", *payload["node_ids"]]
    assert payload["node_ids"] == [
        "tests/test_calc.py::test_add_params[1-2-3]",
        "tests/test_calc.py::test_add_params[0-0-0]",
        "tests/test_calc.py::test_add_params[5--2-3]",
    ]
    assert payload["skipped_tests"] == []


def test_pytest_runner_reports_non_literal_parametrize_case(tmp_path: Path):
    _write_repo(
        tmp_path,
        """
        import pytest
        from pkg.calc import add

        CASE = (1, 2, 3)

        @pytest.mark.parametrize("a,b,expected", [CASE])
        def test_add_dynamic(a, b, expected):
            assert add(a, b) == expected
        """,
    )
    _index(tmp_path)

    proc = _run_tests_cmd(tmp_path, "--runner", "pytest", "--runner-json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["node_ids"] == []
    assert payload["skipped_tests"] == [
        {
            "canonical_name": "tests.test_calc.test_add_dynamic",
            "reason": "parametrize arguments are not literal",
        }
    ]
