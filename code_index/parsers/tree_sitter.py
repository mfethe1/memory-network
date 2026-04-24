"""Tree-sitter adapter.

Intentionally lazy: the import is deferred so `code_index` runs with zero
extra deps. `available()` returns False when the optional package is missing;
the registry will skip this parser and fall through to ctags/heuristic.

This is a scaffold — it wires availability detection and language dispatch,
but the actual query-based symbol extraction is v2 work. For v1, the parser
still returns a valid ParseResult via the heuristic fallback so coverage is
never worse than the text chunker.
"""

from __future__ import annotations

from code_index.parsers.base import ParseResult


def available() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_languages  # noqa: F401
    except Exception:
        return False
    return True


class TreeSitterParser:
    name = "tree-sitter"
    confidence = 0.75
    language = "multi"

    def __init__(self, languages: list[str] | None = None) -> None:
        self.languages = languages or []
        self._available = available()

    def supports(self, rel_path: str) -> bool:
        return False  # v1: disabled by default; registry skips past this.

    def parse(self, *, rel_path: str, source: str) -> ParseResult:
        return ParseResult(
            language="text",
            semantic_source="tree-sitter",
            confidence=0.0,
            parse_status="skipped",
            parse_error="tree-sitter adapter not yet implemented for v1",
        )
