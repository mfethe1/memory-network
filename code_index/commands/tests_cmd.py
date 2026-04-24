"""`code_index tests <target>`: affected-tests lookup (direct + transitive).

Queries the `test_edges` table materialized during every reindex. Each edge
carries depth (1 = direct call; >1 = transitive) and a path_json with the
ordered symbol chain so downstream tools can render rationale.

Input forms:
- symbol_uid         → exact match on symbols.symbol_uid
- canonical name     → exact match
- substring          → LIKE match (same fallback as `code_index symbol`)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands.impact_cmd import _resolve_target
from code_index.runners.pytest import build_pytest_invocation


def _affected(conn, target_pk: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.symbol_uid, s.canonical_name, s.kind,
               te.edge_type, te.depth, te.confidence, te.path_json,
               te.provenance,
               (SELECT f.file_path FROM occurrences o
                  JOIN files f ON f.file_pk = o.file_pk
                 WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_file,
               (SELECT o.start_line FROM occurrences o
                 WHERE o.symbol_pk = s.symbol_pk AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_line,
               (SELECT c.context_json FROM chunks c
                 WHERE c.primary_symbol_pk = s.symbol_pk
                   AND c.deleted_at IS NULL
                 ORDER BY c.chunk_pk ASC LIMIT 1) AS context_json
          FROM test_edges te
          JOIN symbols s ON s.symbol_pk = te.test_symbol_pk
         WHERE te.target_symbol_pk = ?
           AND s.deleted_at IS NULL
         ORDER BY te.depth ASC, s.canonical_name ASC
        """,
        (target_pk,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            path = json.loads(d.get("path_json") or "[]")
        except Exception:
            path = []
        d["path"] = path
        # Human-friendly rationale: "test → a → b → target"
        if path:
            d["rationale"] = " → ".join(path)
        else:
            d["rationale"] = d["canonical_name"]
        # Surface @pytest.mark.parametrize summary when the test carries one.
        param = None
        try:
            ctx = json.loads(d.get("context_json") or "{}")
            param = ctx.get("parametrize")
        except Exception:
            param = None
        d["parametrize"] = param
        d.pop("context_json", None)
        out.append(d)
    return out


def _resolve_input(conn, raw: str) -> list[dict]:
    # Accept a full symbol_uid first (20-hex chars in our scheme).
    if raw and len(raw) == 20 and all(ch in "0123456789abcdef" for ch in raw):
        row = conn.execute(
            "SELECT symbol_pk, symbol_uid, kind, canonical_name, display_name FROM symbols WHERE symbol_uid = ? AND deleted_at IS NULL",
            (raw,),
        ).fetchone()
        if row:
            return [dict(row)]
    return _resolve_target(conn, raw)


def run(args: argparse.Namespace) -> int:
    runner = getattr(args, "runner", None)
    runner_json = getattr(args, "runner_json", False)
    if runner and runner != "pytest":
        print(json.dumps({"error": "unknown runner", "supported": ["pytest"]}))
        return 2

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    if not args.symbol:
        print(
            "error: provide a symbol (symbol_uid, canonical name, display name, or substring)"
        )
        return 2

    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        candidates = _resolve_input(conn, args.symbol)
        if not candidates:
            payload = {"query": args.symbol, "error": "no matching symbol"}
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"no symbol matching {args.symbol!r}")
            return 2
        target = candidates[0]
        affected = _affected(conn, int(target["symbol_pk"]))

        # Optional filter flags.
        if args.direct_only:
            affected = [row for row in affected if row["edge_type"] == "direct"]
        if args.max_depth is not None:
            affected = [row for row in affected if row["depth"] <= args.max_depth]

        direct_count = sum(1 for row in affected if row["edge_type"] == "direct")
        transitive_count = sum(
            1 for row in affected if row["edge_type"] == "transitive"
        )
        files = sorted({row["def_file"] for row in affected if row["def_file"]})
        payload = {
            "query": args.symbol,
            "target": {
                "symbol_uid": target["symbol_uid"],
                "canonical_name": target["canonical_name"],
                "kind": target["kind"],
            },
            "candidate_matches": [
                {
                    "canonical_name": c["canonical_name"],
                    "kind": c["kind"],
                    "symbol_uid": c["symbol_uid"],
                }
                for c in candidates
            ],
            "affected_tests": [
                {
                    "symbol_uid": row["symbol_uid"],
                    "canonical_name": row["canonical_name"],
                    "kind": row["kind"],
                    "def_file": row["def_file"],
                    "def_line": row["def_line"],
                    "edge_type": row["edge_type"],
                    "depth": row["depth"],
                    "confidence": row["confidence"],
                    "path": row["path"],
                    "rationale": row["rationale"],
                    "parametrize": row["parametrize"],
                }
                for row in affected
            ],
            "affected_test_files": files,
            "summary": {
                "affected_test_count": len(affected),
                "direct": direct_count,
                "transitive": transitive_count,
                "affected_test_file_count": len(files),
                "parametrized_test_count": sum(
                    1 for row in affected if row.get("parametrize")
                ),
                "parametrized_case_total": sum(
                    (row.get("parametrize") or {}).get("case_count") or 0
                    for row in affected
                ),
            },
            "limitations": [
                "Test discovery is file-name heuristic: tests/*, test_*.py, *_test.py, conftest.py.",
                "Edges follow 'calls' relations. Indirect use via fixtures is not resolved.",
                "BFS depth is bounded (default 4 hops) during materialization.",
                "Edges only exist to targets whose definition lives outside test files.",
                "Default JSON groups parametrized tests under one test symbol. "
                "`--runner pytest` expands captured literal cases into node ids, but "
                "non-literal case values are reported as skipped.",
            ],
        }
    finally:
        db_mod.close(conn)

    if runner == "pytest":
        runner_payload = build_pytest_invocation(affected)
        if runner_json:
            print(json.dumps(runner_payload, indent=2))
        else:
            for node_id in runner_payload["node_ids"]:
                print(node_id)
        return 0

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    t = payload["target"]
    s = payload["summary"]
    print(f"tests affected by {t['canonical_name']} ({t['kind']}):")
    if not affected:
        print("  none")
        return 0
    print(
        f"  {s['affected_test_count']} tests "
        f"(direct={s['direct']}, transitive={s['transitive']}) "
        f"in {s['affected_test_file_count']} files"
    )
    for row in affected[:30]:
        loc = f"{row['def_file']}:{row['def_line']}" if row["def_file"] else "?"
        print(
            f"    [{row['edge_type']} d={row['depth']} conf={row['confidence']:.2f}] "
            f"{row['canonical_name']}  ({loc})"
        )
        print(f"        via: {row['rationale']}")
    if len(affected) > 30:
        print(f"    ... +{len(affected) - 30} more")
    return 0
