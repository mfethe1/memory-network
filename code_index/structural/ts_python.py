"""Tree-sitter structural queries for Python.

This slice scopes structural search to Python per the task guidance. The
adapter lazily imports `tree_sitter` + `tree_sitter_python` so `code_index`
works without them — only `query --ast` needs these installed.

Bundled queries: small, well-known patterns agents reach for. Users can also
supply a raw tree-sitter query via `--pattern <S-expression>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


BUNDLED_QUERIES: dict[str, str] = {
    "class": "(class_definition name: (identifier) @name) @class",
    "function": ("(module (function_definition name: (identifier) @name) @function)"),
    "method": (
        "(class_definition body: (block (function_definition"
        "   name: (identifier) @name) @method))"
    ),
    "async-function": (
        '(function_definition "async" name: (identifier) @name) @async_function'
    ),
    "decorator": "(decorated_definition (decorator) @decorator)",
    "call": "(call function: (_) @callee) @call",
    "import": "(import_statement) @import",
    "import-from": "(import_from_statement) @import_from",
    "global-assignment": "(module (expression_statement (assignment) @assignment))",
    "exception-handler": "(try_statement (except_clause) @except)",
    "docstring": (
        "[(module . (expression_statement (string) @doc))"
        " (class_definition body: (block . (expression_statement (string) @doc)))"
        " (function_definition body: (block . (expression_statement (string) @doc)))]"
    ),
}


@dataclass
class TsCapture:
    file_path: str
    capture_name: str
    node_kind: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    text: str


@dataclass
class TsResult:
    query: str
    expanded_query: str
    captures: list[TsCapture]


def available() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
    except Exception:
        return False
    return True


def _unavailable_reason() -> str:
    try:
        import tree_sitter  # noqa: F401
    except Exception as exc:
        return f"tree_sitter import failed: {exc}"
    try:
        import tree_sitter_python  # noqa: F401
    except Exception as exc:
        return f"tree_sitter_python import failed: {exc}"
    return "unknown"


def expand_query(query_or_name: str) -> str:
    """Resolve a bundled query alias to its S-expression, or pass through."""
    key = query_or_name.strip()
    if key in BUNDLED_QUERIES:
        return BUNDLED_QUERIES[key]
    return query_or_name


class _PythonEngine:
    def __init__(self) -> None:
        from tree_sitter import Language, Parser
        import tree_sitter_python

        lang_ptr = tree_sitter_python.language()
        self.language = Language(lang_ptr)
        self.parser = Parser(self.language)

    def parse(self, source: bytes):
        return self.parser.parse(source)

    def run(self, *, source: bytes, query: str) -> list[tuple[str, Any]]:
        from tree_sitter import Query, QueryCursor

        tree = self.parser.parse(source)
        compiled = Query(self.language, query)
        cursor = QueryCursor(compiled)
        # tree-sitter 0.25 returns [(pattern_index, {capture_name: [node,...]}) ...]
        matches = cursor.matches(tree.root_node)
        flat: list[tuple[str, Any]] = []
        for _pattern_idx, cap_map in matches:
            for name, nodes in cap_map.items():
                for node in nodes:
                    flat.append((name, node))
        return flat


_engine: _PythonEngine | None = None


def _get_engine() -> _PythonEngine:
    global _engine
    if _engine is None:
        _engine = _PythonEngine()
    return _engine


def query_text(source: str, query_or_name: str) -> TsResult:
    """Run a structural query over a single Python source string."""
    if not available():
        raise RuntimeError(
            "tree-sitter is not installed. "
            "install with: pip install 'code-index[tree-sitter]'"
        )
    expanded = expand_query(query_or_name)
    engine = _get_engine()
    results = engine.run(source=source.encode("utf-8", "replace"), query=expanded)
    captures = []
    for name, node in results:
        captures.append(
            TsCapture(
                file_path="",
                capture_name=name,
                node_kind=node.type,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                text=node.text.decode("utf-8", "replace")
                if node.text is not None
                else "",
            )
        )
    return TsResult(query=query_or_name, expanded_query=expanded, captures=captures)


def query_files(files: list[tuple[Path, str]], query_or_name: str) -> TsResult:
    """Run a structural query over many (path, rel_path) pairs."""
    if not available():
        raise RuntimeError(
            "tree-sitter is not installed. "
            "install with: pip install 'code-index[tree-sitter]'"
        )
    expanded = expand_query(query_or_name)
    engine = _get_engine()
    all_caps: list[TsCapture] = []
    for abs_path, rel_path in files:
        try:
            source = abs_path.read_bytes()
        except OSError:
            continue
        try:
            results = engine.run(source=source, query=expanded)
        except Exception:
            # Bad file / parser glitch; skip silently, diagnostics stay in the
            # indexer path.
            continue
        for name, node in results:
            all_caps.append(
                TsCapture(
                    file_path=rel_path,
                    capture_name=name,
                    node_kind=node.type,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                    text=node.text.decode("utf-8", "replace")
                    if node.text is not None
                    else "",
                )
            )
    return TsResult(query=query_or_name, expanded_query=expanded, captures=all_caps)


def bundled_query_names() -> list[str]:
    return sorted(BUNDLED_QUERIES)


def availability_report() -> dict:
    avail = available()
    return {
        "available": avail,
        "reason": None if avail else _unavailable_reason(),
        "bundled_queries": bundled_query_names(),
    }
