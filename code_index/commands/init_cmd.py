"""`code_index init`: scaffold .code_index/ + run full scan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.locking import LockTimeoutError
from code_index.pipeline import reindex


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    if not root.exists():
        print(f"error: root does not exist: {root}")
        return 2
    config = cfg_mod.load(root)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        cfg_mod.save(config)

    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        try:
            stats = reindex(
                conn, config, paths=None, event_source="init", force=args.force
            )
        except LockTimeoutError as exc:
            err = {
                "error": "another writer holds the lock",
                "lock_path": str(exc.lock_path),
                "timeout_s": exc.timeout_s,
            }
            if getattr(args, "json", False):
                print(json.dumps(err, indent=2))
            else:
                print(f"error: {err['error']} at {err['lock_path']}")
            return 3
    finally:
        db_mod.close(conn)

    report = {
        "root": str(config.root),
        "index_dir": str(config.index_dir),
        "db_path": str(config.db_path),
        "schema_version": db_mod.SCHEMA_VERSION,
        "stats": stats.to_dict(),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"initialized code_index at {config.index_dir}")
        print(f"  db:            {config.db_path}")
        print(f"  files seen:    {stats.files_seen}")
        print(f"  files parsed:  {stats.files_parsed}")
        print(f"  files skipped: {stats.files_skipped}")
        print(f"  chunks added:  {stats.chunks_created}")
        print(f"  symbols added: {stats.symbols_upserted}")
        if stats.errors:
            print(f"  errors:        {len(stats.errors)} (use --json for detail)")
    return 0
