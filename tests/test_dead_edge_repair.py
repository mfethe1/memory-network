"""Dead edges (live src → tombstoned dst) should queue into unresolved_calls
and heal on later reindex if a matching canonical_name reappears anywhere."""

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


def _write_base_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "util.py").write_text(
        "def helper(x):\n    return x + 1\n", encoding="utf-8"
    )
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


def test_dead_edge_queued_for_repair_when_target_tombstones(tmp_path: Path):
    _write_base_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # service.run → util.helper edge exists.
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM relations r
                  JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
                  JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
                 WHERE s1.canonical_name = 'pkg.service.run'
                   AND s2.canonical_name = 'pkg.util.helper'
                """
            ).fetchone()[0]
            == 1
        )

        # Remove helper entirely (no replacement). service.py is NOT reparsed.
        (tmp_path / "pkg" / "util.py").write_text(
            "def other():\n    return 2\n", encoding="utf-8"
        )
        stats = reindex(
            conn,
            config,
            paths=[tmp_path / "pkg" / "util.py"],
            event_source="update",
        )
        # The dead edge is removed; an unresolved_calls row replaces it.
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM relations r
                  JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
                  JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
                 WHERE s1.canonical_name = 'pkg.service.run'
                   AND s2.canonical_name = 'pkg.util.helper'
                """
            ).fetchone()[0]
            == 0
        )
        assert stats.relations_queued_for_repair >= 1
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM unresolved_calls
                 WHERE resolved_at IS NULL
                   AND provenance LIKE '%repair:dst-tombstoned%'
                """
            ).fetchone()[0]
            >= 1
        )
    finally:
        db_mod.close(conn)


def test_dead_edge_heals_when_same_canonical_name_reappears(tmp_path: Path):
    _write_base_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # Remove helper.
        (tmp_path / "pkg" / "util.py").write_text(
            "def other():\n    return 2\n", encoding="utf-8"
        )
        reindex(
            conn, config, paths=[tmp_path / "pkg" / "util.py"], event_source="update"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
            ).fetchone()[0]
            >= 1
        )
        # Now a DIFFERENT file reintroduces pkg.util.helper (as if someone
        # restored it by editing util.py again).
        (tmp_path / "pkg" / "util.py").write_text(
            "def helper(x):\n    return x + 1\n", encoding="utf-8"
        )
        stats = reindex(
            conn, config, paths=[tmp_path / "pkg" / "util.py"], event_source="update"
        )
        # The repaired edge is back.
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM relations r
                  JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
                  JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
                 WHERE s1.canonical_name = 'pkg.service.run'
                   AND s2.canonical_name = 'pkg.util.helper'
                """
            ).fetchone()[0]
            == 1
        )
        assert stats.relations_backfilled >= 1
    finally:
        db_mod.close(conn)


def test_unresolved_row_does_not_advance_when_still_unresolved(tmp_path: Path):
    """Regression guard: earlier implementation stamped resolved_at=now even
    when no edge was created, preventing later retries."""
    _write_base_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        (tmp_path / "pkg" / "util.py").write_text(
            "def other():\n    return 2\n", encoding="utf-8"
        )
        reindex(
            conn, config, paths=[tmp_path / "pkg" / "util.py"], event_source="update"
        )
        open_before = conn.execute(
            "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
        ).fetchone()[0]
        # Run a no-op reindex (nothing changed). The unresolved row must STAY
        # open so the next real change can heal it.
        reindex(conn, config, paths=[], event_source="update")
        open_after = conn.execute(
            "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
        ).fetchone()[0]
        assert open_after == open_before
    finally:
        db_mod.close(conn)
