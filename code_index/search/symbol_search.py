"""Symbol lookup from the semantic spine."""

from __future__ import annotations

import sqlite3


def lookup(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 50,
    include_references: bool = False,
) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    clauses = ["s.deleted_at IS NULL"]
    params: list = []
    # Exact canonical match first, then prefix, then substring via LIKE.
    clauses.append(
        "(s.canonical_name = ? OR s.canonical_name LIKE ? OR s.display_name = ? OR s.display_name LIKE ?)"
    )
    like = f"%{query}%"
    params.extend([query, like, query, like])
    if kind:
        clauses.append("s.kind = ?")
        params.append(kind)
    if language:
        clauses.append("s.language = ?")
        params.append(language)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT s.symbol_pk, s.symbol_uid, s.kind, s.language,
               s.canonical_name, s.display_name, s.signature_norm,
               s.semantic_source, s.confidence,
               (SELECT COUNT(*) FROM occurrences o
                WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition') AS def_count,
               (SELECT f.file_path FROM occurrences o
                 JOIN files f ON f.file_pk = o.file_pk
                 WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_file,
               (SELECT o.start_line FROM occurrences o
                 WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_line
        FROM symbols s
        WHERE {where}
        ORDER BY
          (s.canonical_name = ?) DESC,
          (s.display_name = ?) DESC,
          LENGTH(s.canonical_name) ASC,
          s.canonical_name ASC
        LIMIT ?
    """
    params.extend([query, query, int(limit)])
    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]

    # Re-export fallback: if no hit, query may be a `pkg.Name` alias whose
    # real target lives at `pkg.sub.Name`. Consult the re-export map and
    # retry against the resolved canonical name. Marks hits with
    # `via_reexport=True` so consumers can surface the indirection.
    if not results and "." in query:
        from code_index.relation_resolver import _build_reexport_map

        rx_map = _build_reexport_map(conn)
        seen: set[str] = {query}
        target = rx_map.get(query)
        hop_count = 0
        while target and target not in seen and hop_count < 5:
            seen.add(target)
            hop_count += 1
            row = conn.execute(
                """
                SELECT s.symbol_pk, s.symbol_uid, s.kind, s.language,
                       s.canonical_name, s.display_name, s.signature_norm,
                       s.semantic_source, s.confidence,
                       (SELECT COUNT(*) FROM occurrences o
                        WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition') AS def_count,
                       (SELECT f.file_path FROM occurrences o
                         JOIN files f ON f.file_pk = o.file_pk
                         WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                         ORDER BY o.start_line ASC LIMIT 1) AS def_file,
                       (SELECT o.start_line FROM occurrences o
                         WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                         ORDER BY o.start_line ASC LIMIT 1) AS def_line
                  FROM symbols s
                 WHERE s.deleted_at IS NULL
                   AND s.canonical_name = ?
                 LIMIT 1
                """,
                (target,),
            ).fetchone()
            if row:
                hit = dict(row)
                hit["via_reexport"] = query
                results.append(hit)
                break
            target = rx_map.get(target)

    for result in results:
        symbol_pk = int(result.pop("symbol_pk"))
        if include_references:
            ref_rows = conn.execute(
                """
                SELECT f.file_path AS file,
                       o.start_line AS start_line,
                       o.end_line AS end_line
                  FROM occurrences o
                  JOIN files f ON f.file_pk = o.file_pk
                 WHERE o.symbol_pk = ?
                   AND o.role = 'reference'
                   AND o.syntax_kind = 'call'
                   AND f.deleted_at IS NULL
                 ORDER BY f.file_path ASC, o.start_line ASC, o.end_line ASC
                 LIMIT 50
                """,
                (symbol_pk,),
            ).fetchall()
            result["references"] = [dict(r) for r in ref_rows]
    return results
