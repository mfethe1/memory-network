"""Import SCIP JSON into the local semantic spine.

Initial scope: consume JSON produced by `scip print --json`. This keeps the
first integration testable without requiring protobuf bindings or the `scip`
binary. The importer is additive: it writes files, symbols, occurrences,
relations, and diagnostics, while leaving chunks as the local retrieval
projection owned by `pipeline.reindex()`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_index.config import Config
from code_index.hashing import worktree_hash
from code_index.symbols import SymbolIdentity, normalize_signature


SYMBOL_ROLE_DEFINITION = 0x1
SYMBOL_ROLE_IMPORT = 0x2
SYMBOL_ROLE_WRITE = 0x4
SYMBOL_ROLE_READ = 0x8
SYMBOL_ROLE_TEST = 0x20


@dataclass
class ScipImportStats:
    documents_seen: int = 0
    files_upserted: int = 0
    symbols_upserted: int = 0
    external_symbols_upserted: int = 0
    occurrences_inserted: int = 0
    relations_inserted: int = 0
    diagnostics_inserted: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get(obj: dict[str, Any], *names: str, default=None):
    for name in names:
        if name in obj:
            return obj[name]
    return default


def _tool_name(payload: dict[str, Any]) -> str:
    metadata = _get(payload, "metadata", default={}) or {}
    tool = _get(metadata, "toolInfo", "tool_info", default={}) or {}
    name = _get(tool, "name", default=None)
    return str(name or "scip")


def _kind(raw: Any) -> str:
    if raw is None:
        return "unknown"
    if isinstance(raw, int):
        # SCIP enum numbers are intentionally broad. Keep numeric values
        # inspectable rather than pretending they are local semantic kinds.
        return f"scip-kind-{raw}"
    value = str(raw).split(".")[-1].strip()
    if not value:
        return "unknown"
    mapping = {
        "AbstractMethod": "method",
        "Class": "class",
        "Constructor": "method",
        "Enum": "enum",
        "File": "module",
        "Function": "function",
        "Interface": "interface",
        "Method": "method",
        "Module": "module",
        "Namespace": "module",
        "Package": "module",
        "Property": "property",
        "Struct": "class",
        "Trait": "interface",
        "Type": "class",
        "Variable": "variable",
    }
    return mapping.get(value, value[:1].lower() + value[1:])


_METHOD_DESC_RE = re.compile(r"([^/#.:!\[\]()]+)\([^)]*\)\.")
_DESC_RE = re.compile(r"([^/#.:!\[\]()]+)([/#.:!])")


def _unescape_symbol_part(value: str) -> str:
    return value.replace("``", "`").replace("  ", " ")


def canonical_from_scip_symbol(symbol: str, display_name: str | None = None) -> str:
    """Best-effort display canonicalization for SCIP symbol strings.

    The raw SCIP symbol remains the source identifier in provenance/context.
    This function only makes local lookup output readable.
    """
    if not symbol:
        return display_name or "unknown"
    if symbol.startswith("local "):
        return display_name or symbol

    pieces = symbol.split(" ", 4)
    descriptors = pieces[4] if len(pieces) == 5 else symbol
    names: list[str] = []
    i = 0
    while i < len(descriptors):
        method = _METHOD_DESC_RE.match(descriptors, i)
        if method:
            names.append(_unescape_symbol_part(method.group(1)))
            i = method.end()
            continue
        desc = _DESC_RE.match(descriptors, i)
        if desc:
            raw_name, suffix = desc.groups()
            name = _unescape_symbol_part(raw_name)
            if suffix in {"/", "#", ".", ":"} and name:
                names.append(name)
            i = desc.end()
            continue
        i += 1
    if names:
        return ".".join(names)
    return display_name or symbol


def _signature(info: dict[str, Any]) -> str | None:
    sig_doc = _get(info, "signatureDocumentation", "signature_documentation")
    if isinstance(sig_doc, dict):
        text = _get(sig_doc, "text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _symbol_uid(
    *,
    language: str,
    kind: str,
    canonical_name: str,
    signature: str | None,
    container_uid: str = "",
) -> str:
    return SymbolIdentity(
        language=language,
        kind=kind,
        canonical_name=canonical_name,
        signature_norm=normalize_signature(signature),
        container_uid=container_uid,
    ).symbol_uid


def _upsert_symbol(
    conn: sqlite3.Connection,
    *,
    raw_symbol: str,
    info: dict[str, Any],
    language: str,
    source: str,
    symbol_to_uid: dict[str, str],
    symbol_to_pk: dict[str, int],
) -> int:
    display_name = _get(info, "displayName", "display_name")
    display = str(display_name) if display_name else canonical_from_scip_symbol(raw_symbol)
    kind = _kind(_get(info, "kind"))
    signature = _signature(info)
    enclosing = _get(info, "enclosingSymbol", "enclosing_symbol")
    container_uid = symbol_to_uid.get(enclosing or "", "")
    container_pk = symbol_to_pk.get(enclosing or "")
    canonical = canonical_from_scip_symbol(raw_symbol, display)
    uid = _symbol_uid(
        language=language,
        kind=kind,
        canonical_name=canonical,
        signature=signature,
        container_uid=container_uid,
    )
    now = _now_iso()
    row = conn.execute(
        "SELECT symbol_pk FROM symbols WHERE symbol_uid = ?",
        (uid,),
    ).fetchone()
    docs = _get(info, "documentation", default=[]) or []
    context_note = ""
    if docs:
        context_note = "\n".join(str(d) for d in docs if d)
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO symbols(
                symbol_uid, language, kind, canonical_name, display_name,
                container_symbol_pk, signature_norm, semantic_source,
                confidence, first_indexed_at, last_indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                language,
                kind,
                canonical,
                display,
                container_pk,
                normalize_signature(signature),
                source,
                0.98,
                now,
                now,
            ),
        )
        pk = int(cur.lastrowid)
    else:
        pk = int(row["symbol_pk"])
        conn.execute(
            """
            UPDATE symbols SET
                language = ?,
                kind = ?,
                canonical_name = ?,
                display_name = ?,
                container_symbol_pk = ?,
                signature_norm = ?,
                semantic_source = ?,
                confidence = ?,
                last_indexed_at = ?,
                deleted_at = NULL
            WHERE symbol_pk = ?
            """,
            (
                language,
                kind,
                canonical,
                display,
                container_pk,
                normalize_signature(signature),
                source,
                0.98,
                now,
                pk,
            ),
        )
    if context_note:
        conn.execute(
            """
            INSERT INTO diagnostics(
                file_pk, tool, code, severity, message, observed_at
            )
            SELECT o.file_pk, ?, 'scip-doc', 'info', ?, ?
              FROM occurrences o
             WHERE o.symbol_pk = ?
             LIMIT 1
            """,
            (source, context_note[:2000], now, pk),
        )
    symbol_to_uid[raw_symbol] = uid
    symbol_to_pk[raw_symbol] = pk
    return pk


def _range_lines(value: Any) -> tuple[int | None, int | None]:
    if not isinstance(value, list) or len(value) < 3:
        return None, None
    try:
        start_line = int(value[0]) + 1
        end_line = int(value[2]) + 1 if len(value) >= 4 else start_line
    except (TypeError, ValueError):
        return None, None
    return start_line, end_line


def _occurrence_role(raw_roles: Any) -> str:
    try:
        roles = int(raw_roles or 0)
    except (TypeError, ValueError):
        roles = 0
    if roles & SYMBOL_ROLE_DEFINITION:
        return "definition"
    if roles & SYMBOL_ROLE_IMPORT:
        return "import"
    return "reference"


def _relation_kinds(rel: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    if _get(rel, "isImplementation", "is_implementation", default=False):
        kinds.append("implements")
    if _get(rel, "isReference", "is_reference", default=False):
        kinds.append("references")
    if _get(rel, "isTypeDefinition", "is_type_definition", default=False):
        kinds.append("type_definition")
    if _get(rel, "isDefinition", "is_definition", default=False):
        kinds.append("definition")
    return kinds


def _ensure_file(
    conn: sqlite3.Connection,
    *,
    config: Config,
    rel_path: str,
    language: str,
    source: str,
    text: str | None,
) -> int:
    path = config.root / rel_path
    now = _now_iso()
    data: bytes | None = None
    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError:
            data = None
    if data is None and text is not None:
        data = text.encode("utf-8", "replace")
    size = len(data or b"")
    wth = worktree_hash(data or b"")
    mtime_ns = None
    if path.exists():
        try:
            mtime_ns = getattr(path.stat(), "st_mtime_ns", None)
        except OSError:
            mtime_ns = None
    row = conn.execute(
        "SELECT file_pk FROM files WHERE file_path = ?",
        (rel_path,),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO files(
                file_path, language, worktree_hash, size_bytes, mtime_ns,
                parse_status, semantic_source, parser_confidence, indexed_at
            ) VALUES (?, ?, ?, ?, ?, 'ok', ?, 0.98, ?)
            """,
            (rel_path, language, wth, size, mtime_ns, source, now),
        )
        return int(cur.lastrowid)
    conn.execute(
        """
        UPDATE files SET
            language = ?,
            worktree_hash = ?,
            size_bytes = ?,
            mtime_ns = ?,
            parse_status = 'ok',
            parse_error = NULL,
            semantic_source = ?,
            parser_confidence = 0.98,
            indexed_at = ?,
            deleted_at = NULL
        WHERE file_pk = ?
        """,
        (language, wth, size, mtime_ns, source, now, row["file_pk"]),
    )
    return int(row["file_pk"])


def import_scip_json(
    conn: sqlite3.Connection,
    config: Config,
    payload: dict[str, Any],
    *,
    event_source: str = "import-scip",
) -> ScipImportStats:
    stats = ScipImportStats()
    tool = _tool_name(payload)
    source = f"scip:{tool}"
    symbol_to_uid: dict[str, str] = {}
    symbol_to_pk: dict[str, int] = {}

    documents = _get(payload, "documents", default=[]) or []
    external = _get(payload, "externalSymbols", "external_symbols", default=[]) or []

    # Upsert external symbols first so relationships can point at them.
    for info in external:
        if not isinstance(info, dict):
            continue
        raw_symbol = _get(info, "symbol")
        if not raw_symbol:
            continue
        _upsert_symbol(
            conn,
            raw_symbol=str(raw_symbol),
            info=info,
            language="external",
            source=source,
            symbol_to_uid=symbol_to_uid,
            symbol_to_pk=symbol_to_pk,
        )
        stats.external_symbols_upserted += 1

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        stats.documents_seen += 1
        rel_path = _get(doc, "relativePath", "relative_path")
        if not rel_path:
            stats.errors.append("document missing relative_path")
            continue
        rel_path = str(rel_path).replace("\\", "/")
        language = str(_get(doc, "language", default="unknown") or "unknown").lower()
        file_pk = _ensure_file(
            conn,
            config=config,
            rel_path=rel_path,
            language=language,
            source=source,
            text=_get(doc, "text"),
        )
        stats.files_upserted += 1

        conn.execute(
            "DELETE FROM diagnostics WHERE file_pk = ? AND tool LIKE 'scip:%'",
            (file_pk,),
        )

        for info in _get(doc, "symbols", default=[]) or []:
            if not isinstance(info, dict):
                continue
            raw_symbol = _get(info, "symbol")
            if not raw_symbol:
                continue
            _upsert_symbol(
                conn,
                raw_symbol=str(raw_symbol),
                info=info,
                language=language,
                source=source,
                symbol_to_uid=symbol_to_uid,
                symbol_to_pk=symbol_to_pk,
            )
            stats.symbols_upserted += 1

        occurrence_rows: list[tuple[int, str, int | None, int | None, str]] = []
        occurrence_symbol_pks: set[int] = set()
        for occ in _get(doc, "occurrences", default=[]) or []:
            if not isinstance(occ, dict):
                continue
            raw_symbol = _get(occ, "symbol")
            if not raw_symbol:
                continue
            symbol_pk = symbol_to_pk.get(str(raw_symbol))
            if symbol_pk is None:
                fallback_info = {
                    "symbol": raw_symbol,
                    "kind": "Unknown",
                    "displayName": canonical_from_scip_symbol(str(raw_symbol)),
                }
                symbol_pk = _upsert_symbol(
                    conn,
                    raw_symbol=str(raw_symbol),
                    info=fallback_info,
                    language=language,
                    source=source,
                    symbol_to_uid=symbol_to_uid,
                    symbol_to_pk=symbol_to_pk,
                )
                stats.symbols_upserted += 1
            start_line, end_line = _range_lines(_get(occ, "range"))
            occurrence_symbol_pks.add(symbol_pk)
            occurrence_rows.append(
                (
                    symbol_pk,
                    _occurrence_role(_get(occ, "symbolRoles", "symbol_roles")),
                    start_line,
                    end_line,
                    str(_get(occ, "syntaxKind", "syntax_kind", default="") or ""),
                )
            )
            for diag in _get(occ, "diagnostics", default=[]) or []:
                if not isinstance(diag, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO diagnostics(
                        file_pk, tool, code, severity, start_line, end_line,
                        message, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_pk,
                        source,
                        _get(diag, "code"),
                        str(_get(diag, "severity", default="info") or "info").lower(),
                        start_line,
                        end_line,
                        str(_get(diag, "message", default="")),
                        _now_iso(),
                    ),
                )
                stats.diagnostics_inserted += 1

        if occurrence_symbol_pks:
            placeholders = ",".join("?" for _ in occurrence_symbol_pks)
            conn.execute(
                f"DELETE FROM occurrences WHERE file_pk = ? AND symbol_pk IN ({placeholders})",
                (file_pk, *sorted(occurrence_symbol_pks)),
            )
        for symbol_pk, role, start_line, end_line, syntax_kind in occurrence_rows:
            conn.execute(
                """
                INSERT INTO occurrences(
                    symbol_pk, file_pk, role, start_line, end_line, syntax_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (symbol_pk, file_pk, role, start_line, end_line, syntax_kind),
            )
            stats.occurrences_inserted += 1

    for info in [*(external or []), *[s for d in documents if isinstance(d, dict) for s in (_get(d, "symbols", default=[]) or [])]]:
        if not isinstance(info, dict):
            continue
        raw_symbol = _get(info, "symbol")
        if not raw_symbol:
            continue
        src_pk = symbol_to_pk.get(str(raw_symbol))
        if src_pk is None:
            continue
        for rel in _get(info, "relationships", default=[]) or []:
            if not isinstance(rel, dict):
                continue
            dst_raw = _get(rel, "symbol")
            dst_pk = symbol_to_pk.get(str(dst_raw))
            if dst_pk is None:
                continue
            for kind in _relation_kinds(rel):
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO relations(
                        src_symbol_pk, dst_symbol_pk, relation_kind, provenance, weight
                    ) VALUES (?, ?, ?, ?, 0.98)
                    """,
                    (src_pk, dst_pk, kind, source),
                )
                if cur.rowcount:
                    stats.relations_inserted += 1

    # Record a compact audit marker so the ingestion appears in existing
    # edit/history reports even though SCIP does not own chunk text.
    conn.execute(
        """
        INSERT INTO chunk_edits(
            timestamp, event_source, change_type, diff_summary
        ) VALUES (?, ?, 'scip-import', ?)
        """,
        (
            _now_iso(),
            event_source,
            json.dumps(
                {
                    "documents": stats.documents_seen,
                    "symbols": stats.symbols_upserted,
                    "external_symbols": stats.external_symbols_upserted,
                    "occurrences": stats.occurrences_inserted,
                    "relations": stats.relations_inserted,
                },
                sort_keys=True,
            ),
        ),
    )
    return stats


def load_scip_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("SCIP JSON root must be an object")
    return raw
