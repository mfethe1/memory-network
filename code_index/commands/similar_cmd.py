"""`code_index similar`: semantic retrieval over embedded chunks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.embeddings import (
    DEFAULT_MODEL,
    availability_report,
    get_backend,
    semantic_search,
)


def run(args: argparse.Namespace) -> int:
    report = availability_report()
    if not report["available"]:
        payload = {
            "error": "no embedding backend installed",
            "hint": "pip install fastembed  (or: pip install sentence-transformers)",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {payload['error']}")
        return 2

    if not args.query:
        print("error: provide a query string")
        return 2

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    backend = get_backend(args.model or DEFAULT_MODEL)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        hits = semantic_search(
            conn,
            backend,
            args.query,
            limit=args.limit,
            language=args.lang,
            chunk_type=args.type,
        )
    finally:
        db_mod.close(conn)

    payload = {
        "engine": "embeddings",
        "provider": backend.provider,
        "model": backend.model_name,
        "query": args.query,
        "results": hits,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if not hits:
        print("no matches (did you run `code_index embed` first?)")
        return 0
    for h in hits:
        name = h.get("symbol_path") or h.get("symbol_name") or "?"
        print(
            f"[{h['chunk_type']}] {name}  "
            f"{h['file_path']}:{h['start_line']}-{h['end_line']}  "
            f"score={h['score']:.3f}"
        )
    return 0
