"""Relation resolution helpers for the indexing pipeline."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


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
    for row in rows:
        pkg = row["symbol_path"] or ""
        if not pkg:
            continue
        try:
            ctx = json.loads(row["context_json"] or "{}")
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
    config: Any | None,
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
    stats: Any,
    now: str,
    reexport_map: dict[str, str] | None = None,
    config: Any | None = None,
    relation_touched_files: set[int] | None = None,
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
                    json.dumps(list(rel.dst_candidates)),
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
            if relation_touched_files is not None:
                relation_touched_files.add(int(file_pk))
            if used_jedi:
                stats.relations_resolved_by_jedi += 1
            if rel.relation_kind == "calls":
                _insert_call_reference_occurrence(
                    conn,
                    src_pk=src_pk,
                    dst_pk=dst_pk,
                    site_line=rel.site_line,
                )


def _backfill_unresolved(
    conn: sqlite3.Connection,
    stats: Any,
    now: str,
    reexport_map: dict[str, str] | None = None,
    config: Any | None = None,
    relation_touched_files: set[int] | None = None,
    candidate_names: set[str] | None = None,
) -> None:
    """Re-attempt every still-unresolved pending relation in the DB.

    Cheap because the unresolved_calls table is small and each row is a
    handful of indexed canonical-name lookups. Rows only advance to
    `resolved_at = now` when an edge actually lands; otherwise they stay
    open so a later reindex can retry when the missing symbol appears.
    """
    sql = """
        SELECT unresolved_pk, file_pk, src_symbol_uid, relation_kind,
               dst_candidates_json, site_line, provenance
          FROM unresolved_calls
         WHERE resolved_at IS NULL
    """
    params: list[str] = []
    if candidate_names is not None:
        names = sorted(name for name in candidate_names if name)
        if not names:
            return
        if len(names) <= 100:
            clauses = []
            for name in names:
                clauses.append("dst_candidates_json LIKE ?")
                params.append(f'%"{name}"%')
            sql += " AND (" + " OR ".join(clauses) + ")"
    rows = conn.execute(sql, params).fetchall()

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
            candidates = json.loads(row["dst_candidates_json"] or "[]")
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
            if relation_touched_files is not None and row["file_pk"] is not None:
                relation_touched_files.add(int(row["file_pk"]))
            if used_jedi:
                stats.relations_resolved_by_jedi += 1
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
    stats: Any,
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
                json.dumps(candidates),
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
