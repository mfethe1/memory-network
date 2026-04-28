"""`code_index rebuild-fts`: prune tombstone drift in the FTS index.

Live chunks (where `deleted_at IS NULL`) are the source of truth. The external
content FTS5 table (`chunks_fts`) accumulates stale rows when chunks are
tombstoned by soft-delete. This command rebuilds the FTS index from live
chunks so drift disappears and `bm25()` weighting stays accurate.

Strategy:
  1. Capture live + FTS row counts before the rebuild.
  2. `INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')` clears the
     index without dropping the virtual table.
  3. Reinsert only live chunks from the canonical table.
  4. Report counts + drift delta as JSON.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.db_router import transaction


_FTS_CREATE_DDL = """
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    symbol_name,
    symbol_path,
    signature,
    file_path,
    content,
    content='chunks',
    content_rowid='chunk_pk',
    tokenize='unicode61 remove_diacritics 2'
)
"""

_TRIGGER_AI = """
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES (new.chunk_pk,
            COALESCE(new.symbol_name, ''),
            COALESCE(new.symbol_path, ''),
            COALESCE(new.signature, ''),
            COALESCE(new.file_path, ''),
            COALESCE(new.content, ''));
END
"""

_TRIGGER_AD = """
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES ('delete', old.chunk_pk,
            COALESCE(old.symbol_name, ''),
            COALESCE(old.symbol_path, ''),
            COALESCE(old.signature, ''),
            COALESCE(old.file_path, ''),
            COALESCE(old.content, ''));
END
"""

_TRIGGER_AU = """
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES ('delete', old.chunk_pk,
            COALESCE(old.symbol_name, ''),
            COALESCE(old.symbol_path, ''),
            COALESCE(old.signature, ''),
            COALESCE(old.file_path, ''),
            COALESCE(old.content, ''));
    INSERT INTO chunks_fts(rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES (new.chunk_pk,
            COALESCE(new.symbol_name, ''),
            COALESCE(new.symbol_path, ''),
            COALESCE(new.signature, ''),
            COALESCE(new.file_path, ''),
            COALESCE(new.content, ''));
END
"""


def _indexed_count(conn) -> int | None:
    import sqlite3 as _sql

    try:
        return conn.execute("SELECT COUNT(*) FROM chunks_fts_docsize").fetchone()[0]
    except _sql.OperationalError:
        return None


def _drift_count(conn) -> int:
    import sqlite3 as _sql

    try:
        return conn.execute(
            """
            SELECT COUNT(*) FROM chunks_fts_docsize d
              LEFT JOIN chunks c
                ON c.chunk_pk = d.id AND c.deleted_at IS NULL
             WHERE c.chunk_pk IS NULL
            """
        ).fetchone()[0]
    except _sql.OperationalError:
        return 0


def _rebuild(conn) -> dict:
    live = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
    ).fetchone()[0]
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    indexed_before = _indexed_count(conn)
    drift_before = _drift_count(conn)

    with transaction(conn):
        # External-content FTS5 does not support 'delete-all'. The reliable
        # pattern is drop + recreate + repopulate from the canonical table.
        # Drop the associated triggers first so the CREATE TRIGGER below
        # doesn't collide.
        conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
        conn.execute("DROP TRIGGER IF EXISTS chunks_au")
        conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.execute(_FTS_CREATE_DDL)
        conn.execute(_TRIGGER_AI)
        conn.execute(_TRIGGER_AD)
        conn.execute(_TRIGGER_AU)
        conn.execute(
            """
            INSERT INTO chunks_fts(rowid, symbol_name, symbol_path, signature, file_path, content)
            SELECT chunk_pk,
                   COALESCE(symbol_name, ''),
                   COALESCE(symbol_path, ''),
                   COALESCE(signature, ''),
                   COALESCE(file_path, ''),
                   COALESCE(content, '')
              FROM chunks
             WHERE deleted_at IS NULL
            """
        )

    indexed_after = _indexed_count(conn)
    drift_after = _drift_count(conn)
    return {
        "live_chunks": live,
        "total_chunks": total_chunks,
        "tombstoned_chunks": total_chunks - live,
        "indexed_before": indexed_before,
        "indexed_after": indexed_after,
        "drift_before": drift_before,
        "drift_after": drift_after,
        "ok": drift_after == 0 and indexed_after == live,
    }


def run(args: argparse.Namespace) -> int:
    from code_index.locking import LockTimeoutError, writer_lock

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                report = _rebuild(conn)
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        payload = {"error": str(exc), "lock_path": str(exc.lock_path)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {exc}")
        return 3
    report["root"] = str(config.root)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(
        f"rebuild-fts: live={report['live_chunks']} "
        f"tombstoned={report['tombstoned_chunks']} "
        f"indexed {report['indexed_before']} → {report['indexed_after']} "
        f"(drift {report['drift_before']} → {report['drift_after']})"
    )
    print("  status: " + ("ok" if report["ok"] else "drift remains"))
    return 0
