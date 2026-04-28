"""Regression: edited chunks must not keep pre-edit embedding vectors.

`populate()` used to skip any chunk that already had an embedding row keyed
by `(chunk_pk, provider, model)` — even when the chunk's content had been
edited since. That left stale vectors pointing at old code until a full
`--refresh` sweep. See plans/slice-10-adversarial-review-fixes.md Task A.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import textwrap
from dataclasses import dataclass
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.embeddings.store import populate
from code_index.pipeline import reindex


@dataclass
class _TokenBackend:
    """Deterministic embedding that is sensitive to content, not just the
    symbol path/signature. Vector = summed per-byte token hash so distinct
    function bodies map to distinct vectors. Symbol name is stable between
    edits, so any staleness shows up as a vector that matches the OLD body.
    """

    model_name: str = "mock/token-32"
    dimension: int = 32
    provider: str = "mock"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self.dimension
            for tok in t.split():
                h = hashlib.sha1(tok.encode("utf-8")).digest()
                for i in range(self.dimension):
                    vec[i] += (h[i % 20] / 128.0) - 1.0
            out.append(vec)
        return out


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _unpack(blob: bytes, dim: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dim}f", blob)


def _write_v1(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    f = tmp_path / "pkg" / "target.py"
    f.write_text(
        textwrap.dedent(
            """
            def my_function():
                alpha = 1
                beta = 2
                return alpha + beta
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return f


def _write_v2(file_path: Path) -> None:
    # Rewrite the SAME function (same symbol_path, same chunk_uid) with a
    # completely different body.  The indexer updates the chunks row in
    # place — chunk_pk does not change.
    file_path.write_text(
        textwrap.dedent(
            """
            def my_function():
                zulu = 99
                yankee = 100
                whiskey = 101
                return zulu * yankee * whiskey
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_embedding_refreshes_when_chunk_content_changes(tmp_path: Path):
    """Index → populate → edit chunk body → reindex → populate again.
    The stored vector must reflect the NEW body, not the pre-edit one."""
    file_path = _write_v1(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        backend = _TokenBackend()
        populate(conn, backend)

        row = conn.execute(
            """
            SELECT e.chunk_pk, e.dimension, e.embedding_blob
              FROM embeddings e
              JOIN chunks c ON c.chunk_pk = e.chunk_pk
             WHERE c.symbol_name = 'my_function'
            """
        ).fetchone()
        assert row is not None, "expected embedding for my_function"
        chunk_pk = int(row["chunk_pk"])
        v1_vec = _unpack(row["embedding_blob"], int(row["dimension"]))

        # What vector SHOULD the populated row hold after the edit? Compute
        # it from the v2 body directly via the backend so we have a ground
        # truth independent of populate().
        _write_v2(file_path)
        reindex(conn, config, paths=[file_path], event_source="update")
        assert chunk_pk == int(
            conn.execute(
                "SELECT chunk_pk FROM chunks WHERE symbol_name = 'my_function'"
            ).fetchone()[0]
        ), "chunk_pk should be stable across in-place updates"

        populate(conn, backend)

        # Reread the stored vector. It must now equal the v2 expectation,
        # not the v1 one.
        row2 = conn.execute(
            """
            SELECT e.dimension, e.embedding_blob
              FROM embeddings e
              JOIN chunks c ON c.chunk_pk = e.chunk_pk
             WHERE c.symbol_name = 'my_function'
            """
        ).fetchone()
        assert row2 is not None
        v2_stored = _unpack(row2["embedding_blob"], int(row2["dimension"]))

        # Ground truth: embed the current chunk text the same way populate does.
        chunk_row = conn.execute(
            "SELECT symbol_path, signature, content FROM chunks WHERE chunk_pk = ?",
            (chunk_pk,),
        ).fetchone()
        from code_index.embeddings.store import _chunk_text_for_embedding

        expected_vec = backend.embed([_chunk_text_for_embedding(chunk_row)])[0]

        # Post-fix: the stored vector is the v2 vector.
        diff_post = max(abs(a - b) for a, b in zip(v2_stored, expected_vec))
        assert diff_post < 1e-5, (
            "stored embedding does not match the post-edit chunk content — "
            "populate() is still returning a stale vector"
        )
        # And it is NOT the v1 vector (sanity — the test only has teeth if
        # v1 and v2 actually produce different vectors).
        diff_versions = max(abs(a - b) for a, b in zip(v1_vec, expected_vec))
        assert diff_versions > 1e-4, "test backend produced identical v1/v2 vectors"
    finally:
        db_mod.close(conn)


def test_stale_count_doctor_metric(tmp_path: Path):
    """doctor --json must surface a `stale_count` field on the embeddings
    block so agents can detect pending re-embed work."""
    from code_index.commands.doctor_cmd import _embeddings_summary

    file_path = _write_v1(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        populate(conn, _TokenBackend())
        clean = _embeddings_summary(conn)
        assert "stale_count" in clean, "doctor must report stale_count"
        assert clean["stale_count"] == 0

        # Simulate content drift by bumping raw_hash on one chunk without
        # going through the pipeline. This models any path that mutates
        # chunks.content without invalidating embeddings (e.g. a future
        # feature) — doctor should flag it as stale.
        conn.execute(
            "UPDATE chunks SET raw_hash = 'drifted-hash' "
            "WHERE symbol_name = 'my_function'"
        )
        dirty = _embeddings_summary(conn)
        assert dirty["stale_count"] >= 1
    finally:
        db_mod.close(conn)


def test_schema_version_is_current(tmp_path: Path):
    _write_v1(tmp_path)
    _config, conn = _init(tmp_path)
    try:
        assert db_mod.get_schema_version(conn) == db_mod.SCHEMA_VERSION
        cols = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}
        assert "content_hash" in cols
    finally:
        db_mod.close(conn)


def test_v4_to_v5_migration_backfills_content_hash(tmp_path: Path):
    """A DB at v4 with existing embedding rows must have `content_hash`
    backfilled from the chunks.raw_hash on upgrade."""
    _write_v1(tmp_path)
    _config, conn = _init(tmp_path)
    try:
        reindex(conn, _config, paths=None, event_source="init")
        populate(conn, _TokenBackend())

        # Force the migration path back to v4: drop the column and stamp
        # the version so apply_schema will re-run v4→v5.
        # (SQLite can drop columns via rebuild; emulate by moving rows.)
        conn.execute("ALTER TABLE embeddings DROP COLUMN content_hash")
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
            "('schema_version', '4')"
        )
        db_mod.apply_schema(conn)

        rows = conn.execute(
            "SELECT e.content_hash, c.raw_hash FROM embeddings e "
            "JOIN chunks c ON c.chunk_pk = e.chunk_pk"
        ).fetchall()
        assert rows, "expected at least one embedding row"
        for eh, ch in rows:
            assert eh == ch, (
                "v4→v5 migration must backfill embeddings.content_hash "
                "from chunks.raw_hash"
            )
    finally:
        db_mod.close(conn)
