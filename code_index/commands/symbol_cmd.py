"""`code_index symbol`: durable-identity symbol lookup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.search import symbol_search


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        results = symbol_search.lookup(
            conn,
            args.name,
            kind=args.kind,
            language=args.lang,
            limit=args.limit,
            include_references=getattr(args, "references", False),
        )
    finally:
        db_mod.close(conn)

    if args.json:
        print(json.dumps({"query": args.name, "results": results}, indent=2))
        return 0
    if not results:
        print(f"no symbols matching {args.name!r}")
        return 0
    for row in results:
        loc = f"{row['def_file']}:{row['def_line']}" if row["def_file"] else "?"
        print(
            f"[{row['kind']}] {row['canonical_name']}  ({loc})  uid={row['symbol_uid']}"
        )
    return 0
