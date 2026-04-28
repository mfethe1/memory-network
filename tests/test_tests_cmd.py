"""Affected-tests lookup: direct + transitive via materialized test_edges."""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.pipeline import reindex


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "lowlevel.py").write_text(
        "def deep_target(x):\n    return x + 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "midlevel.py").write_text(
        textwrap.dedent(
            """
            from pkg.lowlevel import deep_target

            def wrapper(x):
                return deep_target(x) * 2
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_direct.py").write_text(
        textwrap.dedent(
            """
            from pkg.lowlevel import deep_target

            def test_deep_target_directly():
                assert deep_target(3) == 4
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_indirect.py").write_text(
        textwrap.dedent(
            """
            from pkg.midlevel import wrapper

            def test_wrapper_reaches_deep_target():
                assert wrapper(3) == 8
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _write_related_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "service.py").write_text(
        textwrap.dedent(
            """
            class Service:
                def target(self):
                    return 1

                def alternate(self):
                    return 2
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_service.py").write_text(
        textwrap.dedent(
            """
            def test_alternate_related():
                assert True
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_test_edges_direct_and_transitive(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # Find deep_target.
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE canonical_name = ?",
            ("pkg.lowlevel.deep_target",),
        ).fetchone()
        assert row is not None
        target_pk = int(row["symbol_pk"])

        rows = conn.execute(
            """
            SELECT s.canonical_name AS tname, te.edge_type, te.depth, te.path_json
              FROM test_edges te
              JOIN symbols s ON s.symbol_pk = te.test_symbol_pk
             WHERE te.target_symbol_pk = ?
            """,
            (target_pk,),
        ).fetchall()
        by_name = {r["tname"]: r for r in rows}

        direct_name = "tests.test_direct.test_deep_target_directly"
        transitive_name = "tests.test_indirect.test_wrapper_reaches_deep_target"
        assert direct_name in by_name
        assert transitive_name in by_name
        assert by_name[direct_name]["edge_type"] == "direct"
        assert by_name[direct_name]["depth"] == 1
        assert by_name[transitive_name]["edge_type"] == "transitive"
        assert by_name[transitive_name]["depth"] >= 2
        # Path must route through wrapper → deep_target for the transitive case.
        import json as _json

        path = _json.loads(by_name[transitive_name]["path_json"])
        assert "pkg.midlevel.wrapper" in path
        assert path[-1] == "pkg.lowlevel.deep_target"
    finally:
        db_mod.close(conn)


def test_tests_cmd_returns_both_kinds(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    import json as _json
    import subprocess, sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "tests",
            "deep_target",
            "--root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = _json.loads(proc.stdout)
    assert payload["summary"]["direct"] >= 1
    assert payload["summary"]["transitive"] >= 1
    names = [t["canonical_name"] for t in payload["affected_tests"]]
    assert any("test_deep_target_directly" in n for n in names)
    assert any("test_wrapper_reaches_deep_target" in n for n in names)

    # --direct-only drops transitive.
    proc2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "tests",
            "deep_target",
            "--root",
            str(tmp_path),
            "--direct-only",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload2 = _json.loads(proc2.stdout)
    assert payload2["summary"]["transitive"] == 0
    assert payload2["summary"]["direct"] >= 1


def test_symbol_uid_input_resolves(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        row = conn.execute(
            "SELECT symbol_uid FROM symbols WHERE canonical_name = ?",
            ("pkg.lowlevel.deep_target",),
        ).fetchone()
        assert row is not None
        symbol_uid = row["symbol_uid"]
    finally:
        db_mod.close(conn)

    import json as _json
    import subprocess, sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "tests",
            symbol_uid,
            "--root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = _json.loads(proc.stdout)
    assert payload["target"]["symbol_uid"] == symbol_uid
    assert payload["summary"]["affected_test_count"] >= 2


def test_tests_cmd_falls_back_to_related_method_coverage(tmp_path: Path):
    _write_related_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        rows = {
            row["canonical_name"]: row
            for row in conn.execute(
                """
                SELECT symbol_pk, canonical_name
                  FROM symbols
                 WHERE canonical_name IN (
                    'pkg.service.Service.target',
                    'pkg.service.Service.alternate',
                    'tests.test_service.test_alternate_related'
                 )
                """
            ).fetchall()
        }
        target_pk = int(rows["pkg.service.Service.target"]["symbol_pk"])
        alternate_pk = int(rows["pkg.service.Service.alternate"]["symbol_pk"])
        test_pk = int(rows["tests.test_service.test_alternate_related"]["symbol_pk"])
        chunk = conn.execute(
            "SELECT chunk_pk FROM chunks WHERE primary_symbol_pk = ?",
            (test_pk,),
        ).fetchone()
        assert chunk is not None
        conn.execute(
            """
            INSERT INTO test_edges(
                test_chunk_pk, test_symbol_pk, target_symbol_pk,
                edge_type, depth, confidence, path_json, provenance
            ) VALUES (?, ?, ?, 'direct', 1, 1.0, ?, 'test:manual')
            """,
            (
                int(chunk["chunk_pk"]),
                test_pk,
                alternate_pk,
                '["tests.test_service.test_alternate_related","pkg.service.Service.alternate"]',
            ),
        )
        assert not conn.execute(
            "SELECT 1 FROM test_edges WHERE target_symbol_pk = ?",
            (target_pk,),
        ).fetchone()
    finally:
        db_mod.close(conn)

    import json as _json
    import subprocess, sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "tests",
            "pkg.service.Service.target",
            "--root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = _json.loads(proc.stdout)
    assert payload["summary"]["match_scope"] == "related"
    assert payload["summary"]["affected_test_count"] == 1
    row = payload["affected_tests"][0]
    assert row["canonical_name"] == "tests.test_service.test_alternate_related"
    assert row["match_reason"] in {"sibling", "same_file"}
    assert row["matched_target"]["canonical_name"] == "pkg.service.Service.alternate"
