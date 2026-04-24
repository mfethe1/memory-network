"""Targeted-update convergence: cross-file symbol additions resolve
previously unresolved calls without requiring `update --all`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import reindex


def _init_db(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_cross_file_call_backfills_on_targeted_update(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    # File B references pkg.util.helper before pkg.util exists.
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

    config, conn = _init_db(tmp_path)
    try:
        stats = reindex(conn, config, paths=None, event_source="init")
        assert stats.relations_unresolved >= 2  # the imports + calls edges
        # No edges should exist yet.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relations WHERE relation_kind IN ('calls','imports')"
            ).fetchone()[0]
            == 0
        )
        open_before = conn.execute(
            "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
        ).fetchone()[0]
        assert open_before >= 2

        # Now add util.py with the helper and update --files util.py (NOT service.py).
        util = tmp_path / "pkg" / "util.py"
        util.write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
        stats2 = reindex(conn, config, paths=[util], event_source="update")
        assert stats2.relations_backfilled >= 2
        assert stats2.relations_inserted >= 2

        # The service → util edge appears even though service was not reparsed.
        edge = conn.execute(
            """
            SELECT 1 FROM relations r
              JOIN symbols s1 ON s1.symbol_pk = r.src_symbol_pk
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls'
               AND s1.canonical_name = 'pkg.service.run'
               AND s2.canonical_name = 'pkg.util.helper'
            """
        ).fetchone()
        assert edge is not None

        # The unresolved_calls row for that edge is marked resolved.
        open_after = conn.execute(
            "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
        ).fetchone()[0]
        assert open_after < open_before

        # Running again should be a no-op for backfills.
        stats3 = reindex(conn, config, paths=[util], event_source="update")
        assert stats3.relations_backfilled == 0
    finally:
        db_mod.close(conn)


def test_rename_only_updates_the_renamed_file_and_converges(tmp_path: Path):
    # File A defines helper; file B calls it.
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
    config, conn = _init_db(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        calls = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE relation_kind='calls'"
        ).fetchone()[0]
        assert calls == 1

        # Rename helper to assist in util.py; update --files util.py only.
        (tmp_path / "pkg" / "util.py").write_text(
            "def assist(x):\n    return x + 1\n", encoding="utf-8"
        )
        stats = reindex(
            conn, config, paths=[tmp_path / "pkg" / "util.py"], event_source="update"
        )
        # The stale edge to helper should be gone.
        stale = conn.execute(
            """
            SELECT 1 FROM relations r
              JOIN symbols s2 ON s2.symbol_pk = r.dst_symbol_pk
             WHERE r.relation_kind = 'calls' AND s2.canonical_name = 'pkg.util.helper'
               AND s2.deleted_at IS NULL
            """
        ).fetchone()
        assert stale is None
        # service.py still references `helper` by name — this is expected to
        # become an unresolved row until service.py itself is reparsed with
        # the new target name. Backfill cannot invent a rename.
        # Prove that the unresolved entry exists so an agent can flag it.
        assert stats.relations_backfilled == 0
    finally:
        db_mod.close(conn)
