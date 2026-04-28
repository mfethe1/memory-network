import textwrap

from code_index.parsers.python_ast import PythonAstParser


def test_extracts_module_class_method_function():
    src = textwrap.dedent(
        '''
        """top doc"""

        def top() -> int:
            return 1

        class Box:
            def __init__(self):
                self.x = 0
            def bump(self):
                self.x += 1
        '''
    ).lstrip()
    parser = PythonAstParser()
    result = parser.parse(rel_path="pkg/mod.py", source=src)
    assert result.parse_status == "ok"
    kinds = sorted({s.kind for s in result.symbols})
    assert kinds == ["class", "function", "method", "module"]
    qualified = {s.canonical_name for s in result.symbols}
    assert "pkg.mod.top" in qualified
    assert "pkg.mod.Box" in qualified
    assert "pkg.mod.Box.bump" in qualified
    # Module chunk + one per class/function/method (no inner blocks).
    chunk_types = sorted(c.chunk_type for c in result.chunks)
    assert "module" in chunk_types
    assert chunk_types.count("class") == 1
    assert chunk_types.count("method") == 2
    assert chunk_types.count("function") == 1


def test_symbol_uid_stable_across_reformatting():
    src1 = textwrap.dedent(
        """
        def f(x: int) -> int:
            return x + 1
        """
    ).lstrip()
    src2 = textwrap.dedent(
        """

        def f(x: int) -> int:

            return x + 1
        """
    ).lstrip()
    parser = PythonAstParser()
    a = parser.parse(rel_path="mod.py", source=src1)
    b = parser.parse(rel_path="mod.py", source=src2)
    uid_a = {s.canonical_name: s.symbol_uid for s in a.symbols}
    uid_b = {s.canonical_name: s.symbol_uid for s in b.symbols}
    assert uid_a["mod.f"] == uid_b["mod.f"]


def test_syntax_error_recorded_as_diagnostic():
    parser = PythonAstParser()
    result = parser.parse(rel_path="bad.py", source="def broken(:\n")
    assert result.parse_status == "failed"
    assert result.diagnostics
    assert result.diagnostics[0].severity == "error"


def test_repeated_local_definitions_get_distinct_chunk_uids():
    src = textwrap.dedent(
        """
        def builds_classes():
            class Inner:
                value = 1

            class Inner:
                value = 2
        """
    ).lstrip()
    parser = PythonAstParser()
    result = parser.parse(rel_path="pkg/repeated.py", source=src)
    inner_chunks = [
        chunk
        for chunk in result.chunks
        if chunk.symbol_path == "pkg.repeated.builds_classes.Inner"
    ]
    assert len(inner_chunks) == 2
    assert len({chunk.chunk_uid for chunk in inner_chunks}) == 2
