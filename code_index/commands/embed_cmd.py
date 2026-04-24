"""`code_index embed`: populate / refresh the embeddings table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.embeddings import (
    DEFAULT_MODEL,
    availability_report,
    coverage,
    get_backend,
    populate,
)


def run(args: argparse.Namespace) -> int:
    report = availability_report()
    if not report["available"]:
        payload = {
            "error": "no embedding backend installed",
            "hint": "pip install fastembed  (or: pip install sentence-transformers)",
            "backends": report["backends"],
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {payload['error']}")
            print(f"hint:  {payload['hint']}")
        return 2

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    from code_index.locking import LockTimeoutError, writer_lock

    backend = get_backend(args.model or DEFAULT_MODEL)
    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                stats = populate(
                    conn,
                    backend,
                    batch=args.batch,
                    refresh=args.refresh,
                    limit=args.limit,
                )
                cov = coverage(conn)
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        payload = {"error": str(exc), "lock_path": str(exc.lock_path)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {exc}")
        return 3

    payload = {
        "provider": backend.provider,
        "model": backend.model_name,
        "dimension": backend.dimension,
        "stats": stats,
        "coverage": cov,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(
        f"embed: {stats['embedded']} new, {stats['errors']} errors, "
        f"coverage {cov['coverage_pct']:.1f}% ({cov['embedded_chunks']}/{cov['total_chunks']})"
    )
    print(
        f"  backend: {backend.provider} / {backend.model_name} ({backend.dimension}d)"
    )
    return 0
