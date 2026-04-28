"""Python resolution quality: relative imports + class-qualified calls."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.parsers.python_ast import (
    PythonAstParser,
    _resolve_relative_module,
)
from code_index.pipeline import reindex


def test_resolve_relative_module_basic():
    # `from . import x` inside pkg.sub.mod → package is pkg.sub.
    assert _resolve_relative_module(1, None, "pkg.sub.mod") == "pkg.sub"
    assert _resolve_relative_module(1, "sibling", "pkg.sub.mod") == "pkg.sub.sibling"
    assert _resolve_relative_module(2, "thing", "pkg.sub.mod") == "pkg.thing"
    # Popping past the root returns None rather than a misleading guess.
    assert _resolve_relative_module(5, "x", "pkg.sub.mod") is None


def test_resolve_relative_module_in_package_init():
    # `from . import x` in `pkg/__init__.py` (module_name='pkg') — level 1
    # means "within this package"; the resolver must NOT drop a segment
    # because __init__.py's __package__ == self_module.
    assert _resolve_relative_module(1, "helper", "pkg", is_package=True) == "pkg.helper"
    # And level 2 goes one step up: `from .. import x` in pkg.sub/__init__.py.
    assert (
        _resolve_relative_module(2, "helper", "pkg.sub", is_package=True)
        == "pkg.helper"
    )
    # Regular module (not __init__.py): level 1 drops one segment.
    assert _resolve_relative_module(1, "helper", "pkg.mod") == "pkg.helper"


def test_parser_emits_resolved_relative_imports():
    src = textwrap.dedent(
        """
        from . import sibling
        from ..pkg import thing as T
        from .sub.mod import helper
        """
    ).lstrip()
    r = PythonAstParser().parse(rel_path="mypkg/subpkg/module.py", source=src)
    import_candidates = [
        pr.dst_candidates for pr in r.pending_relations if pr.relation_kind == "imports"
    ]
    # Flatten for easy membership checks.
    flat = [c for group in import_candidates for c in group]
    assert "mypkg.subpkg.sibling" in flat
    assert "mypkg.pkg.thing" in flat
    assert "mypkg.subpkg.sub.mod.helper" in flat
    # Provenance tags level so we can debug later.
    prov = [
        pr.provenance for pr in r.pending_relations if pr.relation_kind == "imports"
    ]
    assert any("relative=level1" in p for p in prov)
    assert any("relative=level2" in p for p in prov)


def test_class_qualified_call_inside_class_body():
    src = textwrap.dedent(
        """
        class Controller:
            @classmethod
            def make(cls):
                return cls.configure()

            @classmethod
            def configure(cls):
                return 1

            def run(self):
                Controller.make()
                return self.configure()
        """
    ).lstrip()
    r = PythonAstParser().parse(rel_path="pkg/ctrl.py", source=src)
    call_edges = [pr for pr in r.pending_relations if pr.relation_kind == "calls"]
    # self.configure → class-qualified already worked; cls.configure too.
    # The new capability: Controller.make() inside Controller.run()
    # resolves class-internally without requiring self.
    found_class_internal = False
    for pr in call_edges:
        if "pkg.ctrl.Controller.make" in pr.dst_candidates:
            found_class_internal = True
            break
    assert found_class_internal, (
        f"Expected class-internal Controller.make resolution; got {call_edges}"
    )


def test_relative_import_resolves_to_sibling_package(tmp_path: Path):
    """End-to-end: `from . import helper` in pkg/sub/a.py resolves to
    pkg.sub.helper which exists in pkg/sub/helper.py.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "helper.py").write_text(
        "def thing():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "sub" / "a.py").write_text(
        textwrap.dedent(
            """
            from . import helper

            def caller():
                return helper.thing()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        reindex(conn, config, paths=None, event_source="init")
        # The imports edge should land: pkg.sub.a -> pkg.sub.helper (or its thing).
        edge = conn.execute(
            """
            SELECT s2.canonical_name AS dst FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'imports'
               AND s1.canonical_name = 'pkg.sub.a'
            """
        ).fetchall()
        dsts = [row["dst"] for row in edge]
        assert any(d in {"pkg.sub.helper", "pkg.sub.helper.thing"} for d in dsts), dsts

        # And the call edge caller -> helper.thing resolves via the sibling
        # package module.
        call = conn.execute(
            """
            SELECT s2.canonical_name AS dst FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls'
               AND s1.canonical_name = 'pkg.sub.a.caller'
            """
        ).fetchall()
        assert any(row["dst"] == "pkg.sub.helper.thing" for row in call), [
            r["dst"] for r in call
        ]
    finally:
        db_mod.close(conn)
