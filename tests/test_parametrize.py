"""@pytest.mark.parametrize reporting — tests carry a compact case list."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.parsers.python_ast import PythonAstParser, _extract_parametrize
from code_index.pipeline import reindex
import ast


def test_extract_parametrize_string_argnames():
    src = textwrap.dedent(
        """
        import pytest

        @pytest.mark.parametrize("a, b", [(1, 2), (3, 4)])
        def test_add(a, b):
            assert a + b == b + a
        """
    ).lstrip()
    tree = ast.parse(src)
    func = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "test_add"
    )
    summary = _extract_parametrize(func)
    assert summary is not None
    assert summary["argnames"] == ["a", "b"]
    assert summary["case_count"] == 2
    assert summary["cases"] == ["(1, 2)", "(3, 4)"]
    assert summary["truncated"] is False


def test_extract_parametrize_list_argnames_large_case_list():
    # 20 cases — exceeds our 16-cap; list is truncated but count is accurate.
    cases = ", ".join(f"({i}, {i + 1})" for i in range(20))
    src = textwrap.dedent(
        f"""
        import pytest

        @pytest.mark.parametrize(["a", "b"], [{cases}])
        def test_many(a, b):
            pass
        """
    ).lstrip()
    tree = ast.parse(src)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    summary = _extract_parametrize(func)
    assert summary is not None
    assert summary["case_count"] == 20
    assert len(summary["cases"]) == 16
    assert summary["truncated"] is True


def test_function_without_parametrize_has_no_summary():
    src = "def test_plain():\n    assert True\n"
    tree = ast.parse(src)
    func = tree.body[0]
    assert _extract_parametrize(func) is None


def test_tests_command_surfaces_parametrize(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_calc.py").write_text(
        textwrap.dedent(
            """
            import pytest
            from pkg.calc import add

            @pytest.mark.parametrize("a, b, expected", [(1, 2, 3), (0, 0, 0), (5, -2, 3)])
            def test_add_params(a, b, expected):
                assert add(a, b) == expected

            def test_add_plain():
                assert add(1, 2) == 3
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    import subprocess, sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "tests",
            "add",
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

    by_name = {t["canonical_name"]: t for t in payload["affected_tests"]}
    param_test = next(
        (t for name, t in by_name.items() if "test_add_params" in name), None
    )
    plain_test = next(
        (t for name, t in by_name.items() if "test_add_plain" in name), None
    )
    assert param_test is not None, by_name
    assert plain_test is not None, by_name

    # Parametrized test exposes the summary.
    p = param_test["parametrize"]
    assert p is not None
    assert p["argnames"] == ["a", "b", "expected"]
    assert p["case_count"] == 3
    assert "(1, 2, 3)" in p["cases"]

    # Plain test has no parametrize.
    assert plain_test["parametrize"] is None

    # Summary rolls these up.
    s = payload["summary"]
    assert s["parametrized_test_count"] == 1
    assert s["parametrized_case_total"] == 3
