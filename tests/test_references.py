"""Call-site reference occurrence coverage."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.pipeline import reindex
from code_index.search import symbol_search


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def _write_repo(tmp_path: Path, caller_body: str) -> None:
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "target.py").write_text(
        "def target():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "caller.py").write_text(
        textwrap.dedent(caller_body).lstrip(), encoding="utf-8"
    )


def _references(conn) -> list[dict]:
    results = symbol_search.lookup(
        conn,
        "pkg.target.target",
        limit=1,
        include_references=True,
    )
    assert results
    return results[0]["references"]


def test_symbol_references_flag_lists_fresh_index_call_sites(tmp_path: Path):
    _write_repo(
        tmp_path,
        """
        from pkg.target import target

        def run():
            return target()
        """,
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_index",
            "symbol",
            "pkg.target.target",
            "--root",
            str(tmp_path),
            "--json",
            "--references",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    refs = payload["results"][0]["references"]
    assert refs == [{"file": "pkg/caller.py", "start_line": 4, "end_line": 4}]


def test_reference_occurrence_disappears_when_caller_call_is_removed(tmp_path: Path):
    _write_repo(
        tmp_path,
        """
        from pkg.target import target

        def run():
            return target()
        """,
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        assert _references(conn) == [
            {"file": "pkg/caller.py", "start_line": 4, "end_line": 4}
        ]

        _write_repo(
            tmp_path,
            """
            from pkg.target import target

            def run():
                return 1
            """,
        )
        reindex(
            conn,
            config,
            paths=[tmp_path / "pkg" / "caller.py"],
            event_source="update",
        )

        assert _references(conn) == []
    finally:
        db_mod.close(conn)


def test_reference_occurrence_reappears_when_caller_call_is_restored(tmp_path: Path):
    _write_repo(
        tmp_path,
        """
        from pkg.target import target

        def run():
            return 1
        """,
    )
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        assert _references(conn) == []

        _write_repo(
            tmp_path,
            """
            from pkg.target import target

            def run():
                return target()
            """,
        )
        reindex(
            conn,
            config,
            paths=[tmp_path / "pkg" / "caller.py"],
            event_source="update",
        )

        assert _references(conn) == [
            {"file": "pkg/caller.py", "start_line": 4, "end_line": 4}
        ]
    finally:
        db_mod.close(conn)
