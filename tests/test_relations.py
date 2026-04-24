"""Pipeline-level tests for imports, calls, and inherits relations."""

from __future__ import annotations

import textwrap
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


def _make_repo(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "util.py").write_text(
        textwrap.dedent(
            """
            def helper(x: int) -> int:
                return x + 1

            class Base:
                def do(self) -> int:
                    return 0
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (pkg / "service.py").write_text(
        textwrap.dedent(
            """
            from pkg.util import helper, Base

            class Widget(Base):
                def run(self, x: int) -> int:
                    value = helper(x)
                    return self.do() + value
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _relation(conn, kind: str):
    return conn.execute(
        f"""
        SELECT s1.canonical_name AS src, s2.canonical_name AS dst
          FROM relations r
          JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
          JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
         WHERE r.relation_kind = ?
        """,
        (kind,),
    ).fetchall()


def test_contains_imports_calls_inherits_all_emitted(tmp_path: Path):
    _make_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        stats = reindex(conn, config, paths=None, event_source="init")
        assert stats.relations_inserted > 0

        calls = {(row["src"], row["dst"]) for row in _relation(conn, "calls")}
        imports = {(row["src"], row["dst"]) for row in _relation(conn, "imports")}
        inherits = {(row["src"], row["dst"]) for row in _relation(conn, "inherits")}
        contains = {(row["src"], row["dst"]) for row in _relation(conn, "contains")}

        # helper() call from service.Widget.run → util.helper resolves.
        assert ("pkg.service.Widget.run", "pkg.util.helper") in calls
        # self.do() from service.Widget.run → Widget.do... but Widget doesn't
        # define do(), so it resolves to the class scope (Widget.do is not a
        # symbol). The pipeline may resolve self.do to Widget.do which doesn't
        # exist. This edge is expected to go UNRESOLVED; make sure we don't
        # accidentally attribute it to Base.do.
        assert ("pkg.service.Widget.run", "pkg.util.Base.do") not in calls

        # Widget inherits from Base.
        assert ("pkg.service.Widget", "pkg.util.Base") in inherits
        # service module imports pkg.util (via `from pkg.util import ...`).
        # The resolver may record either the module or the targeted symbols.
        assert any(
            dst in {"pkg.util", "pkg.util.helper", "pkg.util.Base"}
            for src, dst in imports
            if src == "pkg.service"
        )
        # contains: module → class, class → method.
        assert ("pkg.service", "pkg.service.Widget") in contains
        assert ("pkg.service.Widget", "pkg.service.Widget.run") in contains
    finally:
        db_mod.close(conn)


def test_reindex_without_force_updates_relations_on_edit(tmp_path: Path):
    _make_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        before = {(row["src"], row["dst"]) for row in _relation(conn, "calls")}
        assert ("pkg.service.Widget.run", "pkg.util.helper") in before

        # Remove the call to helper() in service.py; relation should disappear.
        svc = tmp_path / "pkg" / "service.py"
        svc.write_text(
            textwrap.dedent(
                """
                from pkg.util import Base

                class Widget(Base):
                    def run(self, x: int) -> int:
                        return self.do()
                """
            ).lstrip(),
            encoding="utf-8",
        )
        reindex(conn, config, paths=[svc], event_source="update")
        after = {(row["src"], row["dst"]) for row in _relation(conn, "calls")}
        assert ("pkg.service.Widget.run", "pkg.util.helper") not in after
    finally:
        db_mod.close(conn)


def test_impact_surfaces_direct_callers(tmp_path: Path):
    _make_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        from code_index.commands.impact_cmd import _resolve_target, compute_impact

        candidates = _resolve_target(conn, "helper")
        assert candidates, "should find a symbol matching 'helper'"
        target_pk = int(candidates[0]["symbol_pk"])
        impact = compute_impact(conn, target_pk, max_depth=2, include_imports=True)
        names = [s["canonical_name"] for s in impact["impacted_symbols"]]
        # Widget.run calls helper directly.
        assert "pkg.service.Widget.run" in names
        # Impacted files include service.py.
        assert "pkg/service.py" in impact["impacted_files"]
        assert impact["summary"]["direct_callers"] >= 1
    finally:
        db_mod.close(conn)
