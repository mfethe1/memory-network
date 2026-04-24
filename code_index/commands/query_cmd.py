"""`code_index query`: FTS-backed ranked retrieval, or tree-sitter structural
search when --ast is supplied.

FTS path:
  code_index query "keyword phrase"       → BM25-ranked chunks
Structural path:
  code_index query --ast class            → bundled tree-sitter query
  code_index query --ast "(call function: (identifier) @callee)"
                                          → raw S-expression against Python grammar
  code_index query --ast --list-ast-queries
                                          → list bundled query aliases and exit
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.ignore import build as build_matcher
from code_index.scanner import iter_files
from code_index.search import fts
from code_index.structural import ts_python


def _run_ast(args: argparse.Namespace, config: cfg_mod.Config) -> int:
    if args.list_ast_queries:
        payload = {
            "bundled_queries": ts_python.bundled_query_names(),
            "engine_available": ts_python.available(),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for name in payload["bundled_queries"]:
                print(name)
        return 0

    if not ts_python.available():
        reason = ts_python._unavailable_reason()
        payload = {
            "error": "tree-sitter not available",
            "reason": reason,
            "hint": "install with: pip install tree-sitter tree-sitter-python",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {payload['error']} ({reason})")
            print(f"hint:  {payload['hint']}")
        return 2

    lang = args.lang or "python"
    if lang != "python":
        msg = f"--ast currently supports Python only (got --lang={lang})"
        if args.json:
            print(json.dumps({"error": msg, "supported": ["python"]}, indent=2))
        else:
            print(f"error: {msg}")
        return 2

    # Walk Python files in the repo (respecting ignore rules).
    matcher = build_matcher(
        config.root, extra=config.extra_ignore, include_hidden=config.include_hidden
    )
    files: list[tuple[Path, str]] = []
    for scanned in iter_files(config.root, matcher, max_bytes=config.max_file_bytes):
        if scanned.rel_path.lower().endswith((".py", ".pyi")):
            files.append((scanned.path, scanned.rel_path))

    try:
        result = ts_python.query_files(files, args.pattern)
    except RuntimeError as exc:
        payload = {"error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {exc}")
        return 2
    except Exception as exc:  # raw pattern may fail to compile
        payload = {
            "error": "invalid tree-sitter query",
            "detail": repr(exc),
            "pattern": args.pattern,
            "expanded": ts_python.expand_query(args.pattern),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {payload['error']}: {exc}")
        return 2

    # Truncate results to --limit if provided.
    captures = result.captures[: args.limit] if args.limit else result.captures

    payload = {
        "engine": "tree-sitter",
        "language": "python",
        "query": result.query,
        "expanded_query": result.expanded_query,
        "total_captures": len(result.captures),
        "returned": len(captures),
        "results": [
            {
                "file": c.file_path,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "capture_name": c.capture_name,
                "node_kind": c.node_kind,
                "preview": c.text[:120].replace("\n", " "),
            }
            for c in captures
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if not captures:
        print(
            f"no structural matches for '{result.query}' (expanded: {result.expanded_query})"
        )
        return 0
    for cap in captures:
        print(
            f"{cap.file_path}:{cap.start_line}-{cap.end_line} "
            f"[{cap.capture_name}:{cap.node_kind}] "
            f"{cap.text[:100].replace(chr(10), ' ')}"
        )
    return 0


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)

    if args.ast or args.list_ast_queries:
        if args.ast and not args.list_ast_queries and not args.pattern:
            print("error: --ast requires a pattern (bundled name or raw S-expression)")
            return 2
        return _run_ast(args, config)

    if not args.pattern:
        print("error: pattern is required (or use --ast / --list-ast-queries)")
        return 2

    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        results = fts.search(
            conn,
            args.pattern,
            limit=args.limit,
            language=args.lang,
            chunk_type=args.type,
        )
    finally:
        db_mod.close(conn)

    if args.json:
        print(
            json.dumps(
                {"engine": "fts5", "query": args.pattern, "results": results}, indent=2
            )
        )
        return 0
    if not results:
        print("no matches")
        return 0
    for row in results:
        name = row["symbol_path"] or row["symbol_name"] or "?"
        print(
            f"[{row['chunk_type']}] {name}  "
            f"{row['file_path']}:{row['start_line']}-{row['end_line']}  "
            f"score={row['score']:.2f}"
        )
        snippet = (row["snippet"] or "").replace("\n", " ")
        if snippet:
            print(f"    {snippet}")
    return 0
