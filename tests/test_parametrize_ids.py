"""Custom ids= on @pytest.mark.parametrize is captured and used by the
pytest runner formatter."""

from __future__ import annotations

import ast
import textwrap

from code_index.parsers.python_ast import _extract_parametrize
from code_index.runners.pytest import build_pytest_invocation


def _func(src: str, name: str | None = None):
    """Return a FunctionDef from module-level statements.

    When `name` is given, pick the function with that exact name. Otherwise
    return the first test_* function so helper defs don't accidentally win.
    """
    tree = ast.parse(textwrap.dedent(src).lstrip())
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if name is not None:
        return next(f for f in funcs if f.name == name)
    return next(f for f in funcs if f.name.startswith("test_"))


def test_explicit_literal_ids_captured():
    func = _func(
        """
        import pytest

        @pytest.mark.parametrize("x, y", [(1, 2), (3, 4)], ids=["small", "big"])
        def test_pairs(x, y):
            assert True
        """
    )
    summary = _extract_parametrize(func)
    assert summary is not None
    assert summary["ids"] == ["small", "big"]
    assert summary["ids_callable"] is False


def test_callable_ids_flagged_not_captured():
    func = _func(
        """
        import pytest

        def make_id(val):
            return f"id-{val}"

        @pytest.mark.parametrize("x", [1, 2], ids=make_id)
        def test_callable_ids(x):
            assert True
        """
    )
    summary = _extract_parametrize(func)
    assert summary is not None
    assert summary["ids"] is None
    assert summary["ids_callable"] is True


def test_missing_ids_returns_none_and_false():
    func = _func(
        """
        import pytest

        @pytest.mark.parametrize("x", [1, 2])
        def test_no_ids(x):
            assert True
        """
    )
    summary = _extract_parametrize(func)
    assert summary is not None
    assert summary["ids"] is None
    assert summary["ids_callable"] is False


def test_runner_uses_explicit_ids_for_node_ids():
    row = {
        "canonical_name": "tests.test_pairs.test_pairs",
        "def_file": "tests/test_pairs.py",
        "parametrize": {
            "argnames": ["x", "y"],
            "case_count": 2,
            "cases": ["(1, 2)", "(3, 4)"],
            "truncated": False,
            "ids": ["small", "big"],
            "ids_callable": False,
        },
    }
    result = build_pytest_invocation([row])
    assert result["node_ids"] == [
        "tests/test_pairs.py::test_pairs[small]",
        "tests/test_pairs.py::test_pairs[big]",
    ]
    assert result["skipped_tests"] == []


def test_runner_skips_callable_ids_but_still_expands_from_cases():
    row = {
        "canonical_name": "tests.test_callable.test_c",
        "def_file": "tests/test_callable.py",
        "parametrize": {
            "argnames": ["x"],
            "case_count": 2,
            "cases": ["1", "2"],
            "truncated": False,
            "ids": None,
            "ids_callable": True,
        },
    }
    result = build_pytest_invocation([row])
    # Node ids still come out from the literal cases; the callable is flagged
    # so the user knows they may diverge from pytest's real ids at collect time.
    assert result["node_ids"] == [
        "tests/test_callable.py::test_c[1]",
        "tests/test_callable.py::test_c[2]",
    ]
    assert any("callable" in s["reason"] for s in result["skipped_tests"])


def test_runner_falls_back_when_explicit_ids_length_mismatches():
    row = {
        "canonical_name": "tests.test_mismatch.test_m",
        "def_file": "tests/test_mismatch.py",
        "parametrize": {
            "argnames": ["x"],
            "case_count": 3,
            "cases": ["1", "2", "3"],
            "truncated": False,
            "ids": ["only-one"],  # deliberately wrong length
            "ids_callable": False,
        },
    }
    result = build_pytest_invocation([row])
    # Length mismatch → fall back to value-based ids, no skip required.
    assert result["node_ids"] == [
        "tests/test_mismatch.py::test_m[1]",
        "tests/test_mismatch.py::test_m[2]",
        "tests/test_mismatch.py::test_m[3]",
    ]
