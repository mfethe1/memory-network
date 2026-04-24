"""`code_index update`: targeted or full reindex."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.locking import LockTimeoutError
from code_index.pipeline import reindex
from code_index.symbols import rename_symbol


def _load_rename_map(path: Path) -> list[tuple[str, str]]:
    """Load `[{"old": ..., "new": ...}, ...]`. Raises ValueError on bad shape."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("rename-map must be a JSON array")
    entries: list[tuple[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "old" not in item or "new" not in item:
            raise ValueError(f"rename-map entry {i} must be {{'old': ..., 'new': ...}}")
        old = str(item["old"])
        new = str(item["new"])
        if not old or not new:
            raise ValueError(f"rename-map entry {i} has empty old/new")
        entries.append((old, new))
    return entries


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    paths: list[Path] | None = None
    if args.files:
        paths = [Path(p) for p in args.files]
    elif args.all:
        paths = None
    else:
        paths = []  # no-op mode still records schema freshness / pragmas

    rename_entries: list[tuple[str, str]] = []
    rename_map_path = getattr(args, "rename_map", None)
    if rename_map_path:
        try:
            rename_entries = _load_rename_map(Path(rename_map_path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            err = {
                "error": f"invalid --rename-map: {exc}",
                "path": str(rename_map_path),
            }
            if getattr(args, "json", False):
                print(json.dumps(err, indent=2))
            else:
                print(f"error: {err['error']}")
            return 2

    conn = db_mod.connect(config.db_path)
    rename_report: list[dict] = []
    try:
        db_mod.apply_schema(conn)
        if rename_entries:
            for old, new in rename_entries:
                migrated = rename_symbol(conn, old_canonical=old, new_canonical=new)
                rename_report.append({"old": old, "new": new, "migrated": migrated})
            conn.commit()
        try:
            stats = reindex(
                conn,
                config,
                paths=paths,
                event_source="update",
                force=args.force,
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

    report: dict = {"stats": stats.to_dict()}
    if rename_report:
        report["renames"] = rename_report
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"updated: seen={stats.files_seen} "
            f"parsed={stats.files_parsed} "
            f"unchanged={stats.files_unchanged} "
            f"failed={stats.files_failed}"
        )
        print(
            f"chunks: +{stats.chunks_created} ~{stats.chunks_updated} "
            f"-{stats.chunks_tombstoned}"
        )
    return 0
