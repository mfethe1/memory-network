"""SQLite connection management and schema application."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "11"
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        return conn
    except Exception:
        conn.close()
        raise


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def apply_schema(conn: sqlite3.Connection) -> None:
    if not fts5_available(conn):
        raise RuntimeError(
            "SQLite FTS5 is not available in this Python build. "
            "code_index requires FTS5; install a Python whose bundled SQLite includes FTS5."
        )
    prior_version = get_schema_version(conn)
    _migrate_if_needed(conn, prior_version)
    _repair_expected_columns(conn)
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


def schema_is_ready(conn: sqlite3.Connection) -> bool:
    """Read-only health probe: version matches AND all expected additive
    columns/tables are present. Used by read-only commands to skip the
    `apply_schema` write path in the common case.
    """
    try:
        stored = get_schema_version(conn)
    except sqlite3.OperationalError:
        return False
    if stored != SCHEMA_VERSION:
        return False
    columns_ok, _missing_columns = expected_column_health(conn)
    tables_ok, _missing_tables = expected_table_health(conn)
    return columns_ok and tables_ok


def ensure_schema(conn: sqlite3.Connection, config=None) -> None:
    """Idempotent schema-readiness for read-only callers.

    Fast path: if the DB is already at the current version with every
    additive column/table present, do nothing — no writes, no lock.

    Slow path (stale or partially-corrupt DB): acquire the writer lock
    and run `apply_schema`. Writer lock is required because the repair
    path emits ALTER TABLE / CREATE TABLE IF NOT EXISTS / INSERT OR
    REPLACE statements that must not race with a concurrent reindex.

    If `config` is None (caller can't produce a lock), fall back to
    running `apply_schema` unlocked — legacy behavior, documented as
    unsafe under concurrent writers. Production callers should always
    pass config.
    """
    if schema_is_ready(conn):
        return
    if config is None:
        apply_schema(conn)
        return
    from code_index.locking import LockTimeoutError, writer_lock

    try:
        with writer_lock(config, timeout_s=5.0):
            if schema_is_ready(conn):
                return
            apply_schema(conn)
    except LockTimeoutError:
        # Another writer is mid-reindex; it will leave the schema at the
        # current version. Re-check; if still not ready, surface the
        # error so the caller can decide (most read commands will still
        # succeed because the missing column only affects writers).
        if not schema_is_ready(conn):
            raise


def _migrate_if_needed(conn: sqlite3.Connection, prior: str | None) -> None:
    """Local-first migration policy.

    Schema is reapplied idempotently every connection. When a schema version
    bump changes an existing table's shape, drop the affected tables so the
    CREATE IF NOT EXISTS rewrites them cleanly. Content that can be rederived
    from the parse pipeline (test_edges, unresolved_calls) is safe to drop.
    For non-destructive column additions we ALTER TABLE in place.

    Even when the stored version already matches SCHEMA_VERSION, re-probe the
    additive columns introduced by v3/v4 and repair any that are missing — a
    partially-corrupted DB (interrupted ALTER, manual sqlite edits) can claim
    v4 while still missing `embedding_norm` or the git metadata columns.
    """
    if prior is None:
        _repair_expected_columns(conn)
        return
    if prior == SCHEMA_VERSION:
        _repair_expected_columns(conn)
        return
    if prior == "1":
        # v1 → v2: test_edges shape changed; unresolved_calls is new.
        conn.execute("DROP TABLE IF EXISTS test_edges")
        conn.execute("DROP TABLE IF EXISTS unresolved_calls")
    if prior in ("1", "2"):
        # v2 → v3: add git metadata columns to `files`. Only add if absent
        # (the CREATE TABLE IF NOT EXISTS path already has them on fresh
        # DBs, but ALTER is required for pre-existing DBs).
        _add_column_if_missing(conn, "files", "git_blob_oid", "TEXT")
        _add_column_if_missing(conn, "files", "git_committed_at", "INTEGER")
        _add_column_if_missing(conn, "files", "git_author", "TEXT")
    if prior in ("1", "2", "3"):
        # v3 → v4: harden `embeddings`.
        #   (a) dedup existing rows so the new UNIQUE index can be created;
        #   (b) add `embedding_norm REAL` column;
        #   (c) backfill `embedding_norm` from stored BLOBs in a single pass.
        # Only run when the `embeddings` table actually exists — fresh DBs go
        # straight through `schema.sql` which already has the final shape.
        if _table_exists(conn, "embeddings"):
            _migrate_embeddings_v4(conn)
    if prior in ("1", "2", "3", "4"):
        # v4 → v5: add `embeddings.content_hash` and backfill it from
        # `chunks.raw_hash`. This closes the stale-vector bug where
        # `populate()` skipped any chunk whose embedding row already existed,
        # even when the chunk's content had been edited since.
        if _table_exists(conn, "embeddings"):
            _migrate_embeddings_v5(conn)
        return
    if prior == "5":
        # v5 → v6 only adds append-only agent activity tables. `schema.sql`
        # creates them idempotently; do not drop derived graph/index tables.
        if _table_exists(conn, "agent_runs"):
            _add_column_if_missing(conn, "agent_runs", "archived_at", "TEXT")
        return
    if prior == "6":
        # v6 → v7 adds soft-archive support for graph sidebar agent runs.
        if _table_exists(conn, "agent_runs"):
            _add_column_if_missing(conn, "agent_runs", "archived_at", "TEXT")
        return
    if prior == "7":
        # v7 → v8 adds first-class agent file claims. `schema.sql` creates
        # the new table idempotently; existing activity tables are retained.
        return
    if prior == "8":
        # v8 → v9 adds preflight receipts and lease fencing. Both are additive.
        if _table_exists(conn, "agent_file_claims"):
            _add_column_if_missing(
                conn,
                "agent_file_claims",
                "fence_token",
                "INTEGER NOT NULL DEFAULT 0",
            )
        return
    if prior == "9":
        # v9 → v10 adds first-class run blocker edges for task-board planning.
        return
    if prior == "10":
        # v10 → v11 adds durable file lease metadata and lifecycle events.
        return
    # Unknown older version: safest local policy is to drop the derived tables
    # and let the next reindex rebuild them. Canonical tables survive.
    for t in ("test_edges", "unresolved_calls"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")


# Additive columns introduced after v1 — each entry is re-probed on every
# `apply_schema` call so a partial migration can self-heal. (Table, column,
# type.) Keep in sync with `schema.sql`.
_EXPECTED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("files", "git_blob_oid", "TEXT"),
    ("files", "git_committed_at", "INTEGER"),
    ("files", "git_author", "TEXT"),
    ("embeddings", "embedding_norm", "REAL"),
    ("embeddings", "content_hash", "TEXT"),
    ("agent_runs", "archived_at", "TEXT"),
    ("agent_file_claims", "fence_token", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_file_claims", "lease_token_hash", "TEXT"),
    ("agent_file_claims", "lease_kind", "TEXT NOT NULL DEFAULT 'claim'"),
    ("agent_file_claims", "owner_agent", "TEXT"),
    ("agent_file_claims", "heartbeat_interval_ms", "INTEGER"),
    ("agent_file_claims", "conflict_policy", "TEXT"),
    ("agent_file_claims", "last_conflict_json", "TEXT"),
)

_EXPECTED_TABLES: tuple[str, ...] = (
    "agent_runs",
    "agent_events",
    "agent_file_claims",
    "agent_file_claim_events",
    "agent_task_preflights",
    "agent_run_blockers",
)


def expected_column_health(
    conn: sqlite3.Connection,
) -> tuple[bool, list[str]]:
    """Return (ok, missing) for the additive columns the schema expects.

    A column on a non-existent table is not reported as missing — the table
    itself is the failure mode, and the next `apply_schema(conn)` will
    recreate it via `CREATE TABLE IF NOT EXISTS`.
    """
    missing: list[str] = []
    for table, column, _ in _EXPECTED_COLUMNS:
        if not _table_exists(conn, table):
            continue
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            missing.append(f"{table}.{column}")
    return (not missing, missing)


def expected_table_health(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """Return (ok, missing) for additive tables the schema expects.

    A same-version DB can be missing an additive table after an interrupted
    upgrade or manual repair. `schema_is_ready()` checks this so read commands
    can fall back to the locked `apply_schema()` repair path.
    """
    missing = [table for table in _EXPECTED_TABLES if not _table_exists(conn, table)]
    return (not missing, missing)


def _repair_expected_columns(conn: sqlite3.Connection) -> None:
    """Add any additive column that a partial migration dropped on the floor."""
    for table, column, col_type in _EXPECTED_COLUMNS:
        if not _table_exists(conn, table):
            continue
        _add_column_if_missing(conn, table, column, col_type)
    # Backfill embeddings.content_hash from chunks.raw_hash for any row the
    # v5 migration may have missed (column present but NULL).
    if _table_exists(conn, "embeddings"):
        try:
            conn.execute(
                """
                UPDATE embeddings
                   SET content_hash = (
                       SELECT c.raw_hash
                         FROM chunks c
                        WHERE c.chunk_pk = embeddings.chunk_pk
                   )
                 WHERE content_hash IS NULL
                """
            )
        except sqlite3.OperationalError as exc:
            if "no such" not in str(exc).lower():
                raise


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_embeddings_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4 embeddings migration.

    Keeps the row with the largest `embedding_pk` for each
    (chunk_pk, provider, model) triple (that's the most recent insert under
    our always-autoincrement PK). Anything older is a duplicate and is
    dropped so the new UNIQUE index can be created.
    """
    # (a) Dedup. Delete every row whose (chunk_pk, provider, model) has a
    # newer companion.
    conn.execute(
        """
        DELETE FROM embeddings
         WHERE embedding_pk NOT IN (
             SELECT MAX(embedding_pk)
               FROM embeddings
              GROUP BY chunk_pk, provider, model
         )
        """
    )
    # (b) Add the column if a previous partial migration didn't.
    _add_column_if_missing(conn, "embeddings", "embedding_norm", "REAL")
    # (c) Backfill the norm for every row that is missing it. We compute the
    # L2 norm in SQL by unpacking float32 blobs with a recursive CTE — cheaper
    # than shuffling every blob through Python.
    # SQLite can't unpack float32 without an extension, so fall back to a
    # Python loop. This only runs once per DB on upgrade, so the cost is OK.
    import struct as _struct

    cur = conn.execute(
        "SELECT embedding_pk, dimension, embedding_blob FROM embeddings "
        "WHERE embedding_norm IS NULL AND embedding_blob IS NOT NULL"
    )
    updates: list[tuple[float, int]] = []
    for pk, dim, blob in cur.fetchall():
        if not blob or not dim:
            continue
        try:
            vec = _struct.unpack(f"<{int(dim)}f", blob)
        except _struct.error:
            continue
        n = 0.0
        for x in vec:
            n += x * x
        updates.append((n**0.5 or 1.0, int(pk)))
    if updates:
        conn.executemany(
            "UPDATE embeddings SET embedding_norm = ? WHERE embedding_pk = ?",
            updates,
        )


def _migrate_embeddings_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5 embeddings migration.

    Adds `content_hash TEXT` and backfills it from `chunks.raw_hash` so that
    `populate()` can detect drift without having to re-embed every row on
    the first post-upgrade run. Any row whose chunk has since been
    tombstoned or hard-deleted is left with a NULL `content_hash` and will
    be cleaned up by the next `populate(..., refresh=True)` or by normal
    chunk-level cascade on delete.
    """
    _add_column_if_missing(conn, "embeddings", "content_hash", "TEXT")
    conn.execute(
        """
        UPDATE embeddings
           SET content_hash = (
               SELECT c.raw_hash
                 FROM chunks c
                WHERE c.chunk_pk = embeddings.chunk_pk
           )
         WHERE content_hash IS NULL
        """
    )


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    existing = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def get_schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        raise sqlite3.ProgrammingError("Nested transactions are not supported")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def optimize(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.OperationalError:
        # PRAGMA optimize can fail with "database is locked" when another
        # connection holds the write lock, or with "not within a transaction".
        # Both are benign on close.
        pass


def close(conn: sqlite3.Connection) -> None:
    try:
        optimize(conn)
    finally:
        conn.close()


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
