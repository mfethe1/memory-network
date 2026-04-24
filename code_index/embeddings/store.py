"""Embeddings persistence + cosine-similarity retrieval over chunks.

Uses the existing `embeddings` table (chunk_pk, provider, model,
dimension, embedding_blob, embedding_norm, updated_at). BLOB format:
little-endian float32 array, length = dimension. This keeps the index
file portable and avoids pickling.

Brute-force cosine scan is fine at repo scale. We avoid two costs an
earlier version paid per query: (1) full-sorting every scored row
before taking top-k, and (2) recomputing the corpus vector norm every
query. `embedding_norm` is precomputed on insert; `search` uses a
k-sized heap via `heapq.nlargest` and streams rows with `fetchmany`.
"""

from __future__ import annotations

import heapq
import math
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(data: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", data))


def _norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec)) or 1.0


def _chunk_text_for_embedding(row: sqlite3.Row) -> str:
    """Compose a compact embedding input from a chunk row. Prepend
    symbol_path + signature so symbol-level semantics get weighted heavily.
    """
    parts = []
    if row["symbol_path"]:
        parts.append(row["symbol_path"])
    if row["signature"]:
        parts.append(row["signature"])
    content = row["content"] or ""
    # Truncate body — bge-small is 512 tokens; keep some room for
    # the symbol_path prefix.
    parts.append(content[:1800])
    return "\n".join(parts)


def coverage(conn: sqlite3.Connection) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
    ).fetchone()[0]
    embedded = conn.execute(
        """
        SELECT COUNT(DISTINCT e.chunk_pk)
          FROM embeddings e
          JOIN chunks c ON c.chunk_pk = e.chunk_pk
         WHERE c.deleted_at IS NULL
        """
    ).fetchone()[0]
    # `stale_count`: live embedding rows whose stored content_hash does not
    # match the current chunk raw_hash. This is the drift signal for the
    # stale-embedding bug closed in schema v5 — non-zero means the next
    # `populate()` will re-embed that many rows.
    try:
        stale = conn.execute(
            """
            SELECT COUNT(*)
              FROM embeddings e
              JOIN chunks c ON c.chunk_pk = e.chunk_pk
             WHERE c.deleted_at IS NULL
               AND (e.content_hash IS NULL OR e.content_hash != c.raw_hash)
            """
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # Column missing on a stale DB that didn't migrate yet.
        stale = 0
    dims = conn.execute("SELECT DISTINCT dimension FROM embeddings").fetchall()
    return {
        "total_chunks": int(total),
        "embedded_chunks": int(embedded),
        "stale_count": int(stale),
        "coverage_pct": (100 * embedded / total) if total else 0.0,
        "dimensions": sorted({int(r[0]) for r in dims if r[0] is not None}),
    }


def populate(
    conn: sqlite3.Connection,
    backend,
    *,
    batch: int = 32,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Embed every live chunk that doesn't already have an embedding for
    `(backend.provider, backend.model_name)`. Returns a stats dict."""
    if refresh:
        conn.execute(
            "DELETE FROM embeddings WHERE provider = ? AND model = ?",
            (backend.provider, backend.model_name),
        )
    # Find chunks needing work. A chunk needs embedding if either:
    #   (a) no row exists for (chunk_pk, provider, model), OR
    #   (b) a row exists but its stored content_hash != the chunk's current
    #       raw_hash (content drift since the vector was computed).
    # The LEFT JOIN + filter handles both cases in one scan.
    sql = """
        SELECT c.chunk_pk, c.symbol_path, c.signature, c.content, c.raw_hash
          FROM chunks c
          LEFT JOIN embeddings e
                 ON e.chunk_pk = c.chunk_pk
                AND e.provider = ?
                AND e.model = ?
         WHERE c.deleted_at IS NULL
           AND (
                 e.embedding_pk IS NULL
              OR e.content_hash IS NULL
              OR e.content_hash != c.raw_hash
           )
         ORDER BY c.chunk_pk ASC
    """
    params: list[Any] = [backend.provider, backend.model_name]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()

    embedded = 0
    errors = 0
    now = _now_iso()
    buf: list[tuple[int, str, str]] = []
    for row in rows:
        buf.append(
            (
                int(row["chunk_pk"]),
                _chunk_text_for_embedding(row),
                row["raw_hash"],
            )
        )
        if len(buf) >= batch:
            embedded, errors = _flush(conn, backend, buf, embedded, errors, now)
            buf = []
    if buf:
        embedded, errors = _flush(conn, backend, buf, embedded, errors, now)
    return {
        "embedded": embedded,
        "errors": errors,
        "batches": math.ceil(len(rows) / batch) if rows else 0,
    }


def _flush(conn, backend, buf, embedded, errors, now) -> tuple[int, int]:
    texts = [t for _, t, _ in buf]
    try:
        vectors = backend.embed(texts)
    except Exception:
        return embedded, errors + len(buf)
    for (chunk_pk, _, content_hash), vec in zip(buf, vectors):
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings(
                    chunk_pk, provider, model, dimension, embedding_blob,
                    embedding_norm, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_pk,
                    backend.provider,
                    backend.model_name,
                    len(vec),
                    _pack(vec),
                    _norm(vec),
                    content_hash,
                    now,
                ),
            )
            embedded += 1
        except sqlite3.Error:
            errors += 1
    return embedded, errors


def search(
    conn: sqlite3.Connection,
    backend,
    query: str,
    *,
    limit: int = 10,
    language: str | None = None,
    chunk_type: str | None = None,
) -> list[dict]:
    """Embed the query, scan live chunk embeddings, return top-N cosine hits.

    Streams rows with `fetchmany` and uses `heapq.nlargest` so we never
    materialize the full scored list or full-sort it. Reads the stored
    `embedding_norm` from the DB to avoid recomputing the corpus norm on
    every query.
    """
    q_vec = backend.embed([query])[0]
    q_norm = _norm(q_vec)

    where_clauses = ["c.deleted_at IS NULL", "e.provider = ?", "e.model = ?"]
    params: list[Any] = [backend.provider, backend.model_name]
    if language:
        where_clauses.append("c.language = ?")
        params.append(language)
    if chunk_type:
        where_clauses.append("c.chunk_type = ?")
        params.append(chunk_type)
    where = " AND ".join(where_clauses)

    cursor = conn.execute(
        f"""
        SELECT c.chunk_pk, c.chunk_uid, c.file_path, c.language,
               c.chunk_type, c.symbol_name, c.symbol_path, c.signature,
               c.start_line, c.end_line, e.dimension, e.embedding_blob,
               e.embedding_norm
          FROM embeddings e
          JOIN chunks c ON c.chunk_pk = e.chunk_pk
         WHERE {where}
        """,
        params,
    )

    def scored_iter():
        # Stream rows through a generator so only the current batch sits in
        # Python memory. The heap below keeps the top-k. The monotonic
        # counter `idx` is a tiebreaker so heapq never has to compare
        # sqlite3.Row objects (which aren't orderable).
        idx = 0
        while True:
            batch = cursor.fetchmany(512)
            if not batch:
                return
            for row in batch:
                dim = int(row["dimension"])
                blob = row["embedding_blob"]
                stored_norm = row["embedding_norm"]
                # Fall back to on-the-fly norm for rows an old migration
                # path somehow missed. Shouldn't happen post-v4, but cheap
                # insurance.
                vec = struct.unpack(f"<{dim}f", blob)
                if stored_norm is None or stored_norm <= 0:
                    b_norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                else:
                    b_norm = float(stored_norm)
                dot = 0.0
                for x, y in zip(q_vec, vec):
                    dot += x * y
                score = dot / (q_norm * b_norm)
                yield score, idx, row
                idx += 1

    # `heapq.nlargest` does an O(n log k) pass instead of O(n log n) sort.
    top = heapq.nlargest(limit, scored_iter(), key=lambda t: t[0])
    return [
        {
            "chunk_uid": row["chunk_uid"],
            "file_path": row["file_path"],
            "language": row["language"],
            "chunk_type": row["chunk_type"],
            "symbol_name": row["symbol_name"],
            "symbol_path": row["symbol_path"],
            "signature": row["signature"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "score": score,
        }
        for score, _idx, row in top
    ]


def _cosine(a: list[float], a_norm: float, b: list[float]) -> float:
    """Retained for callers/tests outside the search() hot path."""
    dot = sum(x * y for x, y in zip(a, b))
    b_norm = _norm(b)
    return dot / (a_norm * b_norm)
