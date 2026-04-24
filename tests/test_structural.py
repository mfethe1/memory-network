"""Structural (tree-sitter) query tests.

These are skipped gracefully when the optional tree-sitter deps aren't
installed. The CI/dev env for this project installs them via
`pip install -e .[tree-sitter]` or directly `pip install tree-sitter tree-sitter-python`.
"""

from __future__ import annotations

import textwrap

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

from code_index.structural import ts_python


def test_bundled_query_list():
    names = ts_python.bundled_query_names()
    for expected in ("class", "function", "method", "call", "import"):
        assert expected in names


def test_class_query_captures_names():
    src = textwrap.dedent(
        """
        class Foo:
            pass

        class Bar(Foo):
            def m(self):
                pass
        """
    )
    r = ts_python.query_text(src, "class")
    names = {c.text for c in r.captures if c.capture_name == "name"}
    assert names == {"Foo", "Bar"}


def test_call_query_captures_callees():
    src = textwrap.dedent(
        """
        def run():
            helper(x)
            obj.method(y)
        """
    )
    r = ts_python.query_text(src, "call")
    callees = [c.text for c in r.captures if c.capture_name == "callee"]
    assert any(c.startswith("helper") for c in callees)
    assert any(c.startswith("obj.method") for c in callees)


def test_unknown_alias_treated_as_raw_query():
    # A syntactically valid raw query that doesn't match anything should
    # return an empty capture list rather than raise.
    src = "x = 1\n"
    r = ts_python.query_text(src, "(decorator) @d")
    assert r.captures == []


def test_bad_query_raises():
    with pytest.raises(Exception):
        ts_python.query_text("x = 1", "(not_a_real_node_type)")
