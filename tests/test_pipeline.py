import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import reindex
from code_index.search import fts, symbol_search


def _init(tmp_repo: Path):
    config = cfg_mod.load(tmp_repo)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_init_scan_and_idempotent_update(tmp_repo: Path):
    config, conn = _init(tmp_repo)
    try:
        stats = reindex(conn, config, paths=None, event_source="init")
        assert stats.files_parsed >= 2  # pkg/mod.py at minimum + README
        assert stats.chunks_created > 0
        assert stats.edits_recorded == stats.chunks_created
        first_edits = conn.execute("SELECT COUNT(*) FROM chunk_edits").fetchone()[0]
        # Re-run should be a no-op in terms of edits.
        stats2 = reindex(conn, config, paths=None, event_source="update")
        assert stats2.files_unchanged >= 1
        assert stats2.chunks_created == 0
        assert stats2.chunks_updated == 0
        assert stats2.chunks_tombstoned == 0
        second_edits = conn.execute("SELECT COUNT(*) FROM chunk_edits").fetchone()[0]
        assert first_edits == second_edits
    finally:
        db_mod.close(conn)


def test_symbol_and_fts_lookup_after_init(tmp_repo: Path):
    config, conn = _init(tmp_repo)
    try:
        reindex(conn, config, paths=None, event_source="init")
        rows = symbol_search.lookup(conn, "bump")
        names = [r["canonical_name"] for r in rows]
        assert any("Counter.bump" in n for n in names)

        results = fts.search(conn, "greet", limit=5)
        assert results, "fts should find the greet symbol chunk"
        assert any("greet" in (r["symbol_path"] or "") for r in results)
    finally:
        db_mod.close(conn)


def test_edit_produces_edit_row_and_updated_hash(tmp_repo: Path):
    config, conn = _init(tmp_repo)
    try:
        reindex(conn, config, paths=None, event_source="init")
        mod = tmp_repo / "pkg" / "mod.py"
        original = mod.read_text(encoding="utf-8")
        mod.write_text(
            original.replace('return f"{GREETING} {name}"', 'return f"HELLO {name}"'),
            encoding="utf-8",
        )
        stats = reindex(conn, config, paths=[mod], event_source="update")
        assert stats.chunks_updated >= 1
        edits = conn.execute(
            "SELECT change_type FROM chunk_edits ORDER BY edit_pk DESC LIMIT 5"
        ).fetchall()
        assert any(row["change_type"] == "update" for row in edits)
    finally:
        db_mod.close(conn)


def test_file_delete_tombstones_chunks(tmp_repo: Path):
    config, conn = _init(tmp_repo)
    try:
        reindex(conn, config, paths=None, event_source="init")
        (tmp_repo / "pkg" / "mod.py").unlink()
        stats = reindex(conn, config, paths=None, event_source="update")
        assert stats.chunks_tombstoned >= 1
        tombstones = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
        assert tombstones >= 1
    finally:
        db_mod.close(conn)


def test_ignored_files_not_indexed(tmp_repo: Path):
    config, conn = _init(tmp_repo)
    try:
        reindex(conn, config, paths=None, event_source="init")
        rows = conn.execute(
            "SELECT file_path FROM files WHERE deleted_at IS NULL"
        ).fetchall()
        paths = [r["file_path"] for r in rows]
        assert all("ignored_dir" not in p for p in paths)
        assert all(not p.endswith(".log") for p in paths)
    finally:
        db_mod.close(conn)


def test_syntax_error_file_recorded_without_aborting(tmp_repo: Path):
    bad = tmp_repo / "pkg" / "broken.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    config, conn = _init(tmp_repo)
    try:
        stats = reindex(conn, config, paths=None, event_source="init")
        # The run must keep going and index the other files...
        assert stats.files_parsed >= 1
        # ...while still recording the bad file as a parse failure.
        assert stats.files_failed >= 1
        row = conn.execute(
            "SELECT parse_status, parse_error FROM files WHERE file_path = ?",
            ("pkg/broken.py",),
        ).fetchone()
        assert row is not None
        assert row["parse_status"] == "failed"
        assert row["parse_error"] and "SyntaxError" in row["parse_error"]
        diag = conn.execute(
            "SELECT severity FROM diagnostics WHERE file_pk = (SELECT file_pk FROM files WHERE file_path = ?)",
            ("pkg/broken.py",),
        ).fetchone()
        assert diag and diag["severity"] == "error"
    finally:
        db_mod.close(conn)
