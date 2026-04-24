"""Conservative heuristic fallback: one 'file' chunk, no symbols.

Still populates FTS content + file_path, so grep and ranked retrieval work.
"""

from __future__ import annotations

from code_index.hashing import normalized_hash, raw_hash
from code_index.parsers.base import ChunkDraft, ParseResult
from code_index.symbols import make_chunk_uid

# Small, rough language inference by extension for heuristic results.
EXT_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".m": "objective-c",
    ".mm": "objective-c",
    ".dart": "dart",
    ".lua": "lua",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ps1": "powershell",
    ".md": "markdown",
    ".rst": "rst",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".less": "css",
}


def infer_language(rel_path: str) -> str:
    lower = rel_path.lower()
    for ext, lang in EXT_LANG.items():
        if lower.endswith(ext):
            return lang
    return "text"


class HeuristicParser:
    name = "heuristic"
    confidence = 0.10
    language = "text"

    def supports(self, rel_path: str) -> bool:
        return True

    def parse(self, *, rel_path: str, source: str) -> ParseResult:
        lang = infer_language(rel_path)
        if not source.strip():
            return ParseResult(
                language=lang,
                semantic_source="heuristic",
                confidence=self.confidence,
                parse_status="empty",
            )
        lines = source.splitlines()
        content = source
        symbol_path = rel_path
        chunk_uid = make_chunk_uid(
            file_path=rel_path,
            chunk_type="file",
            symbol_uid="",
            symbol_path=symbol_path,
            occurrence_index=0,
        )
        chunk = ChunkDraft(
            chunk_uid=chunk_uid,
            chunk_type="file",
            symbol_uid=None,
            symbol_name=rel_path.rsplit("/", 1)[-1],
            symbol_path=symbol_path,
            parent_symbol_path=None,
            signature=None,
            start_line=1,
            end_line=len(lines) or 1,
            start_byte=0,
            end_byte=len(source.encode("utf-8", "replace")),
            content=content,
            raw_hash=raw_hash(content),
            normalized_hash=normalized_hash(content),
            context={"parser": "heuristic"},
        )
        return ParseResult(
            language=lang,
            semantic_source="heuristic",
            confidence=self.confidence,
            parse_status="ok",
            chunks=[chunk],
        )
