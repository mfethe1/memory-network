"""`code_index import-scip`: ingest SCIP semantic indexes."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.locking import LockTimeoutError, writer_lock
from code_index.scip_import import import_scip_json, load_scip_json


def _load_from_scip_binary(index_path: Path) -> dict:
    scip = shutil.which("scip")
    if not scip:
        raise RuntimeError(
            "scip CLI is not on PATH; install it or pass --json-index instead"
        )
    proc = subprocess.run(
        [scip, "print", "--json", str(index_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"scip print --json failed: {detail}")
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict):
        raise ValueError("scip print --json did not return a JSON object")
    return payload


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    try:
        if args.json_index:
            payload = load_scip_json(Path(args.json_index))
            source_path = str(Path(args.json_index))
        elif args.index:
            payload = _load_from_scip_binary(Path(args.index))
            source_path = str(Path(args.index))
        else:
            raise ValueError("provide --json-index PATH or --from PATH")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        err = {"error": f"invalid SCIP input: {exc}"}
        if args.json:
            print(json.dumps(err, indent=2))
        else:
            print(f"error: {err['error']}")
        return 2

    conn = db_mod.connect(config.db_path)
    try:
        try:
            with writer_lock(config):
                db_mod.apply_schema(conn)
                with db_mod.transaction(conn):
                    stats = import_scip_json(
                        conn,
                        config,
                        payload,
                        event_source="import-scip",
                    )
        except LockTimeoutError as exc:
            err = {
                "error": "another writer holds the lock",
                "lock_path": str(exc.lock_path),
                "timeout_s": exc.timeout_s,
            }
            if args.json:
                print(json.dumps(err, indent=2))
            else:
                print(f"error: {err['error']} at {err['lock_path']}")
            return 3
    finally:
        db_mod.close(conn)

    report = {
        "source": source_path,
        "stats": stats.to_dict(),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            "imported SCIP: "
            f"documents={stats.documents_seen} "
            f"symbols={stats.symbols_upserted} "
            f"occurrences={stats.occurrences_inserted} "
            f"relations={stats.relations_inserted}"
        )
        if stats.errors:
            print(f"errors: {len(stats.errors)} (use --json for detail)")
    return 0
