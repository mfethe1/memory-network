"""Compatibility exports for SQLite database helpers.

New application code should prefer :mod:`code_index.db_router`, which routes
callers through a smaller lifecycle API. This module remains as the stable
legacy import path for direct storage tests and third-party callers.
"""

from __future__ import annotations

from code_index.db_core import (
    SCHEMA_FILE,
    SCHEMA_VERSION,
    _add_column_if_missing,
    _apply_pragmas,
    _migrate_embeddings_v4,
    _migrate_embeddings_v5,
    _migrate_if_needed,
    _repair_expected_columns,
    _table_exists,
    apply_schema,
    close,
    connect,
    ensure_schema,
    expected_column_health,
    expected_table_health,
    fts5_available,
    get_schema_version,
    optimize,
    rebuild_fts,
    schema_is_ready,
    transaction,
)

__all__ = [
    "SCHEMA_FILE",
    "SCHEMA_VERSION",
    "apply_schema",
    "close",
    "connect",
    "ensure_schema",
    "expected_column_health",
    "expected_table_health",
    "fts5_available",
    "get_schema_version",
    "optimize",
    "rebuild_fts",
    "schema_is_ready",
    "transaction",
]
