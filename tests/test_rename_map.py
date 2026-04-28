"""Explicit refactor migration via `code_index update --rename-map`.

`symbol_uid` is a deterministic declaration key — it changes when
canonical_name/signature/container/kind/language change. Users that
rename a symbol across files can opt into an in-place migration that
preserves `symbol_pk` (so FK references in relations, occurrences,
chunks.primary_symbol_pk, test_edges, chunk_symbols still resolve)
but changes `canonical_name` and recomputes `symbol_uid`.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.commands import update_cmd
from code_index.pipeline import reindex
from code_index.symbols import SymbolIdentity, rename_symbol


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_repo(tmp_path: Path) -> None:
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


def test_rename_symbol_preserves_pk_and_migrates_uid(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        # Look up the row that currently owns pkg.util.helper.
        before = conn.execute(
            "SELECT symbol_pk, symbol_uid, language, kind, signature_norm, "
            "container_symbol_pk FROM symbols "
            "WHERE canonical_name = 'pkg.util.helper'"
        ).fetchone()
        assert before is not None
        old_pk = before["symbol_pk"]
        old_uid = before["symbol_uid"]
        container_uid = ""
        if before["container_symbol_pk"] is not None:
            crow = conn.execute(
                "SELECT symbol_uid FROM symbols WHERE symbol_pk = ?",
                (before["container_symbol_pk"],),
            ).fetchone()
            if crow is not None:
                container_uid = crow["symbol_uid"] or ""

        # Capture a live downstream FK to prove it survives the rename.
        rel_count_before = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE dst_symbol_pk = ?", (old_pk,)
        ).fetchone()[0]
        assert rel_count_before >= 1

        # Migrate identity in place.
        ok = rename_symbol(
            conn,
            old_canonical="pkg.util.helper",
            new_canonical="pkg.util.renamed_helper",
        )
        conn.commit()
        assert ok is True

        after = conn.execute(
            "SELECT symbol_pk, symbol_uid, canonical_name FROM symbols "
            "WHERE symbol_pk = ?",
            (old_pk,),
        ).fetchone()
        assert after["symbol_pk"] == old_pk  # FKs preserved
        assert after["canonical_name"] == "pkg.util.renamed_helper"
        # UID recomputed per the canonical recipe.
        expected_uid = SymbolIdentity(
            language=before["language"] or "",
            kind=before["kind"],
            canonical_name="pkg.util.renamed_helper",
            signature_norm=before["signature_norm"] or "",
            container_uid=container_uid,
        ).symbol_uid
        assert after["symbol_uid"] == expected_uid
        assert after["symbol_uid"] != old_uid

        # Downstream FKs survive.
        rel_count_after = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE dst_symbol_pk = ?", (old_pk,)
        ).fetchone()[0]
        assert rel_count_after == rel_count_before
    finally:
        db_mod.close(conn)


def test_rename_symbol_no_such_symbol_returns_false(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        ok = rename_symbol(
            conn,
            old_canonical="pkg.util.does_not_exist",
            new_canonical="pkg.util.whatever",
        )
        assert ok is False
    finally:
        db_mod.close(conn)


def test_update_cmd_rename_map_flag(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    rename_map = tmp_path / "rename.json"
    rename_map.write_text(
        json.dumps([{"old": "pkg.util.helper", "new": "pkg.util.renamed_helper"}]),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        root=str(tmp_path),
        files=[],
        all=False,
        force=False,
        json=True,
        rename_map=str(rename_map),
    )
    rc = update_cmd.run(args)
    assert rc == 0

    conn = db_mod.connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT canonical_name FROM symbols WHERE canonical_name = ?",
            ("pkg.util.renamed_helper",),
        ).fetchone()
        assert row is not None
        gone = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE canonical_name = ? "
            "AND deleted_at IS NULL",
            ("pkg.util.helper",),
        ).fetchone()[0]
        assert gone == 0
    finally:
        db_mod.close(conn)


def test_doctor_identity_model_block(tmp_path: Path):
    _write_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    from code_index.commands import doctor_cmd
    import io
    import contextlib

    args = argparse.Namespace(root=str(tmp_path), json=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = doctor_cmd.run(args)
    assert rc == 0
    report = json.loads(buf.getvalue())
    assert "identity_model" in report
    block = report["identity_model"]
    assert block["refactor_durable"] is False
    assert block["migrate_via"] == "code_index update --rename-map PATH"
    assert set(block["fields"]) == {
        "language",
        "kind",
        "canonical_name",
        "signature_norm",
        "container_uid",
    }
