"""`code_index grep`: lexical fast path (ripgrep or Python fallback)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index.search import lexical


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    try:
        result = lexical.grep(
            config,
            pattern=args.pattern,
            path_glob=args.path,
            max_count=args.max_count,
            case_insensitive=args.ignore_case,
            fixed_strings=args.fixed_strings,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for hit in result["hits"]:
        print(f"{hit['file']}:{hit['line']}:{hit['column']}: {hit['text']}")
    if not result["hits"]:
        print("no matches")
    return 0
