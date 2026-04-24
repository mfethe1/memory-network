"""`__init__.py` re-export propagation.

When `pkg/__init__.py` does `from .impl import Foo`, a caller elsewhere
writing `from pkg import Foo` should resolve to `pkg.impl.Foo` without us
needing to reparse the caller. The resolver tier is driven by a re-export
map built at the end of each reindex pass."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import _build_reexport_map, reindex


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _setup_reexport_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "impl.py").write_text(
        "def Foo():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .impl import Foo\n", encoding="utf-8"
    )
    (tmp_path / "caller.py").write_text(
        textwrap.dedent(
            """
            from pkg import Foo

            def run():
                return Foo()
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_reexport_map_captured_from_init_py(tmp_path: Path):
    _setup_reexport_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        rx = _build_reexport_map(conn)
        assert rx.get("pkg.Foo") == "pkg.impl.Foo"
    finally:
        db_mod.close(conn)


def test_caller_resolves_through_reexport(tmp_path: Path):
    _setup_reexport_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # caller.run must resolve Foo() to pkg.impl.Foo via the re-export.
        row = conn.execute(
            """
            SELECT s2.canonical_name AS dst FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls'
               AND s1.canonical_name = 'caller.run'
            """
        ).fetchall()
        dsts = [r["dst"] for r in row]
        assert "pkg.impl.Foo" in dsts, dsts
    finally:
        db_mod.close(conn)


def test_as_alias_in_reexport(tmp_path: Path):
    """`from .impl import Foo as Bar` must record pkg.Bar -> pkg.impl.Foo."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "impl.py").write_text(
        "def Foo():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .impl import Foo as Bar\n", encoding="utf-8"
    )
    (tmp_path / "caller.py").write_text(
        "from pkg import Bar\n\ndef run():\n    return Bar()\n", encoding="utf-8"
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        rx = _build_reexport_map(conn)
        assert rx.get("pkg.Bar") == "pkg.impl.Foo"
        row = conn.execute(
            """
            SELECT s2.canonical_name FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls'
               AND s1.canonical_name = 'caller.run'
            """
        ).fetchone()
        assert row is not None and row[0] == "pkg.impl.Foo"
    finally:
        db_mod.close(conn)


def test_reexport_star_import_skipped(tmp_path: Path):
    """`from .impl import *` has an unbounded target set; we skip it rather
    than emit noise."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "impl.py").write_text(
        "def Foo():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .impl import *\n", encoding="utf-8"
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        rx = _build_reexport_map(conn)
        # No entry for `pkg.Foo` should be invented from a star import.
        assert "pkg.Foo" not in rx
    finally:
        db_mod.close(conn)
