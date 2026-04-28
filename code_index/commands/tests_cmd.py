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
from code_index import db_router as db_mod
from code_index.commands.impact_cmd import _resolve_target
from code_index.runners.pytest import build_pytest_invocation


def _affected(
    conn,
    target_pk: int,
    *,
    match_reason: str = "exact",
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.symbol_uid, s.canonical_name, s.kind,
               te.edge_type, te.depth, te.confidence, te.path_json,
               te.provenance,
               te.target_symbol_pk,
               target.symbol_uid AS matched_symbol_uid,
               target.canonical_name AS matched_canonical_name,
               target.kind AS matched_kind,
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
          JOIN symbols target ON target.symbol_pk = te.target_symbol_pk
         WHERE te.target_symbol_pk = ?
           AND s.deleted_at IS NULL
           AND target.deleted_at IS NULL
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
        d["match_reason"] = match_reason
        d["matched_target"] = {
            "symbol_pk": int(d["target_symbol_pk"]),
            "symbol_uid": d["matched_symbol_uid"],
            "canonical_name": d["matched_canonical_name"],
            "kind": d["matched_kind"],
        }
        d.pop("matched_symbol_uid", None)
        d.pop("matched_canonical_name", None)
        d.pop("matched_kind", None)
        d.pop("context_json", None)
        out.append(d)
    return out


def _definition_file(conn, symbol_pk: int) -> str | None:
    row = conn.execute(
        """
        SELECT f.file_path
          FROM occurrences o
          JOIN files f ON f.file_pk = o.file_pk
         WHERE o.symbol_pk = ?
           AND o.role = 'definition'
           AND f.deleted_at IS NULL
         ORDER BY o.start_line ASC
         LIMIT 1
        """,
        (symbol_pk,),
    ).fetchone()
    return row["file_path"] if row else None


def _related_targets(conn, target: dict, *, limit: int = 50) -> list[dict]:
    """Find nearby symbols whose tests are useful when exact edges are absent."""

    target_pk = int(target["symbol_pk"])
    target_kind = str(target.get("kind") or "")
    related: list[dict] = []
    seen: set[int] = {target_pk}

    def add_rows(rows, reason: str) -> None:
        for row in rows:
            symbol_pk = int(row["symbol_pk"])
            if symbol_pk in seen:
                continue
            seen.add(symbol_pk)
            related.append(
                {
                    "symbol_pk": symbol_pk,
                    "symbol_uid": row["symbol_uid"],
                    "canonical_name": row["canonical_name"],
                    "kind": row["kind"],
                    "match_reason": reason,
                }
            )
            if len(related) >= limit:
                return

    if target_kind == "class":
        descendants = conn.execute(
            """
            WITH RECURSIVE descendants(symbol_pk, depth) AS (
                SELECT symbol_pk, 1
                  FROM symbols
                 WHERE container_symbol_pk = ?
                   AND deleted_at IS NULL
                UNION ALL
                SELECT s.symbol_pk, d.depth + 1
                  FROM symbols s
                  JOIN descendants d ON s.container_symbol_pk = d.symbol_pk
                 WHERE s.deleted_at IS NULL
                   AND d.depth < 4
            )
            SELECT s.symbol_pk, s.symbol_uid, s.canonical_name, s.kind
              FROM descendants d
              JOIN symbols s ON s.symbol_pk = d.symbol_pk
             ORDER BY d.depth ASC, s.kind ASC, s.canonical_name ASC
             LIMIT ?
            """,
            (target_pk, limit),
        ).fetchall()
        add_rows(descendants, "descendant")

    if len(related) < limit and target.get("container_symbol_pk") is not None:
        container_pk = int(target["container_symbol_pk"])
        container_rows = conn.execute(
            """
            SELECT symbol_pk, symbol_uid, canonical_name, kind
              FROM symbols
             WHERE symbol_pk = ?
               AND deleted_at IS NULL
            """,
            (container_pk,),
        ).fetchall()
        add_rows(container_rows, "container")
        if len(related) < limit and target_kind in {"method", "function"}:
            siblings = conn.execute(
                """
                SELECT symbol_pk, symbol_uid, canonical_name, kind
                  FROM symbols
                 WHERE container_symbol_pk = ?
                   AND symbol_pk <> ?
                   AND deleted_at IS NULL
                 ORDER BY kind ASC, canonical_name ASC
                 LIMIT ?
                """,
                (container_pk, target_pk, limit - len(related)),
            ).fetchall()
            add_rows(siblings, "sibling")

    if len(related) < limit:
        def_file = _definition_file(conn, target_pk)
        if def_file:
            file_rows = conn.execute(
                """
                SELECT DISTINCT s.symbol_pk, s.symbol_uid, s.canonical_name, s.kind
                  FROM symbols s
                  JOIN occurrences o ON o.symbol_pk = s.symbol_pk
                  JOIN files f ON f.file_pk = o.file_pk
                 WHERE f.file_path = ?
                   AND o.role = 'definition'
                   AND s.symbol_pk <> ?
                   AND s.deleted_at IS NULL
                   AND f.deleted_at IS NULL
                 ORDER BY
                   CASE s.kind
                     WHEN 'class' THEN 0
                     WHEN 'method' THEN 1
                     WHEN 'function' THEN 2
                     ELSE 3
                   END,
                   s.canonical_name ASC
                 LIMIT ?
                """,
                (def_file, target_pk, limit - len(related)),
            ).fetchall()
            add_rows(file_rows, "same_file")

    return related


def _affected_with_related_fallback(
    conn,
    target: dict,
) -> tuple[list[dict], list[dict], str]:
    exact = _affected(conn, int(target["symbol_pk"]))
    if exact:
        matched = [
            {
                "symbol_pk": int(target["symbol_pk"]),
                "symbol_uid": target["symbol_uid"],
                "canonical_name": target["canonical_name"],
                "kind": target["kind"],
                "match_reason": "exact",
            }
        ]
        return exact, matched, "exact"

    related_targets = _related_targets(conn, target)
    affected: list[dict] = []
    by_test: dict[str, int] = {}
    matched_with_edges: list[dict] = []
    for related in related_targets:
        rows = _affected(
            conn,
            int(related["symbol_pk"]),
            match_reason=str(related["match_reason"]),
        )
        if rows:
            matched_with_edges.append(related)
        for row in rows:
            key = row["symbol_uid"]
            existing_index = by_test.get(key)
            if existing_index is None:
                by_test[key] = len(affected)
                affected.append(row)
                continue
            current = affected[existing_index]
            if (row["depth"], row["matched_target"]["canonical_name"]) < (
                current["depth"],
                current["matched_target"]["canonical_name"],
            ):
                affected[existing_index] = row

    affected.sort(
        key=lambda row: (
            row["depth"],
            row["matched_target"]["canonical_name"],
            row["canonical_name"],
        )
    )
    return affected, matched_with_edges, "related" if affected else "exact"


def _resolve_input(conn, raw: str) -> list[dict]:
    # Accept a full symbol_uid first (20-hex chars in our scheme).
    if raw and len(raw) == 20 and all(ch in "0123456789abcdef" for ch in raw):
        row = conn.execute(
            "SELECT symbol_pk, symbol_uid, kind, canonical_name, display_name, container_symbol_pk FROM symbols WHERE symbol_uid = ? AND deleted_at IS NULL",
            (raw,),
        ).fetchone()
        if row:
            return [dict(row)]
    candidates = _resolve_target(conn, raw)
    if not candidates:
        return candidates
    pks = [int(candidate["symbol_pk"]) for candidate in candidates]
    placeholders = ",".join("?" for _ in pks)
    rows = conn.execute(
        f"""
        SELECT symbol_pk, container_symbol_pk
          FROM symbols
         WHERE symbol_pk IN ({placeholders})
        """,
        pks,
    ).fetchall()
    containers = {int(row["symbol_pk"]): row["container_symbol_pk"] for row in rows}
    for candidate in candidates:
        candidate["container_symbol_pk"] = containers.get(int(candidate["symbol_pk"]))
    return candidates


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
        affected, matched_targets, match_scope = _affected_with_related_fallback(
            conn, target
        )

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
                    "match_reason": row["match_reason"],
                    "matched_target": row["matched_target"],
                }
                for row in affected
            ],
            "affected_test_files": files,
            "matched_targets": matched_targets,
            "summary": {
                "affected_test_count": len(affected),
                "direct": direct_count,
                "transitive": transitive_count,
                "affected_test_file_count": len(files),
                "match_scope": match_scope,
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
                "When an exact target has no materialized test edge, class/method queries fall back to related symbols in the same container or file and mark those rows as related.",
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
    if s.get("match_scope") == "related":
        print("  no exact test edges; showing related class/file coverage")
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
