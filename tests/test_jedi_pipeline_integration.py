"""End-to-end: Jedi runs INSIDE reindex() when `config.enable_jedi=True`.

This exercises Task 2 of slice-9: Jedi is a resolver tier, not a
separate standalone pass. A typed-instance call like
`foo = Bar(); foo.method()` should produce a relation on the FIRST
`reindex()`, with `stats.relations_resolved_by_jedi > 0`."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("jedi")

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import reindex


def _make_fixture(tmp_path: Path) -> None:
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


def test_jedi_runs_inside_reindex_and_lands_edge(tmp_path: Path):
    _make_fixture(tmp_path)
    config = cfg_mod.load(tmp_path)
    config.enable_jedi = True
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        stats = reindex(conn, config, paths=None, event_source="init")
        assert stats.relations_resolved_by_jedi > 0, (
            f"expected Jedi to resolve at least one edge, got stats={stats.to_dict()}"
        )
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
        assert row is not None, (
            "expected pkg.caller.do → pkg.bar.Bar.method edge after reindex"
        )
        assert "jedi:goto" in (row[0] or "")
    finally:
        db_mod.close(conn)


def test_jedi_gate_off_leaves_call_unresolved(tmp_path: Path):
    """With enable_jedi=False (default), the typed-instance call lands in
    unresolved_calls — same as before the resolver tier was added."""
    _make_fixture(tmp_path)
    config = cfg_mod.load(tmp_path)
    assert config.enable_jedi is False  # baseline contract
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        stats = reindex(conn, config, paths=None, event_source="init")
        assert stats.relations_resolved_by_jedi == 0
        # Confirm foo.method() stayed open.
        row = conn.execute(
            """
            SELECT COUNT(*) FROM unresolved_calls uc
              JOIN symbols s ON s.symbol_uid = uc.src_symbol_uid
             WHERE uc.resolved_at IS NULL
               AND uc.relation_kind = 'calls'
               AND s.canonical_name = 'pkg.caller.do'
            """
        ).fetchone()
        assert row[0] >= 1
    finally:
        db_mod.close(conn)
