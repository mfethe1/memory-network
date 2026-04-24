"""Embeddings store + retrieval. Tests use a deterministic mock backend so
the suite never depends on downloading a real model."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import textwrap
from dataclasses import dataclass
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.embeddings.store import (
    coverage,
    populate,
    search as semantic_search,
)
from code_index.pipeline import reindex


@dataclass
class _MockBackend:
    """Cheap, deterministic 16-d embeddings derived from token hashes.

    Returns vectors that are cosine-close for texts with overlapping tokens
    and far apart for disjoint ones. Good enough for tests of the store
    layer without loading PyTorch / fastembed.
    """

    model_name: str = "mock/hash-16"
    dimension: int = 16
    provider: str = "mock"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self.dimension
            for tok in t.lower().split():
                h = hashlib.sha1(tok.encode()).digest()
                for i in range(self.dimension):
                    vec[i] += h[i] / 128.0 - 1.0
            out.append(vec)
        return out


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_tiny_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "auth.py").write_text(
        textwrap.dedent(
            """
            def verify_jwt_expiry(token):
                \"\"\"Reject tokens whose JWT expiry has passed.\"\"\"
                return True

            def sign_token(payload):
                return "signed"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "fs.py").write_text(
        textwrap.dedent(
            """
            def read_file(path):
                with open(path) as f:
                    return f.read()
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_populate_then_search_ranks_by_semantic_overlap(tmp_path: Path):
    _write_tiny_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        backend = _MockBackend()
        stats = populate(conn, backend)
        assert stats["embedded"] > 0
        assert stats["errors"] == 0

        hits = semantic_search(conn, backend, "jwt expiry token validation", limit=3)
        assert hits, "expected at least one hit"
        # The auth.py symbol should rank above the fs.py symbol — it shares
        # tokens ("jwt", "expiry", "token") with the query.
        top = hits[0]
        assert "auth" in (top["file_path"] or ""), (
            f"expected auth-related top hit, got {top}"
        )
    finally:
        db_mod.close(conn)


def test_coverage_matches_live_chunks(tmp_path: Path):
    _write_tiny_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        cov_before = coverage(conn)
        assert cov_before["embedded_chunks"] == 0
        backend = _MockBackend()
        populate(conn, backend)
        cov_after = coverage(conn)
        assert cov_after["embedded_chunks"] > 0
        assert cov_after["embedded_chunks"] == cov_after["total_chunks"]
        assert cov_after["coverage_pct"] == 100.0
    finally:
        db_mod.close(conn)


def test_populate_is_idempotent_without_refresh(tmp_path: Path):
    _write_tiny_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        backend = _MockBackend()
        first = populate(conn, backend)
        second = populate(conn, backend)
        assert first["embedded"] > 0
        # Second run finds nothing new to embed.
        assert second["embedded"] == 0
    finally:
        db_mod.close(conn)


def test_refresh_drops_prior_rows(tmp_path: Path):
    _write_tiny_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        backend = _MockBackend()
        populate(conn, backend)
        rows = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE provider = 'mock'"
        ).fetchone()[0]
        assert rows > 0
        second = populate(conn, backend, refresh=True)
        assert second["embedded"] == rows
    finally:
        db_mod.close(conn)


def test_embedding_blob_roundtrips():
    """Packed float32 blobs must unpack to the same vector we stored."""
    from code_index.embeddings.store import _pack, _unpack

    vec = [0.1, -0.2, 3.14, -1e-3, 0.0]
    blob = _pack(vec)
    assert len(blob) == 4 * len(vec)
    restored = _unpack(blob, len(vec))
    for a, b in zip(vec, restored):
        assert abs(a - b) < 1e-5


# --- v4 schema hardening regressions ---------------------------------------


def test_schema_has_unique_index_on_embeddings(tmp_path: Path):
    """Regression: v4 must carry a UNIQUE index on (chunk_pk, provider, model).
    Without it, a concurrent populate can race past `INSERT OR REPLACE` and
    leave duplicate rows."""
    _write_tiny_repo(tmp_path)
    _config, conn = _init(tmp_path)
    try:
        sql_rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='embeddings'"
        ).fetchall()
        joined = " ".join((r[0] or "") for r in sql_rows)
        assert "UNIQUE" in joined.upper()
        assert "chunk_pk" in joined
        assert "provider" in joined
        assert "model" in joined
    finally:
        db_mod.close(conn)


def test_populate_stores_embedding_norm(tmp_path: Path):
    """populate() must precompute and persist the L2 norm so search() doesn't
    recompute it per query."""
    _write_tiny_repo(tmp_path)
    _config, conn = _init(tmp_path)
    try:
        reindex(conn, _config, paths=None, event_source="init")
        populate(conn, _MockBackend())
        row = conn.execute("SELECT embedding_norm FROM embeddings LIMIT 1").fetchone()
        assert row is not None, "expected at least one embedding row"
        assert row[0] is not None, "embedding_norm must be populated"
        assert float(row[0]) > 0.0
    finally:
        db_mod.close(conn)


def test_duplicate_embedding_rows_get_deduped_on_migration(tmp_path: Path):
    """Simulate a pre-v4 DB that already contains duplicate embedding rows
    for the same (chunk_pk, provider, model). Running apply_schema must
    dedup them (keeping the newest) before the UNIQUE index is built."""
    import struct as _struct

    _write_tiny_repo(tmp_path)
    _config, conn = _init(tmp_path)
    try:
        reindex(conn, _config, paths=None, event_source="init")
        chunk_pk = conn.execute(
            "SELECT chunk_pk FROM chunks WHERE deleted_at IS NULL LIMIT 1"
        ).fetchone()[0]
        # Drop the unique index so we can insert a duplicate the way a
        # v3 DB would have allowed.
        conn.execute("DROP INDEX IF EXISTS idx_embeddings_chunk_provider_model")
        blob = _struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
        for _ in range(2):
            conn.execute(
                "INSERT INTO embeddings(chunk_pk, provider, model, dimension, "
                "embedding_blob, embedding_norm, updated_at) "
                "VALUES (?, 'dup', 'dup/v0', 4, ?, NULL, '2025-01-01T00:00:00+00:00')",
                (chunk_pk, blob),
            )
        dup_count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE provider='dup' AND model='dup/v0'"
        ).fetchone()[0]
        assert dup_count == 2
        # Force the migration path to run by stamping the version back to v3.
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
            "('schema_version', '3')"
        )
        db_mod.apply_schema(conn)
        post_count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE provider='dup' AND model='dup/v0'"
        ).fetchone()[0]
        assert post_count == 1, "migration must collapse duplicates to one row"
        # And subsequent INSERT OR IGNORE / INSERT with the same key must
        # now fail-or-replace, not duplicate.
        try:
            conn.execute(
                "INSERT INTO embeddings(chunk_pk, provider, model, dimension, "
                "embedding_blob, embedding_norm, updated_at) "
                "VALUES (?, 'dup', 'dup/v0', 4, ?, 1.0, '2025-01-02T00:00:00+00:00')",
                (chunk_pk, blob),
            )
            raised = False
        except db_mod.sqlite3.IntegrityError:
            raised = True
        assert raised, "unique index must reject duplicate inserts"
    finally:
        db_mod.close(conn)


def test_migration_backfills_embedding_norm(tmp_path: Path):
    """A pre-v4 DB with rows missing `embedding_norm` must have the column
    populated during apply_schema."""
    import struct as _struct

    _write_tiny_repo(tmp_path)
    _config, conn = _init(tmp_path)
    try:
        reindex(conn, _config, paths=None, event_source="init")
        chunk_pk = conn.execute(
            "SELECT chunk_pk FROM chunks WHERE deleted_at IS NULL LIMIT 1"
        ).fetchone()[0]
        vec = (0.6, 0.8, 0.0, 0.0)
        blob = _struct.pack("<4f", *vec)
        conn.execute(
            "INSERT INTO embeddings(chunk_pk, provider, model, dimension, "
            "embedding_blob, embedding_norm, updated_at) "
            "VALUES (?, 'bf', 'bf/v0', 4, ?, NULL, '2025-01-01T00:00:00+00:00')",
            (chunk_pk, blob),
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
            "('schema_version', '3')"
        )
        db_mod.apply_schema(conn)
        norm = conn.execute(
            "SELECT embedding_norm FROM embeddings WHERE provider='bf'"
        ).fetchone()[0]
        assert norm is not None
        # sqrt(0.36 + 0.64) == 1.0
        assert abs(float(norm) - 1.0) < 1e-4
    finally:
        db_mod.close(conn)
