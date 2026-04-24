"""FTS5-backed ranked retrieval.

Columns are weighted so symbol identity ranks above body content:
    symbol_name > symbol_path > signature > file_path > content
"""

from __future__ import annotations

import sqlite3

BM25_WEIGHTS = (
    10.0,
    6.0,
    5.0,
    2.0,
    1.0,
)  # symbol_name, symbol_path, signature, file_path, content


def _sanitize(query: str) -> str:
    # Drop characters that could break FTS5 MATCH syntax; this is a bounded
    # helper, not a full query-language abstraction.
    safe_chars = []
    for ch in query:
        if ch.isalnum() or ch in {"_", ".", "-", "*", ":"}:
            safe_chars.append(ch)
        else:
            safe_chars.append(" ")
    cleaned = " ".join("".join(safe_chars).split())
    return cleaned


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    language: str | None = None,
    chunk_type: str | None = None,
) -> list[dict]:
    if not query.strip():
        return []
    fts_query = _sanitize(query)
    if not fts_query:
        return []
    weights = ",".join(str(w) for w in BM25_WEIGHTS)
    sql = f"""
        SELECT c.chunk_uid, c.file_path, c.language, c.chunk_type,
               c.symbol_name, c.symbol_path, c.signature,
               c.start_line, c.end_line,
               bm25(chunks_fts, {weights}) AS score,
               snippet(chunks_fts, 4, '[', ']', '…', 8) AS snippet
        FROM chunks_fts
        JOIN chunks c ON c.chunk_pk = chunks_fts.rowid
        WHERE chunks_fts MATCH ?
          AND c.deleted_at IS NULL
    """
    params: list = [fts_query]
    if language:
        sql += " AND c.language = ?"
        params.append(language)
    if chunk_type:
        sql += " AND c.chunk_type = ?"
        params.append(chunk_type)
    sql += " ORDER BY score ASC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
