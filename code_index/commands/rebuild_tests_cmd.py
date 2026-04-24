"""`code_index rebuild-tests`: force a full `test_edges` rebuild.

Incremental reindex paths can do a scoped `test_edges` rebuild (only
recomputing edges for test symbols whose definition or targets moved). This
command is the escape hatch when you want the full rebuild — useful after
large refactors or when verifying correctness against the scoped path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.db import transaction
from code_index.pipeline import ReindexStats, _rebuild_test_edges


def run(args: argparse.Namespace) -> int:
    from code_index.locking import LockTimeoutError, writer_lock

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                stats = ReindexStats()
                before = conn.execute("SELECT COUNT(*) FROM test_edges").fetchone()[0]
                with transaction(conn):
                    _rebuild_test_edges(conn, stats)
                after = conn.execute("SELECT COUNT(*) FROM test_edges").fetchone()[0]
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        payload = {"error": str(exc), "lock_path": str(exc.lock_path)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {exc}")
        return 3

    report = {
        "scope": "full",
        "edges_before": before,
        "edges_removed": stats.test_edges_removed,
        "edges_inserted": stats.test_edges_inserted,
        "edges_after": after,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"rebuild-tests: {before} -> {after} "
            f"(removed {stats.test_edges_removed}, inserted {stats.test_edges_inserted})"
        )
    return 0
