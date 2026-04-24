"""FTS maintenance: drift detection + rebuild-fts."""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands.doctor_cmd import _fts_consistency
from code_index.commands.rebuild_fts_cmd import _rebuild
from code_index.pipeline import reindex


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _tiny_repo(tmp_path: Path, extra: bool = False) -> None:
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "def alpha():\n    return 1\n", encoding="utf-8"
    )
    if extra:
        (tmp_path / "pkg" / "b.py").write_text(
            "def beta():\n    return 2\n", encoding="utf-8"
        )


def test_fresh_index_has_zero_drift(tmp_path: Path):
    _tiny_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        report = _fts_consistency(conn)
        assert report["ok"] is True
        assert report["drift"] == 0
        assert report["live_chunks"] == report["fts_indexed_documents"]
    finally:
        db_mod.close(conn)


def test_ok_flag_aligns_with_rebuild_recommendation(tmp_path: Path):
    """`fts_consistency.ok` must flip to False only when a rebuild is
    actually recommended — a small amount of drift below the threshold is
    benign because `query` filters tombstoned chunks via SQL."""
    _tiny_repo(tmp_path, extra=True)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # Delete one file to introduce a tiny drift.
        (tmp_path / "pkg" / "b.py").unlink()
        reindex(conn, config, paths=None, event_source="update")
        report = _fts_consistency(conn)
        # Drift exists but is below threshold (only 1 chunk).
        assert report["drift"] >= 1
        assert report["rebuild_recommended"] is False
        # The `ok` flag should therefore stay True.
        assert report["ok"] is True
    finally:
        db_mod.close(conn)


def test_drift_detected_after_file_delete(tmp_path: Path):
    _tiny_repo(tmp_path, extra=True)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        live_before = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
        ).fetchone()[0]
        (tmp_path / "pkg" / "b.py").unlink()
        reindex(conn, config, paths=None, event_source="update")
        live_after = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
        ).fetchone()[0]
        assert live_after < live_before

        report = _fts_consistency(conn)
        # The indexed count still reflects deleted chunks — that is the drift.
        assert report["drift"] > 0
        # Below the rebuild threshold, drift is benign and `ok` stays True.
        # Above the threshold, rebuild is recommended and `ok` flips to False.
        # Use the signal we actually care about:
        assert report["ok"] == (not report["rebuild_recommended"])
    finally:
        db_mod.close(conn)


def test_rebuild_fts_prunes_drift(tmp_path: Path):
    _tiny_repo(tmp_path, extra=True)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        (tmp_path / "pkg" / "b.py").unlink()
        reindex(conn, config, paths=None, event_source="update")
        before = _fts_consistency(conn)
        assert before["drift"] > 0

        result = _rebuild(conn)
        assert result["ok"] is True
        assert result["drift_after"] == 0
        assert result["indexed_after"] == result["live_chunks"]

        # Doctor agrees.
        after = _fts_consistency(conn)
        assert after["ok"] is True
        assert after["drift"] == 0
    finally:
        db_mod.close(conn)


def test_fts_search_still_works_after_rebuild(tmp_path: Path):
    _tiny_repo(tmp_path, extra=True)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        (tmp_path / "pkg" / "b.py").unlink()
        reindex(conn, config, paths=None, event_source="update")
        _rebuild(conn)
        # After rebuild, alpha is still findable, beta is not.
        rows = conn.execute(
            """
            SELECT c.symbol_name
              FROM chunks_fts JOIN chunks c ON c.chunk_pk = chunks_fts.rowid
             WHERE chunks_fts MATCH 'alpha'
               AND c.deleted_at IS NULL
            """
        ).fetchall()
        names = [r[0] for r in rows]
        assert any("alpha" in n for n in names)
        rows = conn.execute(
            """
            SELECT c.symbol_name
              FROM chunks_fts JOIN chunks c ON c.chunk_pk = chunks_fts.rowid
             WHERE chunks_fts MATCH 'beta'
               AND c.deleted_at IS NULL
            """
        ).fetchall()
        assert rows == []
    finally:
        db_mod.close(conn)
