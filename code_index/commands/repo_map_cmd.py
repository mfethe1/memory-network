"""`code_index repo-map`: Aider-style compact repo overview for LLMs.

Ranks symbols by a composite score combining in-degree (callers + importers
+ inheriters + containers), test coverage (test_edges), and a kind boost
(module/class over function/method). Test-file symbols are filtered out.

Two output modes: `--format json` (default, stable contract) and
`--format text` (human-readable). `--limit` caps the list; `--budget-tokens`
trims the lowest-scored entries until the rough token count (chars/4) fits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.test_edges import _is_test_path


_KIND_BOOST = {"module": 5, "class": 3}


def build_repo_map(conn, *, limit: int = 100) -> dict:
    """Return the repo-map payload. Reusable helper for the MCP surface
    and the `ask` natural-language layer.
    """
    entries = _collect(conn)
    if limit and limit > 0:
        entries = entries[: int(limit)]
    return {"symbols": entries}


def _collect(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.symbol_pk,
               s.canonical_name,
               s.kind,
               s.signature_norm,
               (SELECT f.file_path FROM occurrences o
                  JOIN files f ON f.file_pk = o.file_pk
                 WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_file,
               (SELECT o.start_line FROM occurrences o
                 WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_line,
               (SELECT COUNT(*) FROM relations r
                 WHERE r.dst_symbol_pk = s.symbol_pk) AS in_degree,
               (SELECT COUNT(*) FROM test_edges te
                 WHERE te.target_symbol_pk = s.symbol_pk) AS test_count
          FROM symbols s
         WHERE s.deleted_at IS NULL
        """,
    ).fetchall()

    entries: list[dict] = []
    for r in rows:
        def_file = r["def_file"]
        if def_file is None:
            continue
        if _is_test_path(def_file):
            continue
        kind = r["kind"] or ""
        in_degree = int(r["in_degree"] or 0)
        test_count = int(r["test_count"] or 0)
        score = in_degree + test_count + _KIND_BOOST.get(kind, 0)
        entries.append(
            {
                "canonical_name": r["canonical_name"],
                "kind": kind,
                "def_file": def_file,
                "def_line": int(r["def_line"]) if r["def_line"] is not None else None,
                "signature": r["signature_norm"] or "",
                "in_degree": in_degree,
                "test_count": test_count,
                "score": score,
            }
        )
    # Sort by descending score, tiebreak by canonical_name for determinism.
    entries.sort(key=lambda e: (-e["score"], e["canonical_name"]))
    return entries


def _format_text(entries: list[dict]) -> str:
    lines: list[str] = []
    for e in entries:
        sig = e["signature"] or ""
        sig_part = f" :: {sig}" if sig else ""
        loc = (
            f"({e['def_file']}:{e['def_line']})"
            if e["def_line"] is not None
            else f"({e['def_file']})"
        )
        lines.append(f"[{e['kind']}] {e['canonical_name']}{sig_part}  {loc}")
    return "\n".join(lines)


def _format_json(entries: list[dict]) -> str:
    return json.dumps({"symbols": entries}, indent=2)


def _trim_to_budget(entries: list[dict], budget_tokens: int, fmt: str) -> list[dict]:
    """Drop lowest-scored entries until chars/4 <= budget_tokens."""
    if budget_tokens <= 0:
        return entries
    current = list(entries)
    while current:
        rendered = _format_json(current) if fmt == "json" else _format_text(current)
        if len(rendered) / 4 <= budget_tokens:
            return current
        current.pop()
    return current


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
        entries = _collect(conn)
    finally:
        db_mod.close(conn)

    limit = max(0, int(args.limit))
    entries = entries[:limit]

    fmt = args.format
    budget = getattr(args, "budget_tokens", None)
    if budget is not None and budget > 0:
        entries = _trim_to_budget(entries, int(budget), fmt)

    if fmt == "json":
        print(_format_json(entries))
    else:
        text = _format_text(entries)
        if text:
            print(text)
    return 0
