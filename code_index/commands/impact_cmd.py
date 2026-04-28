"""`code_index impact`: best-effort symbol impact analysis.

Given a symbol (by canonical name, display name, or substring), walk the
relation graph *inward* (who depends on this) to identify impacted symbols
and their defining files.

Edges considered, in declining confidence:

  high:    calls          (dst = target; caller is impacted)
           inherits       (dst = target; subclass is impacted)
           contains       (dst = target; enclosing scope is impacted)
  medium:  imports        (dst = target's module; importer module is
                           impacted but may not use the specific target)

Each impacted symbol carries:
  - symbol_uid, kind, canonical_name, def_file, def_line
  - depth (hop count from target)
  - path: list of (relation_kind, intermediate_symbol) describing the hop
  - confidence: 'high' | 'medium' derived from edges traversed

Output also returns:
  - unresolved_call_count: number of calls in the DB that couldn't be
    resolved to a symbol (external / stdlib) — context for agent reasoning.
  - limitations: explicit notes on what this command does not yet model.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod


HIGH_EDGES = ("calls", "inherits", "contains")
MEDIUM_EDGES = ("imports",)


@dataclass
class ImpactHop:
    relation_kind: str
    intermediate: str  # canonical name at previous hop
    line: int | None = None


@dataclass
class ImpactedSymbol:
    symbol_uid: str
    canonical_name: str
    kind: str
    def_file: str | None
    def_line: int | None
    depth: int
    confidence: str
    path: list[ImpactHop] = field(default_factory=list)


def _resolve_target(conn: sqlite3.Connection, name: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT symbol_pk, symbol_uid, kind, canonical_name, display_name
          FROM symbols
         WHERE deleted_at IS NULL
           AND (canonical_name = ?
                OR display_name = ?
                OR canonical_name LIKE ?
                OR display_name LIKE ?)
         ORDER BY (canonical_name = ?) DESC,
                  (display_name = ?) DESC,
                  LENGTH(canonical_name) ASC
         LIMIT 5
        """,
        (name, name, f"%{name}%", f"%{name}%", name, name),
    ).fetchall()
    return [dict(r) for r in rows]


def _def_location(
    conn: sqlite3.Connection, symbol_pk: int
) -> tuple[str | None, int | None]:
    row = conn.execute(
        """
        SELECT f.file_path, o.start_line
          FROM occurrences o
          JOIN files f ON f.file_pk = o.file_pk
         WHERE o.symbol_pk = ? AND o.role = 'definition'
         ORDER BY o.start_line ASC
         LIMIT 1
        """,
        (symbol_pk,),
    ).fetchone()
    if row is None:
        return None, None
    return row["file_path"], row["start_line"]


def _incoming(
    conn: sqlite3.Connection,
    symbol_pk: int,
    kinds: tuple[str, ...],
) -> list[dict]:
    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"""
        SELECT r.relation_kind, r.provenance, r.src_symbol_pk,
               s.symbol_uid, s.canonical_name, s.kind
          FROM relations r
          JOIN symbols s ON s.symbol_pk = r.src_symbol_pk
         WHERE r.dst_symbol_pk = ?
           AND r.relation_kind IN ({placeholders})
           AND s.deleted_at IS NULL
        """,
        (symbol_pk, *kinds),
    ).fetchall()
    return [dict(r) for r in rows]


def _module_symbol_pk_of(conn: sqlite3.Connection, symbol_pk: int) -> int | None:
    """Walk container pointers to the enclosing module symbol."""
    cur_pk = symbol_pk
    seen: set[int] = set()
    for _ in range(16):
        if cur_pk in seen:
            return None
        seen.add(cur_pk)
        row = conn.execute(
            "SELECT symbol_pk, kind, container_symbol_pk FROM symbols WHERE symbol_pk = ?",
            (cur_pk,),
        ).fetchone()
        if row is None:
            return None
        if row["kind"] == "module":
            return int(row["symbol_pk"])
        if row["container_symbol_pk"] is None:
            return None
        cur_pk = int(row["container_symbol_pk"])
    return None


def compute_impact(
    conn: sqlite3.Connection,
    target_pk: int,
    *,
    max_depth: int = 2,
    include_imports: bool = True,
) -> dict:
    target_row = conn.execute(
        "SELECT symbol_pk, symbol_uid, kind, canonical_name FROM symbols WHERE symbol_pk = ?",
        (target_pk,),
    ).fetchone()
    if target_row is None:
        return {"error": "target not found"}
    target = dict(target_row)
    def_file, def_line = _def_location(conn, target_pk)
    target["def_file"] = def_file
    target["def_line"] = def_line

    impacted: dict[str, ImpactedSymbol] = {}

    # BFS outward (inbound edges) from target.
    # Frontier entries: (symbol_pk, depth, prev_canonical_name, edge_kind)
    frontier: list[tuple[int, int, str | None, str | None]] = [
        (target_pk, 0, None, None)
    ]
    visited: set[int] = set()
    kinds = HIGH_EDGES + (MEDIUM_EDGES if include_imports else tuple())

    while frontier:
        sym_pk, depth, prev_name, edge_kind = frontier.pop(0)
        if sym_pk in visited:
            continue
        visited.add(sym_pk)

        # Record everything except the target itself as impacted.
        if sym_pk != target_pk:
            sym_row = conn.execute(
                "SELECT symbol_uid, kind, canonical_name FROM symbols WHERE symbol_pk = ?",
                (sym_pk,),
            ).fetchone()
            if sym_row is None:
                continue
            f, ln = _def_location(conn, sym_pk)
            confidence = "high" if edge_kind in HIGH_EDGES else "medium"
            existing = impacted.get(sym_row["symbol_uid"])
            hop = ImpactHop(
                relation_kind=edge_kind or "?",
                intermediate=prev_name or target["canonical_name"],
            )
            if existing is None or depth < existing.depth:
                impacted[sym_row["symbol_uid"]] = ImpactedSymbol(
                    symbol_uid=sym_row["symbol_uid"],
                    canonical_name=sym_row["canonical_name"],
                    kind=sym_row["kind"],
                    def_file=f,
                    def_line=ln,
                    depth=depth,
                    confidence=confidence,
                    path=[hop],
                )
            else:
                # Upgrade confidence if we reached the same symbol via a
                # higher-confidence edge.
                if confidence == "high" and existing.confidence == "medium":
                    existing.confidence = "high"

        if depth >= max_depth:
            continue

        # For direct-edge traversal we query by the current symbol.
        incoming = _incoming(conn, sym_pk, kinds)
        # For imports, the inbound edge on the *module* of the target is the
        # signal — traverse from the target's module when depth == 0.
        if include_imports and sym_pk == target_pk:
            mod_pk = _module_symbol_pk_of(conn, target_pk)
            if mod_pk is not None and mod_pk != target_pk:
                incoming.extend(_incoming(conn, mod_pk, ("imports",)))

        for edge in incoming:
            next_pk = int(edge["src_symbol_pk"])
            if next_pk in visited:
                continue
            frontier.append(
                (next_pk, depth + 1, edge["canonical_name"], edge["relation_kind"])
            )

    impacted_list = sorted(
        impacted.values(),
        key=lambda s: (s.depth, 0 if s.confidence == "high" else 1, s.canonical_name),
    )
    impacted_files = sorted({s.def_file for s in impacted_list if s.def_file})

    total_calls = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE relation_kind='calls'"
    ).fetchone()[0]
    # Inbound edges directly touching the target (defensible rationale).
    direct_callers = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE relation_kind='calls' AND dst_symbol_pk = ?",
        (target_pk,),
    ).fetchone()[0]
    direct_subclasses = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE relation_kind='inherits' AND dst_symbol_pk = ?",
        (target_pk,),
    ).fetchone()[0]

    return {
        "target": {
            "symbol_uid": target["symbol_uid"],
            "canonical_name": target["canonical_name"],
            "kind": target["kind"],
            "def_file": target["def_file"],
            "def_line": target["def_line"],
        },
        "parameters": {"max_depth": max_depth, "include_imports": include_imports},
        "impacted_symbols": [
            {
                "symbol_uid": s.symbol_uid,
                "canonical_name": s.canonical_name,
                "kind": s.kind,
                "def_file": s.def_file,
                "def_line": s.def_line,
                "depth": s.depth,
                "confidence": s.confidence,
                "path": [
                    {"relation_kind": h.relation_kind, "intermediate": h.intermediate}
                    for h in s.path
                ],
            }
            for s in impacted_list
        ],
        "impacted_files": list(impacted_files),
        "summary": {
            "impacted_symbol_count": len(impacted_list),
            "impacted_file_count": len(impacted_files),
            "direct_callers": direct_callers,
            "direct_subclasses": direct_subclasses,
            "total_resolved_calls_in_index": total_calls,
        },
        "limitations": [
            "Only in-repo symbols are modeled. External/stdlib calls are not tracked.",
            "Transitive graph walk is bounded by max_depth (default 2).",
            "Relative imports and dynamic attribute access are not resolved.",
            "Affected tests are not yet surfaced (see `code_index tests`, reserved).",
        ],
    }


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    if not args.symbol:
        print("error: provide a symbol (canonical name, display name, or substring)")
        return 2

    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        candidates = _resolve_target(conn, args.symbol)
        if not candidates:
            payload = {
                "query": args.symbol,
                "error": "no matching symbol",
                "candidates": [],
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"no symbol matching {args.symbol!r}")
            return 2

        target = candidates[0]
        result = compute_impact(
            conn,
            int(target["symbol_pk"]),
            max_depth=args.max_depth,
            include_imports=not args.no_imports,
        )
        result["query"] = args.symbol
        result["candidate_matches"] = [
            {
                "canonical_name": c["canonical_name"],
                "kind": c["kind"],
                "symbol_uid": c["symbol_uid"],
            }
            for c in candidates
        ]
    finally:
        db_mod.close(conn)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    t = result["target"]
    print(
        f"impact of [{t['kind']}] {t['canonical_name']}  ({t['def_file']}:{t['def_line']})"
    )
    print(f"  max_depth={result['parameters']['max_depth']}")
    if not result["impacted_symbols"]:
        print("  no impacted symbols (no inbound edges found)")
    else:
        print(
            f"  {result['summary']['impacted_symbol_count']} impacted symbols "
            f"in {result['summary']['impacted_file_count']} files:"
        )
        for s in result["impacted_symbols"][:20]:
            loc = f"{s['def_file']}:{s['def_line']}" if s["def_file"] else "?"
            hop = (
                s["path"][0]
                if s["path"]
                else {"relation_kind": "?", "intermediate": "?"}
            )
            print(
                f"    [{s['confidence']}] depth={s['depth']} "
                f"{hop['relation_kind']:<8} {s['canonical_name']}  ({loc})"
            )
        if len(result["impacted_symbols"]) > 20:
            print(f"    ... +{len(result['impacted_symbols']) - 20} more")
    return 0
