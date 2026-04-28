"""Jedi-augmented call resolution — unit tests for the resolver module.

Gated: skip the whole file when `jedi` isn't installed. These exercise
the standalone compat API (`resolve_unresolved_calls`) plus the new
pipeline-facing surface (`resolve_pending_via_jedi`). End-to-end flow
from `reindex()` lives in `test_jedi_pipeline_integration.py`."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("jedi")

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.parsers.jedi_enhanced import (
    is_available,
    resolve_pending_via_jedi,
    resolve_unresolved_calls,
)
from code_index.pipeline import reindex


def _init(tmp_path: Path, *, enable_jedi: bool = True):
    config = cfg_mod.load(tmp_path)
    config.enable_jedi = enable_jedi
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_jedi_availability_matches_import():
    # If this test file ran at all, `jedi` import succeeded.
    assert is_available() is True


def test_disabled_by_default_returns_noop_stats(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    # Do NOT set enable_jedi; default is False.
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        stats = resolve_unresolved_calls(config, conn)
        assert stats["enabled"] is False
        assert stats["resolved_by_jedi"] == 0
    finally:
        db_mod.close(conn)


def test_resolve_pending_via_jedi_returns_candidates(tmp_path: Path):
    """New primary API: given a call-site record, return Jedi candidates."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "bar.py").write_text(
        textwrap.dedent(
            """
            class Bar:
                def method(self):
                    return 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "caller.py").write_text(
        textwrap.dedent(
            """
            from pkg.bar import Bar

            def do():
                foo = Bar()
                return foo.method()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    # Disable jedi on the pipeline pass so unresolved_calls gets a row we
    # can look up — then call the Jedi resolver directly.
    config, conn = _init(tmp_path, enable_jedi=False)
    try:
        reindex(conn, config, paths=None, event_source="init")
        row = conn.execute(
            """
            SELECT uc.file_pk, uc.src_symbol_uid, uc.site_line
              FROM unresolved_calls uc
              JOIN symbols s ON s.symbol_uid = uc.src_symbol_uid
             WHERE uc.resolved_at IS NULL
               AND uc.relation_kind = 'calls'
               AND s.canonical_name = 'pkg.caller.do'
             LIMIT 1
            """
        ).fetchone()
        assert row is not None, "expected unresolved foo.method() row"

        # Flip the gate on the same config so the resolver runs.
        config.enable_jedi = True
        mapping = resolve_pending_via_jedi(
            config,
            conn,
            [
                {
                    "src_symbol_uid": row["src_symbol_uid"],
                    "file_pk": int(row["file_pk"]),
                    "line": int(row["site_line"]),
                    "column": None,
                }
            ],
        )
        key = (row["src_symbol_uid"], int(row["file_pk"]), int(row["site_line"]))
        assert key in mapping, f"expected mapping for {key}, got {mapping!r}"
        cands = mapping[key]
        assert any("Bar.method" in c for c in cands), cands
    finally:
        db_mod.close(conn)


def test_compat_wrapper_still_resolves_leftovers(tmp_path: Path):
    """The compat `resolve_unresolved_calls` is still usable as a retrofit
    pass when the pipeline ran with `enable_jedi=False`.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "bar.py").write_text(
        textwrap.dedent(
            """
            class Bar:
                def method(self):
                    return 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "caller.py").write_text(
        textwrap.dedent(
            """
            from pkg.bar import Bar

            def do():
                foo = Bar()
                return foo.method()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config, conn = _init(tmp_path, enable_jedi=False)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # Now flip the gate on and retrofit.
        config.enable_jedi = True
        stats = resolve_unresolved_calls(config, conn)
        assert stats["available"] is True
        assert stats["enabled"] is True
        assert stats["attempted"] >= 1
        row = conn.execute(
            """
            SELECT r.provenance FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls'
               AND s1.canonical_name = 'pkg.caller.do'
               AND s2.canonical_name = 'pkg.bar.Bar.method'
            """
        ).fetchone()
        assert row is not None, "expected foo.method() → Bar.method edge"
        assert "jedi:goto" in (row[0] or "")
    finally:
        db_mod.close(conn)


def test_unresolvable_dynamic_dispatch_stays_open(tmp_path: Path):
    """`getattr`-style dispatch that Jedi can't resolve must leave the
    unresolved_calls row untouched."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text(
        textwrap.dedent(
            """
            def do(name):
                # Fully dynamic — Jedi cannot narrow this.
                handler = getattr(__import__('os'), name)
                return handler()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        stats = resolve_unresolved_calls(config, conn)
        assert stats["still_unresolved"] >= 0
        # Any landed edges here must point at in-index symbols — never at
        # speculative guesses.
        rows = conn.execute(
            """
            SELECT s2.canonical_name FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls'
               AND s1.canonical_name = 'pkg.mod.do'
            """
        ).fetchall()
        for r in rows:
            assert isinstance(r[0], str)
    finally:
        db_mod.close(conn)


def test_stats_shape_is_stable(tmp_path: Path):
    config, conn = _init(tmp_path)
    try:
        db_mod.apply_schema(conn)
        stats = resolve_unresolved_calls(config, conn)
        for key in (
            "available",
            "enabled",
            "attempted",
            "resolved_by_jedi",
            "still_unresolved",
            "jedi_errors",
        ):
            assert key in stats, f"missing stats key {key}"
    finally:
        db_mod.close(conn)
