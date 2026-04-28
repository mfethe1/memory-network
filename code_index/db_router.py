"""Public routing layer for SQLite connection lifecycle.

The storage implementation lives in :mod:`code_index.db_core`. Importing this
module from commands keeps the graph pointed at a small lifecycle facade while
preserving the legacy :mod:`code_index.db` module for compatibility.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal

from code_index import db_core as _db

SchemaMode = Literal["none", "ensure", "apply"]

SCHEMA_FILE = _db.SCHEMA_FILE
SCHEMA_VERSION = _db.SCHEMA_VERSION

connect = _db.connect
fts5_available = _db.fts5_available
apply_schema = _db.apply_schema
schema_is_ready = _db.schema_is_ready
ensure_schema = _db.ensure_schema
expected_column_health = _db.expected_column_health
expected_table_health = _db.expected_table_health
get_schema_version = _db.get_schema_version
transaction = _db.transaction
optimize = _db.optimize
close = _db.close
rebuild_fts = _db.rebuild_fts


def prepare_schema(
    conn: sqlite3.Connection,
    *,
    schema: SchemaMode = "ensure",
    config: Any = None,
) -> None:
    """Apply the requested schema policy for a routed connection."""
    if schema == "none":
        return
    if schema == "ensure":
        ensure_schema(conn, config)
        return
    if schema == "apply":
        apply_schema(conn)
        return
    raise ValueError(f"unsupported schema mode: {schema!r}")


@contextmanager
def open_connection(
    db_path: Path,
    *,
    schema: SchemaMode = "ensure",
    config: Any = None,
) -> Iterator[sqlite3.Connection]:
    """Open a database connection, prepare schema if requested, and close it."""
    conn = connect(db_path)
    try:
        prepare_schema(conn, schema=schema, config=config)
        yield conn
    finally:
        close(conn)


@contextmanager
def open_config(
    config: Any,
    *,
    schema: SchemaMode = "ensure",
) -> Iterator[sqlite3.Connection]:
    """Open the database referenced by a loaded Config-like object."""
    with open_connection(config.db_path, schema=schema, config=config) as conn:
        yield conn


__all__ = [
    "SCHEMA_FILE",
    "SCHEMA_VERSION",
    "SchemaMode",
    "apply_schema",
    "close",
    "connect",
    "ensure_schema",
    "expected_column_health",
    "expected_table_health",
    "fts5_available",
    "get_schema_version",
    "open_config",
    "open_connection",
    "optimize",
    "prepare_schema",
    "rebuild_fts",
    "schema_is_ready",
    "transaction",
]
