"""Migration discipline: `_migrate_if_needed` self-heals when the stored
schema version matches SCHEMA_VERSION but an additive column is missing,
and `doctor --json` surfaces the repair via a `schema_health` block."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands import doctor_cmd


def _init_index(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_apply_schema_repairs_dropped_additive_column(tmp_path: Path):
    """If a column from a prior additive migration is missing but the
    stored version already matches SCHEMA_VERSION (simulating a partial
    corruption), re-running `apply_schema` adds it back."""
    config, conn = _init_index(tmp_path)
    try:
        # Sanity: the column is there after a fresh init.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
        assert "embedding_norm" in cols

        # Force-drop the column. SQLite 3.35+ supports DROP COLUMN; skip
        # gracefully if the host Python ships a pre-3.35 SQLite.
        try:
            conn.execute("ALTER TABLE embeddings DROP COLUMN embedding_norm")
        except Exception:
            import pytest

            pytest.skip("SQLite < 3.35 — DROP COLUMN not available")

        cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
        assert "embedding_norm" not in cols, "column should be gone after drop"

        # Stored version still matches — the corruption is invisible to
        # the version check.
        assert db_mod.get_schema_version(conn) == db_mod.SCHEMA_VERSION

        # Re-apply schema. The same-version path must still run the
        # additive-column repair.
        db_mod.apply_schema(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
        assert "embedding_norm" in cols, "apply_schema should have repaired the column"
    finally:
        db_mod.close(conn)


def test_expected_column_health_reports_missing(tmp_path: Path):
    config, conn = _init_index(tmp_path)
    try:
        ok, missing = db_mod.expected_column_health(conn)
        assert ok is True
        assert missing == []

        try:
            conn.execute("ALTER TABLE files DROP COLUMN git_blob_oid")
        except Exception:
            import pytest

            pytest.skip("SQLite < 3.35 — DROP COLUMN not available")

        ok, missing = db_mod.expected_column_health(conn)
        assert ok is False
        assert "files.git_blob_oid" in missing
    finally:
        db_mod.close(conn)


def test_doctor_json_includes_schema_health(tmp_path: Path):
    config, conn = _init_index(tmp_path)
    db_mod.close(conn)

    args = argparse.Namespace(root=str(tmp_path), json=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = doctor_cmd.run(args)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "schema_health" in payload
    sh = payload["schema_health"]
    assert sh["version"] == db_mod.SCHEMA_VERSION
    assert sh["expected"] == db_mod.SCHEMA_VERSION
    assert sh["columns_ok"] is True
    assert sh["missing"] == []


def test_ensure_schema_fast_path_writes_nothing(tmp_path: Path):
    """On a healthy DB, `ensure_schema` must not emit any write SQL.
    Guards against the regression where read commands were running the
    full `apply_schema` write path (ALTER/CREATE/INSERT) outside the
    writer lock.

    Uses SQLite's `set_trace_callback` to observe every statement the
    connection executes.
    """
    config, conn = _init_index(tmp_path)
    try:
        assert db_mod.schema_is_ready(conn) is True

        write_verbs = (
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "ALTER",
            "DROP",
            "REPLACE",
        )
        writes: list[str] = []

        def _trace(stmt: str) -> None:
            head = stmt.strip().split(None, 1)[0].upper() if stmt.strip() else ""
            if head in write_verbs:
                writes.append(stmt.strip().splitlines()[0][:100])

        conn.set_trace_callback(_trace)
        try:
            db_mod.ensure_schema(conn, config)
        finally:
            conn.set_trace_callback(None)

        assert writes == [], (
            f"ensure_schema should not write on healthy DB, got: {writes}"
        )
    finally:
        db_mod.close(conn)


def test_ensure_schema_slow_path_takes_writer_lock(tmp_path: Path, monkeypatch):
    """When repair is needed, `ensure_schema` must acquire the writer lock
    before running `apply_schema`.
    """
    config, conn = _init_index(tmp_path)
    try:
        try:
            conn.execute("ALTER TABLE embeddings DROP COLUMN embedding_norm")
        except Exception:
            import pytest

            pytest.skip("SQLite < 3.35 — DROP COLUMN not available")
        assert db_mod.schema_is_ready(conn) is False

        lock_calls: list[tuple] = []
        import code_index.locking as locking

        real_lock = locking.writer_lock

        def _tracker(cfg, **kw):
            lock_calls.append((str(cfg.lock_path), kw))
            return real_lock(cfg, **kw)

        monkeypatch.setattr("code_index.locking.writer_lock", _tracker)
        # ensure_schema imports writer_lock inside the function body, so
        # patch the source module — the late import picks up the patch.
        db_mod.ensure_schema(conn, config)

        assert lock_calls, "slow path must acquire the writer lock before writing"
        # And the repair actually happened.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
        assert "embedding_norm" in cols
    finally:
        db_mod.close(conn)


def test_read_commands_call_apply_schema(tmp_path: Path):
    """Smoke test: a `symbol` command against a DB with a missing column
    still succeeds — because the command runs `apply_schema` at startup,
    which repairs the shape before any query hits the column."""
    config, conn = _init_index(tmp_path)
    try:
        try:
            conn.execute("ALTER TABLE files DROP COLUMN git_blob_oid")
        except Exception:
            import pytest

            pytest.skip("SQLite < 3.35 — DROP COLUMN not available")
        db_mod.close(conn)

        from code_index.commands import symbol_cmd

        args = argparse.Namespace(
            root=str(tmp_path),
            name="nonexistent_symbol_xyz",
            kind=None,
            lang=None,
            limit=10,
            json=True,
            references=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = symbol_cmd.run(args)
        # rc==0 with empty results is the expected shape.
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["query"] == "nonexistent_symbol_xyz"
        assert payload["results"] == []

        # And the column is back after the command ran.
        conn2 = db_mod.connect(config.db_path)
        try:
            cols = {r[1] for r in conn2.execute("PRAGMA table_info(files)")}
            assert "git_blob_oid" in cols
        finally:
            db_mod.close(conn2)
    finally:
        pass
