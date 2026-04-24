"""Shared init/update/watch pipeline.

Every code path that wants to reindex a set of files calls `reindex(conn,
paths, ...)`. That keeps init, update --files, and watch mode on one
deterministic upsert path.

Upsert semantics (per file, per transaction):
1. Read + hash the file; skip if worktree_hash matches and force=False.
2. Parse via registry → ParseResult.
3. Upsert files row.
4. Rewrite occurrences and diagnostics for this file (cheap, correct for v1).
5. Reconcile symbols by symbol_uid: insert new, tombstone removed.
6. Reconcile chunks by chunk_uid: insert new, tombstone removed, update when
   content/hash differs. Append a chunk_edits row for each change.
7. Insert relations (ignore conflict on unique triple).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from code_index.config import Config
from code_index.db import transaction
from code_index.hashing import worktree_hash
from code_index.ignore import IgnoreMatcher, build as build_matcher
from code_index.parsers import ParseResult, Registry, default_registry
from code_index.scanner import iter_files


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ReindexStats:
    files_seen: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    symbols_upserted: int = 0
    symbols_tombstoned: int = 0
    chunks_created: int = 0
    chunks_updated: int = 0
    chunks_tombstoned: int = 0
    edits_recorded: int = 0
    relations_inserted: int = 0
    relations_backfilled: int = 0
    relations_unresolved: int = 0
    relations_queued_for_repair: int = 0
    relations_backfill_skipped: bool = False
    relations_resolved_by_jedi: int = 0
    test_edges_inserted: int = 0
    test_edges_removed: int = 0
    test_edges_rebuilt_scope: str = "full"  # 'full' | 'scoped'
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        return data


def _resolve_paths(
    config: Config,
    matcher: IgnoreMatcher,
    paths: list[Path] | None,
) -> list[tuple[Path, str, int]]:
    """Return (absolute_path, rel_posix, size) triples to index."""
    if paths:
        out: list[tuple[Path, str, int]] = []
        for raw in paths:
            p = raw if raw.is_absolute() else (config.root / raw)
            p = p.resolve()
            if not p.exists():
                continue
            try:
                rel = p.relative_to(config.root).as_posix()
            except ValueError:
                continue
            if matcher.is_ignored(p, is_dir=p.is_dir()):
                continue
            if p.is_dir():
                for scanned in iter_files(p, matcher, max_bytes=config.max_file_bytes):
                    sub_rel = scanned.path.relative_to(config.root).as_posix()
                    out.append((scanned.path, sub_rel, scanned.size))
            else:
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size > config.max_file_bytes:
                    continue
                out.append((p, rel, size))
        return out
    return [
        (sf.path, sf.rel_path, sf.size)
        for sf in iter_files(config.root, matcher, max_bytes=config.max_file_bytes)
    ]


def _read_source(path: Path) -> tuple[str | None, bytes | None, str | None]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, None, f"read failed: {exc}"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:
            return None, data, f"decode failed: {exc}"
    return text, data, None


def _ensure_file_row(
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    language: str | None,
    wth: str | None,
    size: int,
    mtime_ns: int | None,
    parse_status: str,
    parse_error: str | None,
    semantic_source: str | None,
    parser_confidence: float | None,
    git_blob_oid: str | None = None,
    git_committed_at: int | None = None,
    git_author: str | None = None,
) -> int:
    now = _now_iso()
    row = conn.execute(
        "SELECT file_pk FROM files WHERE file_path = ?",
        (rel_path,),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO files(
                file_path, language, worktree_hash, size_bytes, mtime_ns,
                parse_status, parse_error, semantic_source, parser_confidence,
                indexed_at,
                git_blob_oid, git_committed_at, git_author
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rel_path,
                language,
                wth,
                size,
                mtime_ns,
                parse_status,
                parse_error,
                semantic_source,
                parser_confidence,
                now,
                git_blob_oid,
                git_committed_at,
                git_author,
            ),
        )
        return int(cur.lastrowid)
    conn.execute(
        """
        UPDATE files SET
            language = ?,
            worktree_hash = ?,
            size_bytes = ?,
            mtime_ns = ?,
            parse_status = ?,
            parse_error = ?,
            semantic_source = ?,
            parser_confidence = ?,
            indexed_at = ?,
            deleted_at = NULL,
            git_blob_oid = COALESCE(?, git_blob_oid),
            git_committed_at = COALESCE(?, git_committed_at),
            git_author = COALESCE(?, git_author)
        WHERE file_pk = ?
        """,
        (
            language,
            wth,
            size,
            mtime_ns,
            parse_status,
            parse_error,
            semantic_source,
            parser_confidence,
            now,
            git_blob_oid,
            git_committed_at,
            git_author,
            row["file_pk"],
        ),
    )
    return int(row["file_pk"])


def _upsert_symbol(
    conn: sqlite3.Connection,
    sym,  # SymbolDraft
    container_pk: int | None,
    now: str,
) -> int:
    row = conn.execute(
        "SELECT symbol_pk FROM symbols WHERE symbol_uid = ?",
        (sym.symbol_uid,),
    ).fetchone()
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
                sym.symbol_uid,
                sym.language,
                sym.kind,
                sym.canonical_name,
                sym.display_name,
                container_pk,
                sym.signature_norm,
                sym.semantic_source,
                sym.confidence,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)
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
            sym.language,
            sym.kind,
            sym.canonical_name,
            sym.display_name,
            container_pk,
            sym.signature_norm,
            sym.semantic_source,
            sym.confidence,
            now,
            row["symbol_pk"],
        ),
    )
    return int(row["symbol_pk"])


def _apply_parsed_file(
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    file_pk: int,
    parsed: ParseResult,
    event_source: str,
    stats: ReindexStats,
) -> None:
    now = _now_iso()

    # Reset occurrences + diagnostics for this file.
    conn.execute(
        "DELETE FROM occurrences WHERE file_pk = ?",
        (file_pk,),
    )
    conn.execute(
        "DELETE FROM diagnostics WHERE file_pk = ?",
        (file_pk,),
    )

    # Upsert symbols in order. container_uid → container_pk mapping via dict.
    uid_to_pk: dict[str, int] = {}
    for sym in parsed.symbols:
        container_pk = uid_to_pk.get(sym.container_uid or "")
        symbol_pk = _upsert_symbol(conn, sym, container_pk, now)
        uid_to_pk[sym.symbol_uid] = symbol_pk
        stats.symbols_upserted += 1

    # Insert occurrences.
    for occ in parsed.occurrences:
        symbol_pk = uid_to_pk.get(occ.symbol_uid)
        if symbol_pk is None:
            continue
        conn.execute(
            """
            INSERT INTO occurrences(
                symbol_pk, file_pk, role, start_line, end_line,
                start_byte, end_byte, syntax_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol_pk,
                file_pk,
                occ.role,
                occ.start_line,
                occ.end_line,
                occ.start_byte,
                occ.end_byte,
                occ.syntax_kind,
            ),
        )

    # Insert relations (tolerate duplicates).
    for rel in parsed.relations:
        src_pk = uid_to_pk.get(rel.src_symbol_uid)
        dst_pk = uid_to_pk.get(rel.dst_symbol_uid)
        if src_pk is None or dst_pk is None:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO relations(
                src_symbol_pk, dst_symbol_pk, relation_kind, provenance, weight
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (src_pk, dst_pk, rel.relation_kind, rel.provenance, rel.weight),
        )

    # Reconcile chunks for this file.
    existing_rows = conn.execute(
        """
        SELECT chunk_pk, chunk_uid, raw_hash, normalized_hash, deleted_at
        FROM chunks WHERE file_pk = ?
        """,
        (file_pk,),
    ).fetchall()
    existing = {row["chunk_uid"]: row for row in existing_rows}
    new_uids: set[str] = set()

    for chunk in parsed.chunks:
        new_uids.add(chunk.chunk_uid)
        primary_pk = uid_to_pk.get(chunk.symbol_uid or "")
        context_json = json.dumps(chunk.context, ensure_ascii=False, sort_keys=True)
        existing_row = existing.get(chunk.chunk_uid)
        if existing_row is None:
            cur = conn.execute(
                """
                INSERT INTO chunks(
                    chunk_uid, file_pk, file_path, language, chunk_type,
                    symbol_name, symbol_path, parent_symbol_path,
                    primary_symbol_pk, signature, start_line, end_line,
                    start_byte, end_byte, context_json, content,
                    raw_hash, normalized_hash, edit_count, last_indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    chunk.chunk_uid,
                    file_pk,
                    rel_path,
                    parsed.language,
                    chunk.chunk_type,
                    chunk.symbol_name,
                    chunk.symbol_path,
                    chunk.parent_symbol_path,
                    primary_pk,
                    chunk.signature,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.start_byte,
                    chunk.end_byte,
                    context_json,
                    chunk.content,
                    chunk.raw_hash,
                    chunk.normalized_hash,
                    now,
                ),
            )
            chunk_pk = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO chunk_edits(
                    chunk_pk, chunk_uid, symbol_uid, timestamp, event_source,
                    old_raw_hash, new_raw_hash, old_norm_hash, new_norm_hash,
                    change_type, diff_summary
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, 'create', ?)
                """,
                (
                    chunk_pk,
                    chunk.chunk_uid,
                    chunk.symbol_uid,
                    now,
                    event_source,
                    chunk.raw_hash,
                    chunk.normalized_hash,
                    f"create {chunk.chunk_type} {chunk.symbol_path or ''}",
                ),
            )
            stats.chunks_created += 1
            stats.edits_recorded += 1
            continue

        old_raw = existing_row["raw_hash"]
        old_norm = existing_row["normalized_hash"]
        tombstoned = existing_row["deleted_at"] is not None
        changed = (
            (old_raw != chunk.raw_hash)
            or (old_norm != chunk.normalized_hash)
            or tombstoned
        )
        if not changed:
            # Still refresh denormalized fields + last_indexed_at.
            conn.execute(
                """
                UPDATE chunks SET
                    file_path = ?,
                    language = ?,
                    chunk_type = ?,
                    symbol_name = ?,
                    symbol_path = ?,
                    parent_symbol_path = ?,
                    primary_symbol_pk = ?,
                    signature = ?,
                    start_line = ?,
                    end_line = ?,
                    start_byte = ?,
                    end_byte = ?,
                    context_json = ?,
                    last_indexed_at = ?,
                    deleted_at = NULL
                WHERE chunk_pk = ?
                """,
                (
                    rel_path,
                    parsed.language,
                    chunk.chunk_type,
                    chunk.symbol_name,
                    chunk.symbol_path,
                    chunk.parent_symbol_path,
                    primary_pk,
                    chunk.signature,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.start_byte,
                    chunk.end_byte,
                    context_json,
                    now,
                    existing_row["chunk_pk"],
                ),
            )
            continue

        conn.execute(
            """
            UPDATE chunks SET
                file_path = ?,
                language = ?,
                chunk_type = ?,
                symbol_name = ?,
                symbol_path = ?,
                parent_symbol_path = ?,
                primary_symbol_pk = ?,
                signature = ?,
                start_line = ?,
                end_line = ?,
                start_byte = ?,
                end_byte = ?,
                context_json = ?,
                content = ?,
                raw_hash = ?,
                normalized_hash = ?,
                edit_count = edit_count + 1,
                last_indexed_at = ?,
                deleted_at = NULL
            WHERE chunk_pk = ?
            """,
            (
                rel_path,
                parsed.language,
                chunk.chunk_type,
                chunk.symbol_name,
                chunk.symbol_path,
                chunk.parent_symbol_path,
                primary_pk,
                chunk.signature,
                chunk.start_line,
                chunk.end_line,
                chunk.start_byte,
                chunk.end_byte,
                context_json,
                chunk.content,
                chunk.raw_hash,
                chunk.normalized_hash,
                now,
                existing_row["chunk_pk"],
            ),
        )
        # Invalidate any embeddings keyed to this chunk_pk — the content we
        # embedded is now stale. Every (provider, model) row must go because
        # the chunk text changed and all stored vectors reference it.
        # `populate()` will re-embed lazily on its next pass. See
        # plans/slice-10-adversarial-review-fixes.md Task A.
        conn.execute(
            "DELETE FROM embeddings WHERE chunk_pk = ?",
            (existing_row["chunk_pk"],),
        )
        conn.execute(
            """
            INSERT INTO chunk_edits(
                chunk_pk, chunk_uid, symbol_uid, timestamp, event_source,
                old_raw_hash, new_raw_hash, old_norm_hash, new_norm_hash,
                change_type, diff_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'update', ?)
            """,
            (
                existing_row["chunk_pk"],
                chunk.chunk_uid,
                chunk.symbol_uid,
                now,
                event_source,
                old_raw,
                chunk.raw_hash,
                old_norm,
                chunk.normalized_hash,
                f"update {chunk.chunk_type} {chunk.symbol_path or ''}",
            ),
        )
        stats.chunks_updated += 1
        stats.edits_recorded += 1

    # Tombstone chunks that disappeared from this file's parse result.
    for uid, row in existing.items():
        if uid in new_uids:
            continue
        if row["deleted_at"] is not None:
            continue
        conn.execute(
            "UPDATE chunks SET deleted_at = ? WHERE chunk_pk = ?",
            (now, row["chunk_pk"]),
        )
        conn.execute(
            """
            INSERT INTO chunk_edits(
                chunk_pk, chunk_uid, timestamp, event_source,
                old_raw_hash, new_raw_hash, change_type, diff_summary
            ) VALUES (?, ?, ?, ?, ?, NULL, 'delete', ?)
            """,
            (
                row["chunk_pk"],
                uid,
                now,
                event_source,
                row["raw_hash"],
                "chunk disappeared from file",
            ),
        )
        stats.chunks_tombstoned += 1
        stats.edits_recorded += 1

    # Tombstone symbols whose only definition was in this file and no longer
    # appear. Conservative: only tombstone symbols with zero remaining
    # occurrences after this file's rewrite.
    defined_pks = {
        uid_to_pk[s.symbol_uid] for s in parsed.symbols if s.symbol_uid in uid_to_pk
    }
    orphans = conn.execute(
        """
        SELECT s.symbol_pk FROM symbols s
        WHERE s.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM occurrences o
              WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
          )
        """
    ).fetchall()
    for row in orphans:
        if row["symbol_pk"] in defined_pks:
            continue
        conn.execute(
            "UPDATE symbols SET deleted_at = ? WHERE symbol_pk = ?",
            (now, row["symbol_pk"]),
        )
        stats.symbols_tombstoned += 1

    # Insert diagnostics.
    for diag in parsed.diagnostics:
        conn.execute(
            """
            INSERT INTO diagnostics(
                file_pk, tool, code, severity, start_line, end_line,
                message, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_pk,
                diag.tool,
                diag.code,
                diag.severity,
                diag.start_line,
                diag.end_line,
                diag.message,
                now,
            ),
        )


def _record_diagnostics_only(
    conn: sqlite3.Connection,
    *,
    file_pk: int,
    parsed: ParseResult,
) -> None:
    """Insert diagnostics from a parse that otherwise produced no symbols/chunks."""
    now = _now_iso()
    conn.execute("DELETE FROM diagnostics WHERE file_pk = ?", (file_pk,))
    for diag in parsed.diagnostics:
        conn.execute(
            """
            INSERT INTO diagnostics(
                file_pk, tool, code, severity, start_line, end_line,
                message, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_pk,
                diag.tool,
                diag.code,
                diag.severity,
                diag.start_line,
                diag.end_line,
                diag.message,
                now,
            ),
        )


def _build_reexport_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Build `alias_canonical_name → target_canonical_name` map from every
    `__init__.py`'s `ImportFrom` statements (which live in the module chunk's
    `context_json.imports` list, captured by the Python AST parser).

    Scope:
    - Only __init__.py files contribute (`from X import Y` in regular modules
      binds names locally but does NOT re-export them).
    - Absolute, relative, and `as` forms are all handled. Star imports are
      skipped (the target surface is unbounded).
    - Map entries are normalized: we prefer the most-specific target the
      parser already resolved in `imports[i]["module"]`.
    """
    out: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT c.symbol_path, c.context_json
          FROM chunks c
          JOIN files f ON f.file_pk = c.file_pk
         WHERE c.chunk_type = 'module'
           AND c.deleted_at IS NULL
           AND f.file_path LIKE '%/__init__.py'
           AND c.language = 'python'
        """
    ).fetchall()
    import json as _json

    for row in rows:
        pkg = row["symbol_path"] or ""
        if not pkg:
            continue
        try:
            ctx = _json.loads(row["context_json"] or "{}")
        except Exception:
            continue
        imports = ctx.get("imports") or []
        for imp in imports:
            if imp.get("kind") != "import_from":
                continue
            base = imp.get("module") or ""
            name = imp.get("name") or ""
            if not base or not name or name == "*":
                continue
            asname = imp.get("asname") or name
            # `from .sub import Foo [as Bar]` in pkg/__init__.py ⇒
            # `pkg.Bar` re-exports `pkg.sub.Foo` (or just the base name if
            # the target isn't a symbol we know yet — both are worth storing,
            # the backfill resolver will try the longer form first).
            alias_canonical = f"{pkg}.{asname}"
            target_canonical = f"{base}.{name}"
            out[alias_canonical] = target_canonical
    return out


def _try_resolve_candidates(
    conn: sqlite3.Connection,
    candidates: list[str],
    *,
    reexport_map: dict[str, str] | None = None,
) -> int | None:
    """Return the symbol_pk of the best-matching candidate, or None.

    Resolution tiers (first match wins):
      1. Exact `canonical_name` match among live symbols.
      2. Re-export tier: if the candidate is an alias in `reexport_map`,
         resolve the target canonical name.
      3. Suffix match `%.candidate` for partially-qualified targets.
    """
    for cand in candidates:
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE canonical_name = ? AND deleted_at IS NULL LIMIT 1",
            (cand,),
        ).fetchone()
        if row:
            return int(row["symbol_pk"])
        if reexport_map:
            # Walk the re-export chain (up to 5 hops to avoid cycles).
            seen: set[str] = {cand}
            current = cand
            for _ in range(5):
                nxt = reexport_map.get(current)
                if nxt is None or nxt in seen:
                    break
                seen.add(nxt)
                row = conn.execute(
                    "SELECT symbol_pk FROM symbols WHERE canonical_name = ? AND deleted_at IS NULL LIMIT 1",
                    (nxt,),
                ).fetchone()
                if row:
                    return int(row["symbol_pk"])
                current = nxt
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE canonical_name LIKE ? AND deleted_at IS NULL ORDER BY LENGTH(canonical_name) ASC LIMIT 1",
            (f"%.{cand}",),
        ).fetchone()
        if row:
            return int(row["symbol_pk"])
    return None


def _match_exact_or_reexport(
    conn: sqlite3.Connection,
    candidates: list[str],
    *,
    reexport_map: dict[str, str] | None = None,
) -> int | None:
    """Exact canonical_name + re-export chain, WITHOUT the suffix fallback.

    Used to split the resolver so Jedi's precise answer can slot in
    between the exact-match tier and the suffix fallback.
    """
    for cand in candidates:
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE canonical_name = ? AND deleted_at IS NULL LIMIT 1",
            (cand,),
        ).fetchone()
        if row:
            return int(row["symbol_pk"])
        if reexport_map:
            seen: set[str] = {cand}
            current = cand
            for _ in range(5):
                nxt = reexport_map.get(current)
                if nxt is None or nxt in seen:
                    break
                seen.add(nxt)
                row = conn.execute(
                    "SELECT symbol_pk FROM symbols WHERE canonical_name = ? AND deleted_at IS NULL LIMIT 1",
                    (nxt,),
                ).fetchone()
                if row:
                    return int(row["symbol_pk"])
                current = nxt
    return None


def _match_suffix(
    conn: sqlite3.Connection,
    candidates: list[str],
) -> int | None:
    """Suffix match `%.candidate` — the speculative fallback."""
    for cand in candidates:
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE canonical_name LIKE ? AND deleted_at IS NULL ORDER BY LENGTH(canonical_name) ASC LIMIT 1",
            (f"%.{cand}",),
        ).fetchone()
        if row:
            return int(row["symbol_pk"])
    return None


def _try_resolve_with_jedi(
    conn: sqlite3.Connection,
    candidates: list[str],
    *,
    reexport_map: dict[str, str] | None = None,
    jedi_candidates: list[str] | None = None,
) -> tuple[int | None, bool]:
    """Resolve with Jedi wedged between exact match and suffix fallback.

    Tier order:
      1. Exact / re-export on AST candidates.
      2. Exact on Jedi candidates (Jedi already returns a fully-qualified
         name, so suffix match would just re-introduce wrong-edge risk).
      3. Suffix fallback on AST candidates.

    Returns ``(symbol_pk, used_jedi)``. ``used_jedi`` is True iff tier 2 hit.
    """
    pk = _match_exact_or_reexport(conn, candidates, reexport_map=reexport_map)
    if pk is not None:
        return pk, False
    if jedi_candidates:
        pk = _match_exact_or_reexport(conn, jedi_candidates, reexport_map=reexport_map)
        if pk is not None:
            return pk, True
    pk = _match_suffix(conn, candidates)
    return pk, False


def _maybe_jedi_resolve(
    config: Config | None,
    conn: sqlite3.Connection,
    records: list[dict],
) -> dict[tuple[str, int, int], list[str]]:
    """Call Jedi if enabled + available. Swallow any import/resolver error."""
    if config is None:
        return {}
    if not getattr(config, "enable_jedi", False):
        return {}
    try:
        from code_index.parsers.jedi_enhanced import (
            is_available,
            resolve_pending_via_jedi,
        )
    except Exception:
        return {}
    if not is_available():
        return {}
    if not records:
        return {}
    try:
        return resolve_pending_via_jedi(config, conn, records)
    except Exception:
        return {}


def _insert_relation(
    conn: sqlite3.Connection,
    *,
    src_pk: int,
    dst_pk: int,
    relation_kind: str,
    provenance: str | None,
    weight: float = 1.0,
) -> bool:
    if dst_pk == src_pk:
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO relations(
            src_symbol_pk, dst_symbol_pk, relation_kind, provenance, weight
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (src_pk, dst_pk, relation_kind, provenance, weight),
    )
    return bool(cur.rowcount)


def _insert_call_reference_occurrence(
    conn: sqlite3.Connection,
    *,
    src_pk: int,
    dst_pk: int,
    site_line: int | None,
) -> None:
    src_def = conn.execute(
        """
        SELECT file_pk
          FROM occurrences
         WHERE symbol_pk = ? AND role = 'definition'
         ORDER BY start_line ASC
         LIMIT 1
        """,
        (src_pk,),
    ).fetchone()
    if src_def is None:
        return
    file_pk = int(src_def["file_pk"])
    conn.execute(
        """
        INSERT INTO occurrences(
            symbol_pk, file_pk, role, start_line, end_line,
            start_byte, end_byte, syntax_kind
        )
        SELECT ?, ?, 'reference', ?, ?, NULL, NULL, 'call'
         WHERE NOT EXISTS (
            SELECT 1
              FROM occurrences
             WHERE symbol_pk = ?
               AND file_pk = ?
               AND role = 'reference'
               AND start_line IS ?
               AND end_line IS ?
               AND syntax_kind = 'call'
         )
        """,
        (
            dst_pk,
            file_pk,
            site_line,
            site_line,
            dst_pk,
            file_pk,
            site_line,
            site_line,
        ),
    )


def _resolve_pending(
    conn: sqlite3.Connection,
    pending: list,  # list[tuple[int, PendingRelation]]
    stats: ReindexStats,
    now: str,
    reexport_map: dict[str, str] | None = None,
    config: Config | None = None,
    jedi_touched_files: set[int] | None = None,
) -> None:
    """Resolve pending relations from the current parse.

    Strategy per candidate list (ordered best-first):
      1. Exact canonical_name match among live symbols.
      2. Re-export tier: resolve through `pkg.__init__.py` re-exports.
      3. Jedi goto (if `config.enable_jedi` and Jedi is installed) —
         runs BEFORE the suffix-match fallback so a precise type-inferred
         target wins over a speculative `%.name` match.
      4. Suffix match `%.candidate` (for partially-qualified targets).
    Unresolved candidates are persisted into `unresolved_calls` so later
    reindex runs can backfill them once the missing symbol is indexed.
    """
    import json as _json

    jedi_map = _maybe_jedi_resolve(
        config,
        conn,
        [
            {
                "src_symbol_uid": rel.src_symbol_uid,
                "file_pk": file_pk,
                "line": rel.site_line,
                "column": None,
            }
            for file_pk, rel in pending
            if rel.relation_kind == "calls" and rel.site_line is not None
        ],
    )

    for file_pk, rel in pending:
        src_row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE symbol_uid = ? AND deleted_at IS NULL",
            (rel.src_symbol_uid,),
        ).fetchone()
        if src_row is None:
            stats.relations_unresolved += 1
            continue
        src_pk = int(src_row["symbol_pk"])

        dst_pk, used_jedi = _try_resolve_with_jedi(
            conn,
            rel.dst_candidates,
            reexport_map=reexport_map,
            jedi_candidates=jedi_map.get((rel.src_symbol_uid, file_pk, rel.site_line)),
        )
        if dst_pk is None or dst_pk == src_pk:
            stats.relations_unresolved += 1
            conn.execute(
                """
                INSERT INTO unresolved_calls(
                    file_pk, src_symbol_uid, relation_kind,
                    dst_candidates_json, site_line, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_pk,
                    rel.src_symbol_uid,
                    rel.relation_kind,
                    _json.dumps(list(rel.dst_candidates)),
                    rel.site_line,
                    rel.provenance,
                    now,
                ),
            )
            continue

        provenance = rel.provenance or ""
        if rel.site_line is not None:
            provenance = f"{provenance};line={rel.site_line}".lstrip(";")
        if used_jedi:
            provenance = f"{provenance};jedi:goto".lstrip(";")
        if _insert_relation(
            conn,
            src_pk=src_pk,
            dst_pk=dst_pk,
            relation_kind=rel.relation_kind,
            provenance=provenance,
            weight=0.9 if used_jedi else rel.weight,
        ):
            stats.relations_inserted += 1
            if used_jedi:
                stats.relations_resolved_by_jedi += 1
                if jedi_touched_files is not None:
                    jedi_touched_files.add(int(file_pk))
            if rel.relation_kind == "calls":
                _insert_call_reference_occurrence(
                    conn,
                    src_pk=src_pk,
                    dst_pk=dst_pk,
                    site_line=rel.site_line,
                )


def _backfill_unresolved(
    conn: sqlite3.Connection,
    stats: ReindexStats,
    now: str,
    reexport_map: dict[str, str] | None = None,
    config: Config | None = None,
    jedi_touched_files: set[int] | None = None,
) -> None:
    """Re-attempt every still-unresolved pending relation in the DB.

    Cheap because the unresolved_calls table is small and each row is a
    handful of indexed canonical-name lookups. Rows only advance to
    `resolved_at = now` when an edge actually lands; otherwise they stay
    open so a later reindex can retry when the missing symbol appears.
    """
    import json as _json

    rows = conn.execute(
        """
        SELECT unresolved_pk, file_pk, src_symbol_uid, relation_kind,
               dst_candidates_json, site_line, provenance
          FROM unresolved_calls
         WHERE resolved_at IS NULL
        """
    ).fetchall()

    # Build one Jedi lookup over every calls-row with a known file+line.
    jedi_records: list[dict] = []
    for row in rows:
        if (
            row["relation_kind"] == "calls"
            and row["file_pk"] is not None
            and row["site_line"] is not None
        ):
            jedi_records.append(
                {
                    "src_symbol_uid": row["src_symbol_uid"],
                    "file_pk": int(row["file_pk"]),
                    "line": int(row["site_line"]),
                    "column": None,
                }
            )
    jedi_map = _maybe_jedi_resolve(config, conn, jedi_records)

    for row in rows:
        try:
            candidates = _json.loads(row["dst_candidates_json"] or "[]")
        except Exception:
            candidates = []
        src_row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE symbol_uid = ? AND deleted_at IS NULL",
            (row["src_symbol_uid"],),
        ).fetchone()
        if src_row is None:
            # src itself is gone — drop the retry; there's nothing to resolve.
            conn.execute(
                "UPDATE unresolved_calls SET resolved_at = ? WHERE unresolved_pk = ?",
                (now, row["unresolved_pk"]),
            )
            continue
        src_pk = int(src_row["symbol_pk"])

        jedi_cands: list[str] | None = None
        if row["file_pk"] is not None and row["site_line"] is not None:
            jedi_cands = jedi_map.get(
                (
                    row["src_symbol_uid"],
                    int(row["file_pk"]),
                    int(row["site_line"]),
                )
            )

        dst_pk, used_jedi = _try_resolve_with_jedi(
            conn,
            list(candidates),
            reexport_map=reexport_map,
            jedi_candidates=jedi_cands,
        )
        if dst_pk is None or dst_pk == src_pk:
            # No resolution yet. Leave the row open so a later reindex retries.
            continue
        provenance = row["provenance"] or ""
        if row["site_line"] is not None:
            provenance = f"{provenance};line={row['site_line']};backfill".lstrip(";")
        if used_jedi:
            provenance = f"{provenance};jedi:goto".lstrip(";")
        if _insert_relation(
            conn,
            src_pk=src_pk,
            dst_pk=dst_pk,
            relation_kind=row["relation_kind"],
            provenance=provenance,
            weight=0.9 if used_jedi else 1.0,
        ):
            stats.relations_inserted += 1
            if used_jedi:
                stats.relations_resolved_by_jedi += 1
                if jedi_touched_files is not None and row["file_pk"] is not None:
                    jedi_touched_files.add(int(row["file_pk"]))
            if row["relation_kind"] == "calls":
                _insert_call_reference_occurrence(
                    conn,
                    src_pk=src_pk,
                    dst_pk=dst_pk,
                    site_line=row["site_line"],
                )
            stats.relations_backfilled += 1
            if stats.relations_unresolved > 0:
                stats.relations_unresolved -= 1
            conn.execute(
                "UPDATE unresolved_calls SET resolved_at = ? WHERE unresolved_pk = ?",
                (now, row["unresolved_pk"]),
            )


def _repair_dead_edges(
    conn: sqlite3.Connection,
    stats: ReindexStats,
    now: str,
) -> None:
    """Edges whose dst_symbol tombstoned become candidates for repair.

    For each live relation pointing at a tombstoned symbol we:
      1. Emit an `unresolved_calls` row carrying the tombstoned dst's
         canonical_name + display_name as candidates (with a repair
         provenance tag).
      2. Delete the dead relation.

    The backfill pass then retries on every subsequent reindex. If a symbol
    with a matching canonical_name appears (e.g. a move into a new file
    preserving the last segment), the edge heals. Bounded: this does not
    infer arbitrary renames — if `helper` is renamed to `run`, no candidate
    matches and the row stays open until some file reintroduces `helper`.
    """
    import json as _json

    rows = conn.execute(
        """
        SELECT r.relation_pk, r.src_symbol_pk, r.relation_kind, r.provenance,
               src_s.symbol_uid AS src_uid,
               dst_s.canonical_name AS dst_canon,
               dst_s.display_name AS dst_display
          FROM relations r
          JOIN symbols src_s ON src_s.symbol_pk = r.src_symbol_pk
          JOIN symbols dst_s ON dst_s.symbol_pk = r.dst_symbol_pk
         WHERE dst_s.deleted_at IS NOT NULL
           AND src_s.deleted_at IS NULL
        """
    ).fetchall()
    repaired_to_queue = 0
    for row in rows:
        # Extract a site_line hint from the stored provenance (format
        # "...;line=N;..."). Cheap best-effort; None when absent.
        site_line: int | None = None
        provenance = row["provenance"] or ""
        for part in provenance.split(";"):
            if part.startswith("line="):
                try:
                    site_line = int(part.split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
                break
        candidates = [row["dst_canon"]]
        if row["dst_display"] and row["dst_display"] != row["dst_canon"]:
            candidates.append(row["dst_display"])
        conn.execute(
            """
            INSERT INTO unresolved_calls(
                file_pk, src_symbol_uid, relation_kind,
                dst_candidates_json, site_line, provenance, created_at
            ) VALUES (
                (SELECT o.file_pk FROM occurrences o
                  WHERE o.symbol_pk = ? AND o.role = 'definition'
                  ORDER BY o.start_line ASC LIMIT 1),
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["src_symbol_pk"],
                row["src_uid"],
                row["relation_kind"],
                _json.dumps(candidates),
                site_line,
                (provenance + ";repair:dst-tombstoned").lstrip(";"),
                now,
            ),
        )
        conn.execute(
            "DELETE FROM relations WHERE relation_pk = ?",
            (row["relation_pk"],),
        )
        repaired_to_queue += 1
    stats.relations_queued_for_repair = repaired_to_queue


_TEST_FILE_PATTERNS = ("tests/", "/tests/", "test_", "_test.py", "conftest")


def _is_test_path(path: str) -> bool:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py":
        return True
    if "/tests/" in "/" + low or low.startswith("tests/"):
        return True
    return False


def _collect_test_targets_for(
    conn: sqlite3.Connection,
    test_pk: int,
    test_name: str,
    *,
    max_depth: int,
    def_file_cache: dict[int, str | None],
) -> dict[int, tuple[int, list[str]]]:
    """BFS outward from one test symbol. Returns {dst_pk: (depth, path_names)}."""

    def _def_file(sym_pk: int) -> str | None:
        if sym_pk in def_file_cache:
            return def_file_cache[sym_pk]
        row = conn.execute(
            """
            SELECT f.file_path FROM occurrences o
              JOIN files f ON f.file_pk = o.file_pk
             WHERE o.symbol_pk = ? AND o.role = 'definition'
             ORDER BY o.start_line ASC LIMIT 1
            """,
            (sym_pk,),
        ).fetchone()
        val = row["file_path"] if row else None
        def_file_cache[sym_pk] = val
        return val

    frontier: list[tuple[int, int, list[str]]] = [(test_pk, 0, [test_name])]
    seen: dict[int, int] = {test_pk: 0}
    best_targets: dict[int, tuple[int, list[str]]] = {}

    while frontier:
        sym_pk, depth, path_names = frontier.pop(0)
        if depth >= max_depth:
            continue
        out = conn.execute(
            """
            SELECT r.dst_symbol_pk, s.canonical_name
              FROM relations r
              JOIN symbols s ON s.symbol_pk = r.dst_symbol_pk
             WHERE r.src_symbol_pk = ?
               AND r.relation_kind = 'calls'
               AND s.deleted_at IS NULL
            """,
            (sym_pk,),
        ).fetchall()
        if depth == 0:
            contains = conn.execute(
                """
                SELECT r.dst_symbol_pk, s.canonical_name
                  FROM relations r
                  JOIN symbols s ON s.symbol_pk = r.dst_symbol_pk
                 WHERE r.src_symbol_pk = ?
                   AND r.relation_kind = 'contains'
                   AND s.deleted_at IS NULL
                """,
                (sym_pk,),
            ).fetchall()
            out = list(out) + list(contains)
        for row in out:
            dst_pk = int(row["dst_symbol_pk"])
            dst_name = row["canonical_name"]
            new_depth = depth + 1
            prior = seen.get(dst_pk)
            if prior is not None and prior <= new_depth:
                continue
            seen[dst_pk] = new_depth
            new_path = path_names + [dst_name]
            df = _def_file(dst_pk)
            if df is not None and not _is_test_path(df):
                prev = best_targets.get(dst_pk)
                if prev is None or new_depth < prev[0]:
                    best_targets[dst_pk] = (new_depth, new_path)
            frontier.append((dst_pk, new_depth, new_path))

    return best_targets


def _insert_test_edges_for(
    conn: sqlite3.Connection,
    *,
    test_chunk_pk: int,
    test_pk: int,
    best_targets: dict[int, tuple[int, list[str]]],
) -> int:
    import json as _json

    inserted = 0
    for dst_pk, (depth, path_names) in best_targets.items():
        edge_type = "direct" if depth == 1 else "transitive"
        confidence = max(0.3, 1.0 - 0.15 * (depth - 1))
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO test_edges(
                test_chunk_pk, test_symbol_pk, target_symbol_pk,
                edge_type, depth, confidence, path_json, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pipeline:bfs')
            """,
            (
                test_chunk_pk,
                test_pk,
                dst_pk,
                edge_type,
                depth,
                confidence,
                _json.dumps(path_names),
            ),
        )
        if cur.rowcount:
            inserted += 1
    return inserted


def _rebuild_test_edges_scoped(
    conn: sqlite3.Connection,
    stats: ReindexStats,
    touched_file_pks: set[int],
    *,
    max_depth: int = 4,
) -> None:
    """Scoped rebuild: recompute test_edges only for test symbols whose
    definition OR whose existing edge target lives in a touched file.

    Correctness: a test T's reachability to some target X can change only if
    (a) T's own body changed — T's file was touched, or (b) something on
    T's reachability path changed — the edge's target file was touched.
    Test symbols that can't satisfy either condition keep their edges.
    """
    if not touched_file_pks:
        stats.test_edges_rebuilt_scope = "scoped"
        return
    placeholders = ",".join("?" for _ in touched_file_pks)
    file_pk_params = list(touched_file_pks)

    # Test symbols whose definition is in a touched file.
    defn_tests = conn.execute(
        f"""
        SELECT DISTINCT s.symbol_pk, s.canonical_name
          FROM symbols s
          JOIN occurrences o ON o.symbol_pk = s.symbol_pk
          JOIN files f ON f.file_pk = o.file_pk
         WHERE o.role = 'definition'
           AND s.deleted_at IS NULL
           AND f.deleted_at IS NULL
           AND o.file_pk IN ({placeholders})
        """,
        file_pk_params,
    ).fetchall()

    impacted: dict[int, str] = {}
    for r in defn_tests:
        fp = conn.execute(
            """
            SELECT f.file_path FROM occurrences o
              JOIN files f ON f.file_pk = o.file_pk
             WHERE o.symbol_pk = ? AND o.role = 'definition'
             ORDER BY o.start_line ASC LIMIT 1
            """,
            (r["symbol_pk"],),
        ).fetchone()
        if fp and _is_test_path(fp["file_path"]):
            impacted[int(r["symbol_pk"])] = r["canonical_name"]

    # Test symbols whose existing edges target a symbol defined in a touched file.
    target_tests = conn.execute(
        f"""
        SELECT DISTINCT te.test_symbol_pk, s.canonical_name
          FROM test_edges te
          JOIN symbols s ON s.symbol_pk = te.test_symbol_pk
          JOIN occurrences o ON o.symbol_pk = te.target_symbol_pk
         WHERE o.role = 'definition'
           AND o.file_pk IN ({placeholders})
           AND s.deleted_at IS NULL
        """,
        file_pk_params,
    ).fetchall()
    for r in target_tests:
        impacted.setdefault(int(r["test_symbol_pk"]), r["canonical_name"])

    if not impacted:
        stats.test_edges_rebuilt_scope = "scoped"
        return

    # Delete outbound edges for impacted test symbols.
    impacted_placeholders = ",".join("?" for _ in impacted)
    before = conn.execute(
        f"SELECT COUNT(*) FROM test_edges WHERE test_symbol_pk IN ({impacted_placeholders})",
        list(impacted),
    ).fetchone()[0]
    conn.execute(
        f"DELETE FROM test_edges WHERE test_symbol_pk IN ({impacted_placeholders})",
        list(impacted),
    )

    def_file_cache: dict[int, str | None] = {}
    inserted = 0
    for test_pk, test_name in impacted.items():
        chunk_row = conn.execute(
            "SELECT chunk_pk FROM chunks WHERE primary_symbol_pk = ? AND deleted_at IS NULL LIMIT 1",
            (test_pk,),
        ).fetchone()
        if chunk_row is None:
            continue
        targets = _collect_test_targets_for(
            conn,
            test_pk,
            test_name,
            max_depth=max_depth,
            def_file_cache=def_file_cache,
        )
        inserted += _insert_test_edges_for(
            conn,
            test_chunk_pk=int(chunk_row["chunk_pk"]),
            test_pk=test_pk,
            best_targets=targets,
        )

    stats.test_edges_inserted = inserted
    stats.test_edges_removed = before
    stats.test_edges_rebuilt_scope = "scoped"


def _rebuild_test_edges(
    conn: sqlite3.Connection,
    stats: ReindexStats,
    *,
    max_depth: int = 4,
) -> None:
    """Populate test_edges from the current relation graph.

    For each test symbol, BFS outward along `calls` edges up to `max_depth`
    hops. Any target whose own definition is in a non-test file becomes an
    edge, with `edge_type='direct'` at depth=1 and `edge_type='transitive'`
    beyond. `path_json` stores the ordered canonical-name chain from the
    test symbol to the target so downstream tools can render rationale.

    We also include depth=1 edges reached via `contains` from the test
    module → test function, so module-level targets of test funcs still
    register against the module symbol in DB-level queries.
    """
    import json as _json

    before = conn.execute("SELECT COUNT(*) FROM test_edges").fetchone()[0]
    conn.execute("DELETE FROM test_edges")

    # Collect candidate test symbols: definitions in test-looking files.
    test_defs = conn.execute(
        """
        SELECT DISTINCT s.symbol_pk, s.canonical_name, f.file_path
          FROM symbols s
          JOIN occurrences o ON o.symbol_pk = s.symbol_pk
          JOIN files f ON f.file_pk = o.file_pk
         WHERE o.role = 'definition'
           AND s.deleted_at IS NULL
           AND f.deleted_at IS NULL
        """
    ).fetchall()

    test_rows = [r for r in test_defs if _is_test_path(r["file_path"])]
    if not test_rows:
        return

    # Cache: def file per symbol_pk (to skip test-only targets).
    def_file_cache: dict[int, str | None] = {}

    def _def_file(sym_pk: int) -> str | None:
        if sym_pk in def_file_cache:
            return def_file_cache[sym_pk]
        row = conn.execute(
            """
            SELECT f.file_path FROM occurrences o
              JOIN files f ON f.file_pk = o.file_pk
             WHERE o.symbol_pk = ? AND o.role = 'definition'
             ORDER BY o.start_line ASC LIMIT 1
            """,
            (sym_pk,),
        ).fetchone()
        val = row["file_path"] if row else None
        def_file_cache[sym_pk] = val
        return val

    inserted = 0
    for test_row in test_rows:
        test_pk = int(test_row["symbol_pk"])
        test_name = test_row["canonical_name"]
        # We need a chunk for the FK; pick the test symbol's own chunk if any.
        chunk_row = conn.execute(
            "SELECT chunk_pk FROM chunks WHERE primary_symbol_pk = ? AND deleted_at IS NULL LIMIT 1",
            (test_pk,),
        ).fetchone()
        if chunk_row is None:
            continue
        test_chunk_pk = int(chunk_row["chunk_pk"])

        # BFS: (sym_pk, depth, path_uids_list, path_names_list)
        # Start by emitting a depth-1 edge for every `contains` child of this
        # test symbol (covers test modules that contain test functions).
        # More importantly, BFS outward on `calls` up to max_depth.
        frontier: list[tuple[int, int, list[str]]] = [(test_pk, 0, [test_name])]
        seen: dict[int, int] = {test_pk: 0}  # sym_pk → best depth
        best_targets: dict[int, tuple[int, list[str]]] = {}

        while frontier:
            sym_pk, depth, path_names = frontier.pop(0)
            if depth >= max_depth:
                continue
            out = conn.execute(
                """
                SELECT r.dst_symbol_pk, s.canonical_name
                  FROM relations r
                  JOIN symbols s ON s.symbol_pk = r.dst_symbol_pk
                 WHERE r.src_symbol_pk = ?
                   AND r.relation_kind = 'calls'
                   AND s.deleted_at IS NULL
                """,
                (sym_pk,),
            ).fetchall()
            # Also descend into contains at depth 0 (test module → test func).
            if depth == 0:
                contains = conn.execute(
                    """
                    SELECT r.dst_symbol_pk, s.canonical_name
                      FROM relations r
                      JOIN symbols s ON s.symbol_pk = r.dst_symbol_pk
                     WHERE r.src_symbol_pk = ?
                       AND r.relation_kind = 'contains'
                       AND s.deleted_at IS NULL
                    """,
                    (sym_pk,),
                ).fetchall()
                out = list(out) + list(contains)
            for row in out:
                dst_pk = int(row["dst_symbol_pk"])
                dst_name = row["canonical_name"]
                new_depth = depth + 1
                prior = seen.get(dst_pk)
                if prior is not None and prior <= new_depth:
                    continue
                seen[dst_pk] = new_depth
                new_path = path_names + [dst_name]
                # Consider it a test target if its def lives outside tests.
                df = _def_file(dst_pk)
                if df is not None and not _is_test_path(df):
                    # Keep the shallowest reach.
                    prev = best_targets.get(dst_pk)
                    if prev is None or new_depth < prev[0]:
                        best_targets[dst_pk] = (new_depth, new_path)
                # Always enqueue further traversal (tests may route through
                # test helpers before reaching the subject under test).
                frontier.append((dst_pk, new_depth, new_path))

        for dst_pk, (depth, path_names) in best_targets.items():
            edge_type = "direct" if depth == 1 else "transitive"
            confidence = max(0.3, 1.0 - 0.15 * (depth - 1))
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO test_edges(
                    test_chunk_pk, test_symbol_pk, target_symbol_pk,
                    edge_type, depth, confidence, path_json, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pipeline:bfs')
                """,
                (
                    test_chunk_pk,
                    test_pk,
                    dst_pk,
                    edge_type,
                    depth,
                    confidence,
                    _json.dumps(path_names),
                ),
            )
            if cur.rowcount:
                inserted += 1

    stats.test_edges_inserted = inserted
    stats.test_edges_removed = before


def _clear_relations_for_file(conn: sqlite3.Connection, file_pk: int) -> None:
    """Delete `calls`, `imports`, `inherits` edges whose source symbol is
    defined in this file. `contains` is regenerated by the parser and also
    wiped here so per-file state is internally consistent. Also clears any
    previously persisted unresolved_calls for this file — they will be
    re-emitted from the fresh parse.
    """
    conn.execute(
        """
        DELETE FROM relations
         WHERE relation_kind IN ('calls', 'imports', 'inherits', 'contains')
           AND src_symbol_pk IN (
               SELECT s.symbol_pk
                 FROM symbols s
                 JOIN occurrences o ON o.symbol_pk = s.symbol_pk
                WHERE o.file_pk = ? AND o.role = 'definition'
           )
        """,
        (file_pk,),
    )
    conn.execute("DELETE FROM unresolved_calls WHERE file_pk = ?", (file_pk,))


def reindex(
    conn: sqlite3.Connection,
    config: Config,
    *,
    paths: list[Path] | None = None,
    event_source: str = "update",
    registry: Registry | None = None,
    force: bool = False,
    lock_timeout_s: float = 30.0,
) -> ReindexStats:
    # Serialize writers. `init`, `update`, watch flushes, and the MCP
    # `update` tool all reach this function. The lock guards the whole
    # logical reindex (file clears, tombstones, backfill, test-edge
    # rebuild), not just individual SQL transactions.
    from code_index.locking import writer_lock

    with writer_lock(config, timeout_s=lock_timeout_s):
        return _reindex_body(
            conn,
            config,
            paths=paths,
            event_source=event_source,
            registry=registry,
            force=force,
        )


def _reindex_body(
    conn: sqlite3.Connection,
    config: Config,
    *,
    paths: list[Path] | None = None,
    event_source: str = "update",
    registry: Registry | None = None,
    force: bool = False,
) -> ReindexStats:
    registry = registry or default_registry()
    matcher = build_matcher(
        config.root,
        extra=config.extra_ignore,
        include_hidden=config.include_hidden,
    )
    targets = _resolve_paths(config, matcher, paths)
    stats = ReindexStats()

    # Git metadata resolver. Non-git repos get an always-disabled instance
    # that short-circuits every call. Blob oids from `ls-files --stage` are
    # cached once; commit-info lookups happen on demand only for files we
    # actually reparse.
    from code_index.git_meta import resolver_for

    git_meta = resolver_for(config.root)

    # Track which files we touched so we can tombstone disappeared files when
    # the caller performed a full scan (paths is None and force or not).
    touched: set[str] = set()

    # Buffer pending relations across all files as (file_pk, PendingRelation).
    # Resolution happens in one pass after every file has been upserted, so
    # cross-file edges can target symbols we only learn about later.
    all_pending: list[tuple[int, "PendingRelation"]] = []

    # Scope-tracking for incremental passes:
    #   * `touched_file_pks` — files that actually got reparsed this run.
    #     Used to scope `_rebuild_test_edges` on targeted updates.
    #   * `symbols_count_before` — snapshot of live-symbol count to detect
    #     whether this parse actually introduced a new symbol. Used to
    #     short-circuit the backfill pass on targeted updates.
    touched_file_pks: set[int] = set()
    symbols_count_before = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE deleted_at IS NULL"
    ).fetchone()[0]

    for abs_path, rel_path, size in targets:
        stats.files_seen += 1
        try:
            stat_result = abs_path.stat()
        except OSError as exc:
            stats.errors.append(f"{rel_path}: stat failed: {exc}")
            continue
        mtime_ns = getattr(stat_result, "st_mtime_ns", None)

        # Fast path: if mtime + size both match the stored values AND we have
        # a non-null worktree_hash AND the file isn't tombstoned AND we aren't
        # forcing, we can skip the expensive read+hash entirely. This turns a
        # no-op reindex into a pure SQLite scan.
        if not force:
            existing = conn.execute(
                "SELECT file_pk, worktree_hash, size_bytes, mtime_ns, deleted_at FROM files WHERE file_path = ?",
                (rel_path,),
            ).fetchone()
            if (
                existing
                and existing["deleted_at"] is None
                and existing["worktree_hash"] is not None
                and existing["size_bytes"] == size
                and existing["mtime_ns"] is not None
                and existing["mtime_ns"] == mtime_ns
            ):
                stats.files_unchanged += 1
                touched.add(rel_path)
                continue

        text, data, read_err = _read_source(abs_path)
        # Git metadata for this file (cheap: blob_oid from ls-files cache;
        # commit_info short-circuits to (None, None) on non-git repos).
        git_blob_oid = git_meta.blob_oid(rel_path)
        git_ts, git_author = git_meta.commit_info(rel_path)
        if read_err:
            stats.files_failed += 1
            stats.errors.append(f"{rel_path}: {read_err}")
            with transaction(conn):
                _ensure_file_row(
                    conn,
                    rel_path=rel_path,
                    language=None,
                    wth=None,
                    size=size,
                    mtime_ns=mtime_ns,
                    parse_status="failed",
                    parse_error=read_err,
                    semantic_source=None,
                    parser_confidence=None,
                    git_blob_oid=git_blob_oid,
                    git_committed_at=git_ts,
                    git_author=git_author,
                )
            continue
        wth = worktree_hash(data or b"")

        # Skip if unchanged and not forced.
        if not force:
            existing = conn.execute(
                "SELECT file_pk, worktree_hash, deleted_at FROM files WHERE file_path = ?",
                (rel_path,),
            ).fetchone()
            if (
                existing
                and existing["worktree_hash"] == wth
                and existing["deleted_at"] is None
            ):
                stats.files_unchanged += 1
                touched.add(rel_path)
                continue

        parser = registry.select(rel_path)
        try:
            parsed = parser.parse(rel_path=rel_path, source=text or "")
        except Exception as exc:  # defensive; parsers should handle their own errors
            stats.files_failed += 1
            stats.errors.append(f"{rel_path}: parser {parser.name} crashed: {exc!r}")
            with transaction(conn):
                _ensure_file_row(
                    conn,
                    rel_path=rel_path,
                    language=None,
                    wth=wth,
                    size=size,
                    mtime_ns=mtime_ns,
                    parse_status="failed",
                    parse_error=f"{parser.name} crashed: {exc!r}",
                    semantic_source=parser.name,
                    parser_confidence=0.0,
                    git_blob_oid=git_blob_oid,
                    git_committed_at=git_ts,
                    git_author=git_author,
                )
            continue

        with transaction(conn):
            file_pk = _ensure_file_row(
                conn,
                rel_path=rel_path,
                language=parsed.language,
                wth=wth,
                size=size,
                mtime_ns=mtime_ns,
                parse_status=parsed.parse_status,
                parse_error=parsed.parse_error,
                semantic_source=parsed.semantic_source,
                parser_confidence=parsed.confidence,
                git_blob_oid=git_blob_oid,
                git_committed_at=git_ts,
                git_author=git_author,
            )
            if parsed.parse_status in {"ok", "empty"}:
                # Wipe relations whose src is defined in this file so the
                # resolve pass rebuilds them from the current parse.
                _clear_relations_for_file(conn, file_pk)
                _apply_parsed_file(
                    conn,
                    rel_path=rel_path,
                    file_pk=file_pk,
                    parsed=parsed,
                    event_source=event_source,
                    stats=stats,
                )
                touched_file_pks.add(file_pk)
                if parsed.pending_relations:
                    all_pending.extend(
                        (file_pk, rel) for rel in parsed.pending_relations
                    )
            elif parsed.parse_status == "failed":
                # Still capture diagnostics so tooling can see why parsing failed.
                _record_diagnostics_only(
                    conn,
                    file_pk=file_pk,
                    parsed=parsed,
                )
        touched.add(rel_path)
        if parsed.parse_status == "ok":
            stats.files_parsed += 1
        elif parsed.parse_status == "failed":
            stats.files_failed += 1
        else:
            stats.files_skipped += 1

    # Full-scan tombstone: only when no explicit path list was given.
    if paths is None:
        existing_paths = {
            row["file_path"]
            for row in conn.execute(
                "SELECT file_path FROM files WHERE deleted_at IS NULL"
            ).fetchall()
        }
        missing = existing_paths - touched
        now = _now_iso()
        for rel in missing:
            with transaction(conn):
                file_row = conn.execute(
                    "SELECT file_pk FROM files WHERE file_path = ?",
                    (rel,),
                ).fetchone()
                if file_row is None:
                    continue
                conn.execute(
                    "UPDATE files SET deleted_at = ?, parse_status = 'deleted' WHERE file_pk = ?",
                    (now, file_row["file_pk"]),
                )
                for chunk_row in conn.execute(
                    "SELECT chunk_pk, chunk_uid, raw_hash FROM chunks WHERE file_pk = ? AND deleted_at IS NULL",
                    (file_row["file_pk"],),
                ).fetchall():
                    conn.execute(
                        "UPDATE chunks SET deleted_at = ? WHERE chunk_pk = ?",
                        (now, chunk_row["chunk_pk"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO chunk_edits(
                            chunk_pk, chunk_uid, timestamp, event_source,
                            old_raw_hash, new_raw_hash, change_type, diff_summary
                        ) VALUES (?, ?, ?, ?, ?, NULL, 'delete', 'file deleted')
                        """,
                        (
                            chunk_row["chunk_pk"],
                            chunk_row["chunk_uid"],
                            now,
                            event_source,
                            chunk_row["raw_hash"],
                        ),
                    )
                    stats.chunks_tombstoned += 1
                    stats.edits_recorded += 1

    now = _now_iso()
    with transaction(conn):
        # Build re-export map once per reindex so cross-file lookups through
        # `__init__.py` alias chains resolve correctly.
        reexport_map = _build_reexport_map(conn)

        # Track file_pks where Jedi added a relation so the scoped
        # test_edges rebuild can pick up any new test→target reachability.
        jedi_touched_files: set[int] = set()

        if all_pending:
            _resolve_pending(
                conn,
                all_pending,
                stats,
                now,
                reexport_map=reexport_map,
                config=config,
                jedi_touched_files=jedi_touched_files,
            )
        # Move dead edges (live src, tombstoned dst) into unresolved_calls so
        # the backfill step can heal them when the target reappears (e.g. stub
        # restored, symbol re-added with the same canonical_name).
        _repair_dead_edges(conn, stats, now)

        # Conditional backfill:
        #   * Full scan (`paths is None`): always backfill — the whole graph
        #     may have shifted.
        #   * Targeted update: only backfill if the parse actually changed
        #     the symbol inventory (new symbol or tombstone). Otherwise the
        #     graph topology is identical and the 15k-row walk is wasted work.
        symbols_count_after = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE deleted_at IS NULL"
        ).fetchone()[0]
        topology_changed = (
            symbols_count_after != symbols_count_before
            or stats.symbols_tombstoned > 0
            or stats.relations_queued_for_repair > 0
        )
        if paths is None or topology_changed:
            _backfill_unresolved(
                conn,
                stats,
                now,
                reexport_map=reexport_map,
                config=config,
                jedi_touched_files=jedi_touched_files,
            )
        else:
            stats.relations_backfill_skipped = True

        # Scoped vs full test_edges rebuild:
        #   * Full scan, force=True, or topology change ⇒ full rebuild.
        #   * Targeted update with no topology change ⇒ only touch edges
        #     whose test_symbol or target_symbol lives in a touched file.
        if paths is None or topology_changed or force:
            _rebuild_test_edges(conn, stats)
            stats.test_edges_rebuilt_scope = "full"
        elif touched_file_pks or jedi_touched_files:
            # Union in any files where Jedi landed new relations so tests
            # that now have a resolved call chain get their edges refreshed.
            _rebuild_test_edges_scoped(
                conn, stats, touched_file_pks | jedi_touched_files
            )
            stats.test_edges_rebuilt_scope = "scoped"
        else:
            # Nothing parsed at all — leave test_edges untouched.
            stats.test_edges_rebuilt_scope = "scoped"

    return stats
