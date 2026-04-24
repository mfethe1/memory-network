"""Targeted-update performance paths: mtime short-circuit, conditional
backfill, and scoped test_edges rebuild."""

from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import reindex


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_repo(tmp_path: Path, n_modules: int = 30) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    for i in range(n_modules):
        (tmp_path / "pkg" / f"mod{i}.py").write_text(
            f"def fn{i}(x):\n    return x + {i}\n", encoding="utf-8"
        )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_mod0.py").write_text(
        textwrap.dedent(
            """
            from pkg.mod0 import fn0
            def test_fn0():
                assert fn0(1) == 1
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_mtime_short_circuit_skips_unchanged(tmp_path: Path):
    """A `update --files X` on an unchanged file must skip the read+hash
    entirely, counting as `files_unchanged` without doing any SQL writes."""
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        target = tmp_path / "pkg" / "mod0.py"
        # Force a known-state baseline.
        stats = reindex(conn, config, paths=[target], event_source="update")
        assert stats.files_unchanged == 1
        assert stats.files_parsed == 0
        assert stats.chunks_created == 0
        assert stats.chunks_updated == 0
    finally:
        db_mod.close(conn)


def test_backfill_skipped_when_no_topology_change(tmp_path: Path):
    """Targeted update that doesn't add/tombstone any symbol should set
    `relations_backfill_skipped=True`."""
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        target = tmp_path / "pkg" / "mod0.py"
        # Add a comment (whitespace-insensitive change — no new symbols).
        src = target.read_text(encoding="utf-8")
        target.write_text(src + "\n# trivial\n", encoding="utf-8")
        stats = reindex(conn, config, paths=[target], event_source="update")
        # Parsed exactly one file; no symbols added/tombstoned.
        assert stats.files_parsed == 1
        assert stats.symbols_tombstoned == 0
        assert stats.relations_backfill_skipped is True
    finally:
        db_mod.close(conn)


def test_backfill_runs_when_new_symbol_appears(tmp_path: Path):
    """Adding a new function in a targeted file must trigger backfill so
    previously unresolved cross-file calls can heal."""
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        target = tmp_path / "pkg" / "mod0.py"
        src = target.read_text(encoding="utf-8")
        target.write_text(src + "\ndef brand_new():\n    return 99\n", encoding="utf-8")
        stats = reindex(conn, config, paths=[target], event_source="update")
        assert stats.files_parsed == 1
        assert stats.relations_backfill_skipped is False
    finally:
        db_mod.close(conn)


def test_noop_full_scan_skips_backfill_and_test_edge_rebuild(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        stats = reindex(conn, config, paths=None, event_source="update")
        assert stats.files_unchanged > 0
        assert stats.relations_backfill_skipped is True
        assert stats.test_edges_rebuilt_scope == "scoped"
        assert stats.test_edges_removed == 0
        assert stats.test_edges_inserted == 0
    finally:
        db_mod.close(conn)


def test_targeted_backfill_rebuilds_affected_tests_scoped(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "service.py").write_text(
        textwrap.dedent(
            """
            from pkg.util import helper

            def run():
                return helper(1)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(
        textwrap.dedent(
            """
            from pkg.service import run

            def test_run():
                assert run() == 2
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        util = tmp_path / "pkg" / "util.py"
        util.write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")

        stats = reindex(conn, config, paths=[util], event_source="update")

        assert stats.relations_backfilled >= 1
        assert stats.test_edges_rebuilt_scope == "scoped"
        helper_edge = conn.execute(
            """
            SELECT 1
              FROM test_edges te
              JOIN symbols test_s ON test_s.symbol_pk = te.test_symbol_pk
              JOIN symbols target_s ON target_s.symbol_pk = te.target_symbol_pk
             WHERE test_s.canonical_name = 'tests.test_service.test_run'
               AND target_s.canonical_name = 'pkg.util.helper'
            """
        ).fetchone()
        assert helper_edge is not None
    finally:
        db_mod.close(conn)


def test_targeted_delete_rebuilds_affected_tests_scoped(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        target = tmp_path / "pkg" / "mod0.py"
        target.unlink()

        stats = reindex(conn, config, paths=[target], event_source="update")

        assert stats.test_edges_rebuilt_scope == "scoped"
        stale_edge = conn.execute(
            """
            SELECT 1
              FROM test_edges te
              JOIN symbols target_s ON target_s.symbol_pk = te.target_symbol_pk
             WHERE target_s.canonical_name = 'pkg.mod0.fn0'
            """
        ).fetchone()
        assert stale_edge is None
    finally:
        db_mod.close(conn)


def test_scoped_test_edges_rebuild_preserves_untouched_edges(tmp_path: Path):
    """Scoped test_edges rebuild must only touch edges whose test symbol or
    target lives in one of the touched files."""
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        before = set(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT test_symbol_pk, target_symbol_pk FROM test_edges"
            ).fetchall()
        )
        # Touch an unrelated module (mod5) — no test references it.
        target = tmp_path / "pkg" / "mod5.py"
        src = target.read_text(encoding="utf-8")
        target.write_text(src + "\n# touched\n", encoding="utf-8")
        stats = reindex(conn, config, paths=[target], event_source="update")
        assert stats.test_edges_rebuilt_scope == "scoped"
        after = set(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT test_symbol_pk, target_symbol_pk FROM test_edges"
            ).fetchall()
        )
        # Edges untouched by the scope must be identical.
        assert before == after
    finally:
        db_mod.close(conn)


def test_rebuild_tests_cli_produces_full_rebuild(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    import json, subprocess, sys

    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parent.parent)
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "rebuild-tests",
            "--root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["scope"] == "full"
    assert payload["edges_inserted"] >= 1


def test_noop_update_under_200ms_on_small_repo(tmp_path: Path):
    """Performance gate: a no-op `update --files` on a small repo must
    complete in well under 200 ms. Allows ±20% headroom on slow CI."""
    _write_repo(tmp_path, n_modules=10)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        target = tmp_path / "pkg" / "mod0.py"
        # Warm the DB cache.
        reindex(conn, config, paths=[target], event_source="update")
        t0 = time.perf_counter()
        stats = reindex(conn, config, paths=[target], event_source="update")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert stats.files_unchanged == 1
        assert elapsed_ms < 240, f"no-op update took {elapsed_ms:.1f}ms"
    finally:
        db_mod.close(conn)
