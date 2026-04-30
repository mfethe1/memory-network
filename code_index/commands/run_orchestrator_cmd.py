"""`code_index run-orchestrator`: inspect or apply Agent Run lifecycle rules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_orchestrator
from code_index.locking import writer_lock


def _policy(args: argparse.Namespace) -> run_orchestrator.OrchestratorPolicy:
    return run_orchestrator.OrchestratorPolicy(
        quiet_after_seconds=float(args.quiet_after_seconds),
        stale_after_seconds=float(args.stale_after_seconds),
    )


def _print_text(payload: dict) -> None:
    orchestrator = payload.get("orchestrator") or {}
    counts = orchestrator.get("counts") or {}
    actions = payload.get("actions") or []
    released_claims = payload.get("released_claims") or []
    print("run-orchestrator")
    if counts:
        print(
            "health: "
            + ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        )
    print(f"actions: {len(actions)}")
    print(f"released claims: {len(released_claims)}")


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    if args.apply:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                payload = run_orchestrator.apply(
                    conn,
                    known_dead_run_ids=set(args.known_dead_run_id or []),
                    policy=_policy(args),
                )
            finally:
                db_mod.close(conn)
    else:
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.ensure_schema(conn, config)
            payload = run_orchestrator.snapshot(conn, policy=_policy(args))
        finally:
            db_mod.close(conn)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_text(payload)
    return 0
