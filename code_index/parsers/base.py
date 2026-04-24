"""Parser interface shared across language adapters.

Every parser returns a ParseResult describing symbols, occurrences, relations,
chunks, and diagnostics for a single file. Upsert semantics live in the
pipeline; parsers are pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SymbolDraft:
    symbol_uid: str
    kind: str
    canonical_name: str
    display_name: str
    signature: str | None
    signature_norm: str
    container_uid: str | None
    start_line: int | None
    end_line: int | None
    start_byte: int | None
    end_byte: int | None
    confidence: float
    semantic_source: str
    language: str


@dataclass
class OccurrenceDraft:
    symbol_uid: str
    role: str  # definition | reference | import | export | alias
    start_line: int | None
    end_line: int | None
    start_byte: int | None = None
    end_byte: int | None = None
    syntax_kind: str | None = None


@dataclass
class RelationDraft:
    src_symbol_uid: str
    dst_symbol_uid: str
    relation_kind: str
    provenance: str | None = None
    weight: float = 1.0


@dataclass
class PendingRelation:
    """A relation whose destination must be resolved against the symbol table.

    Parsers emit these for cross-file edges (imports, calls) where the dst
    symbol may live in another file that has not been parsed yet. The pipeline
    resolves them after all files are parsed.
    """

    src_symbol_uid: str
    relation_kind: str
    dst_candidates: list[str]  # ordered canonical-name candidates
    provenance: str | None = None
    weight: float = 1.0
    site_line: int | None = None


@dataclass
class ChunkDraft:
    chunk_uid: str
    chunk_type: str
    symbol_uid: str | None
    symbol_name: str | None
    symbol_path: str | None
    parent_symbol_path: str | None
    signature: str | None
    start_line: int
    end_line: int
    start_byte: int | None
    end_byte: int | None
    content: str
    raw_hash: str
    normalized_hash: str
    context: dict


@dataclass
class DiagnosticDraft:
    tool: str
    severity: str
    message: str
    code: str | None = None
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class ParseResult:
    language: str
    semantic_source: str
    confidence: float
    parse_status: str  # ok | empty | failed
    parse_error: str | None = None
    symbols: list[SymbolDraft] = field(default_factory=list)
    occurrences: list[OccurrenceDraft] = field(default_factory=list)
    relations: list[RelationDraft] = field(default_factory=list)
    pending_relations: list[PendingRelation] = field(default_factory=list)
    chunks: list[ChunkDraft] = field(default_factory=list)
    diagnostics: list[DiagnosticDraft] = field(default_factory=list)


class Parser(Protocol):
    name: str
    language: str
    confidence: float

    def supports(self, rel_path: str) -> bool: ...

    def parse(self, *, rel_path: str, source: str) -> ParseResult: ...
