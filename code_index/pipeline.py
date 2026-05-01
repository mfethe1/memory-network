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
from code_index.db_router import transaction
from code_index.hashing import worktree_hash
from code_index.ignore import IgnoreMatcher, build as build_matcher
from code_index.parsers import ParseResult, Registry, default_registry
from code_index.scanner import iter_files
from code_index.relation_resolver import (
    _backfill_unresolved,
    _build_reexport_map,
    _insert_call_reference_occurrence,
    _insert_relation,
    _match_exact_or_reexport,
    _match_suffix,
    _maybe_jedi_resolve,
    _repair_dead_edges,
    _resolve_pending,
    _try_resolve_candidates,
    _try_resolve_with_jedi,
)
from code_index.test_edges import (
    _collect_scoped_test_symbols,
    _collect_test_targets_for,
    _insert_test_edges_for,
    _is_test_path,
    _rebuild_edges_for_test_rows,
    _rebuild_test_edges,
    _rebuild_test_edges_for_test_symbols,
    _rebuild_test_edges_scoped,
)


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
    if paths is not None:
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


def _missing_explicit_paths(config: Config, paths: list[Path] | None) -> set[str]:
    """Return repo-relative paths explicitly requested but absent on disk."""
    if paths is None:
        return set()
    root = config.root.resolve()
    missing: set[str] = set()
    for raw in paths:
        p = raw if raw.is_absolute() else (config.root / raw)
        p = p.resolve()
        if p.exists():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel:
            missing.add(rel)
    return missing


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
) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT symbol_pk, deleted_at FROM symbols WHERE symbol_uid = ?",
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
        return int(cur.lastrowid), True
    reactivated = row["deleted_at"] is not None
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
    return int(row["symbol_pk"]), reactivated


def _insert_diagnostics(
    conn: sqlite3.Connection,
    *,
    file_pk: int,
    diagnostics: list,
    now: str,
) -> None:
    for diag in diagnostics:
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


def _apply_parsed_file(
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    file_pk: int,
    parsed: ParseResult,
    event_source: str,
    stats: ReindexStats,
    new_symbol_candidates: set[str] | None = None,
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
        symbol_pk, newly_available = _upsert_symbol(conn, sym, container_pk, now)
        uid_to_pk[sym.symbol_uid] = symbol_pk
        stats.symbols_upserted += 1
        if newly_available and new_symbol_candidates is not None:
            new_symbol_candidates.add(sym.canonical_name)
            if sym.display_name:
                new_symbol_candidates.add(sym.display_name)

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

    _insert_diagnostics(
        conn,
        file_pk=file_pk,
        diagnostics=parsed.diagnostics,
        now=now,
    )


def _tombstone_file(
    conn: sqlite3.Connection,
    *,
    file_pk: int,
    now: str,
    event_source: str,
    stats: ReindexStats,
    test_symbols_to_rebuild: set[int] | None = None,
) -> int:
    """Soft-delete a file and semantic state derived from its definitions."""
    definition_rows = conn.execute(
        """
        SELECT DISTINCT symbol_pk
          FROM occurrences
         WHERE file_pk = ? AND role = 'definition'
        """,
        (file_pk,),
    ).fetchall()
    definition_pks = [int(row["symbol_pk"]) for row in definition_rows]
    symbols_tombstoned = 0

    if definition_pks and test_symbols_to_rebuild is not None:
        placeholders = ",".join("?" for _ in definition_pks)
        rows = conn.execute(
            f"""
            SELECT DISTINCT test_symbol_pk
              FROM test_edges
             WHERE test_symbol_pk IN ({placeholders})
                OR target_symbol_pk IN ({placeholders})
            """,
            (*definition_pks, *definition_pks),
        ).fetchall()
        test_symbols_to_rebuild.update(int(row["test_symbol_pk"]) for row in rows)

    conn.execute(
        "UPDATE files SET deleted_at = ?, parse_status = 'deleted' WHERE file_pk = ?",
        (now, file_pk),
    )
    conn.execute("DELETE FROM diagnostics WHERE file_pk = ?", (file_pk,))
    conn.execute("DELETE FROM unresolved_calls WHERE file_pk = ?", (file_pk,))
    conn.execute("DELETE FROM occurrences WHERE file_pk = ?", (file_pk,))

    for chunk_row in conn.execute(
        """
        SELECT chunk_pk, chunk_uid, raw_hash
          FROM chunks
         WHERE file_pk = ? AND deleted_at IS NULL
        """,
        (file_pk,),
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

    for symbol_pk in definition_pks:
        still_defined = conn.execute(
            """
            SELECT 1
              FROM occurrences o
              JOIN files f ON f.file_pk = o.file_pk
             WHERE o.symbol_pk = ?
               AND o.role = 'definition'
               AND f.deleted_at IS NULL
             LIMIT 1
            """,
            (symbol_pk,),
        ).fetchone()
        if still_defined is not None:
            continue
        cur = conn.execute(
            """
            UPDATE symbols
               SET deleted_at = ?
             WHERE symbol_pk = ? AND deleted_at IS NULL
            """,
            (now, symbol_pk),
        )
        if cur.rowcount:
            stats.symbols_tombstoned += 1
            symbols_tombstoned += 1

    if definition_pks:
        conn.executemany(
            "DELETE FROM relations WHERE src_symbol_pk = ?",
            [(pk,) for pk in definition_pks],
        )
    return symbols_tombstoned


def _record_diagnostics_only(
    conn: sqlite3.Connection,
    *,
    file_pk: int,
    parsed: ParseResult,
) -> None:
    """Insert diagnostics from a parse that otherwise produced no symbols/chunks."""
    now = _now_iso()
    conn.execute("DELETE FROM diagnostics WHERE file_pk = ?", (file_pk,))
    _insert_diagnostics(
        conn,
        file_pk=file_pk,
        diagnostics=parsed.diagnostics,
        now=now,
    )


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
    missing_explicit = _missing_explicit_paths(config, paths)
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
    relation_touched_file_pks: set[int] = set()
    test_symbols_to_rebuild: set[int] = set()
    new_symbol_candidates: set[str] = set()
    symbols_tombstoned_by_deleted_files = 0
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

        # Re-check size guard; file may have grown between scan and read.
        if stat_result.st_size > config.max_file_bytes:
            stats.errors.append(f"{rel_path}: file grew beyond max_file_bytes")
            continue

        text, data, read_err = _read_source(abs_path)
        # Git metadata for this file (cheap: blob_oid from ls-files cache;
        # commit_info short-circuits to (None, None) on non-git repos).
        try:
            git_blob_oid = git_meta.blob_oid(rel_path)
            git_ts, git_author = git_meta.commit_info(rel_path)
        except Exception:
            git_blob_oid = None
            git_ts = None
            git_author = None
        if read_err:
            stats.files_failed += 1
            stats.errors.append(f"{rel_path}: {read_err}")
            with transaction(conn):
                file_pk = _ensure_file_row(
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
                _tombstone_file(
                    conn,
                    file_pk=file_pk,
                    now=_now_iso(),
                    event_source=event_source,
                    stats=stats,
                    test_symbols_to_rebuild=test_symbols_to_rebuild,
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
                file_pk = _ensure_file_row(
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
                _tombstone_file(
                    conn,
                    file_pk=file_pk,
                    now=_now_iso(),
                    event_source=event_source,
                    stats=stats,
                    test_symbols_to_rebuild=test_symbols_to_rebuild,
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
                    new_symbol_candidates=new_symbol_candidates,
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

    # Targeted tombstone: explicit deleted file/dir paths from update --files
    # and watch delete events.
    if missing_explicit:
        now = _now_iso()
        for rel in sorted(missing_explicit):
            prefix = rel.rstrip("/") + "/%"
            rows = conn.execute(
                """
                SELECT file_pk, file_path
                  FROM files
                 WHERE file_path = ? OR file_path LIKE ?
                """,
                (rel, prefix),
            ).fetchall()
            for file_row in rows:
                with transaction(conn):
                    symbols_tombstoned_by_deleted_files += _tombstone_file(
                        conn,
                        file_pk=int(file_row["file_pk"]),
                        now=now,
                        event_source=event_source,
                        stats=stats,
                        test_symbols_to_rebuild=test_symbols_to_rebuild,
                    )
                touched_file_pks.add(int(file_row["file_pk"]))

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
                symbols_tombstoned_by_deleted_files += _tombstone_file(
                    conn,
                    file_pk=int(file_row["file_pk"]),
                    now=now,
                    event_source=event_source,
                    stats=stats,
                    test_symbols_to_rebuild=test_symbols_to_rebuild,
                )
                touched_file_pks.add(int(file_row["file_pk"]))

    legacy_deleted_rows = conn.execute(
        """
        SELECT DISTINCT f.file_pk
          FROM files f
          JOIN occurrences o ON o.file_pk = f.file_pk
          JOIN symbols s ON s.symbol_pk = o.symbol_pk
         WHERE f.deleted_at IS NOT NULL
           AND o.role = 'definition'
           AND s.deleted_at IS NULL
        """
    ).fetchall()
    if legacy_deleted_rows:
        now = _now_iso()
        for file_row in legacy_deleted_rows:
            with transaction(conn):
                symbols_tombstoned_by_deleted_files += _tombstone_file(
                    conn,
                    file_pk=int(file_row["file_pk"]),
                    now=now,
                    event_source=event_source,
                    stats=stats,
                    test_symbols_to_rebuild=test_symbols_to_rebuild,
                )
                touched_file_pks.add(int(file_row["file_pk"]))

    now = _now_iso()
    with transaction(conn):
        # Build re-export map once per reindex so cross-file lookups through
        # `__init__.py` alias chains resolve correctly.
        reexport_map = _build_reexport_map(conn)

        if all_pending:
            _resolve_pending(
                conn,
                all_pending,
                stats,
                now,
                reexport_map=reexport_map,
                config=config,
                relation_touched_files=relation_touched_file_pks,
            )
        # Move dead edges (live src, tombstoned dst) into unresolved_calls so
        # the backfill step can heal them when the target reappears (e.g. stub
        # restored, symbol re-added with the same canonical_name).
        _repair_dead_edges(conn, stats, now)

        # Conditional backfill:
        #   * Force or topology change: retry unresolved rows because new or
        #     repaired symbols may have made previously-open edges resolvable.
        #   * Targeted update: only backfill if the parse actually changed
        #     the symbol inventory (new symbol or tombstone). Otherwise the
        #     graph topology is identical and the 15k-row walk is wasted work.
        symbols_count_after = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE deleted_at IS NULL"
        ).fetchone()[0]
        parsed_symbol_tombstones = (
            stats.symbols_tombstoned > symbols_tombstoned_by_deleted_files
        )
        symbols_added = symbols_count_after > symbols_count_before
        topology_changed = (
            symbols_added
            or parsed_symbol_tombstones
            or stats.relations_queued_for_repair > 0
        )
        backfill_candidates: set[str] | None = new_symbol_candidates
        if (
            force
            or parsed_symbol_tombstones
            or stats.relations_queued_for_repair > 0
            or symbols_count_before == 0
        ):
            backfill_candidates = None

        if force or topology_changed:
            _backfill_unresolved(
                conn,
                stats,
                now,
                reexport_map=reexport_map,
                config=config,
                relation_touched_files=relation_touched_file_pks,
                candidate_names=backfill_candidates,
            )
        else:
            stats.relations_backfill_skipped = True

        # Scoped vs full test_edges rebuild:
        #   * Force, initial build, or in-file symbol removals ⇒ full rebuild.
        #   * Otherwise rebuild only tests whose own file, existing target
        #     file, backfilled source file, or deleted target/test symbol was
        #     touched.
        if force or symbols_count_before == 0 or parsed_symbol_tombstones:
            _rebuild_test_edges(conn, stats)
            stats.test_edges_rebuilt_scope = "full"
        else:
            scoped_test_pks = set(test_symbols_to_rebuild)
            edge_scope_files = touched_file_pks | relation_touched_file_pks
            if edge_scope_files:
                scoped_test_pks.update(
                    _collect_scoped_test_symbols(conn, edge_scope_files)
                )
            if scoped_test_pks:
                _rebuild_test_edges_for_test_symbols(
                    conn,
                    stats,
                    scoped_test_pks,
                )
            stats.test_edges_rebuilt_scope = "scoped"

    return stats
