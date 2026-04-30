"""Affected-test edge rebuild helpers for the indexing pipeline."""

from __future__ import annotations

from collections import deque
import json
import sqlite3
from typing import Any


def _is_test_path(path: str) -> bool:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py":
        return True
    if "/tests/" in "/" + low or low.startswith("tests/"):
        return True
    return False


def _collect_test_targets_for(
    conn: sqlite3.Connection,
    test_pk: int,
    test_name: str,
    *,
    max_depth: int,
    def_file_cache: dict[int, str | None],
) -> dict[int, tuple[int, list[str]]]:
    """BFS outward from one test symbol. Returns {dst_pk: (depth, path_names)}."""

    def _def_file(sym_pk: int) -> str | None:
        if sym_pk in def_file_cache:
            return def_file_cache[sym_pk]
        row = conn.execute(
            """
            SELECT f.file_path FROM occurrences o
              JOIN files f ON f.file_pk = o.file_pk
             WHERE o.symbol_pk = ? AND o.role = 'definition'
             ORDER BY o.start_line ASC LIMIT 1
            """,
            (sym_pk,),
        ).fetchone()
        val = row["file_path"] if row else None
        def_file_cache[sym_pk] = val
        return val

    frontier: deque[tuple[int, int, list[str]]] = deque([(test_pk, 0, [test_name])])
    seen: dict[int, int] = {test_pk: 0}
    best_targets: dict[int, tuple[int, list[str]]] = {}

    while frontier:
        sym_pk, depth, path_names = frontier.popleft()
        if depth >= max_depth:
            continue
        out = conn.execute(
            """
            SELECT r.dst_symbol_pk, s.canonical_name
              FROM relations r
              JOIN symbols s ON s.symbol_pk = r.dst_symbol_pk
             WHERE r.src_symbol_pk = ?
               AND r.relation_kind = 'calls'
               AND s.deleted_at IS NULL
            """,
            (sym_pk,),
        ).fetchall()
        if depth == 0:
            contains = conn.execute(
                """
                SELECT r.dst_symbol_pk, s.canonical_name
                  FROM relations r
                  JOIN symbols s ON s.symbol_pk = r.dst_symbol_pk
                 WHERE r.src_symbol_pk = ?
                   AND r.relation_kind = 'contains'
                   AND s.deleted_at IS NULL
                """,
                (sym_pk,),
            ).fetchall()
            out = list(out) + list(contains)
        for row in out:
            dst_pk = int(row["dst_symbol_pk"])
            dst_name = row["canonical_name"]
            new_depth = depth + 1
            prior = seen.get(dst_pk)
            if prior is not None and prior <= new_depth:
                continue
            seen[dst_pk] = new_depth
            new_path = path_names + [dst_name]
            df = _def_file(dst_pk)
            if df is not None and not _is_test_path(df):
                prev = best_targets.get(dst_pk)
                if prev is None or new_depth < prev[0]:
                    best_targets[dst_pk] = (new_depth, new_path)
            frontier.append((dst_pk, new_depth, new_path))

    return best_targets


def _insert_test_edges_for(
    conn: sqlite3.Connection,
    *,
    test_chunk_pk: int,
    test_pk: int,
    best_targets: dict[int, tuple[int, list[str]]],
) -> int:
    inserted = 0
    for dst_pk, (depth, path_names) in best_targets.items():
        edge_type = "direct" if depth == 1 else "transitive"
        confidence = max(0.3, 1.0 - 0.15 * (depth - 1))
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO test_edges(
                test_chunk_pk, test_symbol_pk, target_symbol_pk,
                edge_type, depth, confidence, path_json, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pipeline:bfs')
            """,
            (
                test_chunk_pk,
                test_pk,
                dst_pk,
                edge_type,
                depth,
                confidence,
                json.dumps(path_names),
            ),
        )
        if cur.rowcount:
            inserted += 1
    return inserted


def _rebuild_edges_for_test_rows(
    conn: sqlite3.Connection,
    test_rows: list,
    *,
    max_depth: int,
) -> int:
    def_file_cache: dict[int, str | None] = {}
    inserted = 0
    for row in test_rows:
        test_pk = int(row["symbol_pk"])
        chunk_row = conn.execute(
            "SELECT chunk_pk FROM chunks WHERE primary_symbol_pk = ? AND deleted_at IS NULL LIMIT 1",
            (test_pk,),
        ).fetchone()
        if chunk_row is None:
            continue
        inserted += _insert_test_edges_for(
            conn,
            test_chunk_pk=int(chunk_row["chunk_pk"]),
            test_pk=test_pk,
            best_targets=_collect_test_targets_for(
                conn,
                test_pk,
                row["canonical_name"],
                max_depth=max_depth,
                def_file_cache=def_file_cache,
            ),
        )
    return inserted


def _collect_scoped_test_symbols(
    conn: sqlite3.Connection,
    touched_file_pks: set[int],
) -> set[int]:
    """Return test symbols whose affected-test edges may depend on touched files.

    Scope: test symbols whose
    definition OR whose existing edge target lives in a touched file.

    Correctness: a test T's reachability to some target X can change only if
    (a) T's own body changed — T's file was touched, or (b) something on
    T's reachability path changed — the edge's target file was touched.
    Test symbols that can't satisfy either condition keep their edges.
    """
    if not touched_file_pks:
        return set()
    placeholders = ",".join("?" for _ in touched_file_pks)
    file_pk_params = list(touched_file_pks)

    # Test symbols whose definition is in a touched file.
    defn_tests = conn.execute(
        f"""
        SELECT DISTINCT s.symbol_pk, s.canonical_name
          FROM symbols s
          JOIN occurrences o ON o.symbol_pk = s.symbol_pk
          JOIN files f ON f.file_pk = o.file_pk
         WHERE o.role = 'definition'
           AND s.deleted_at IS NULL
           AND f.deleted_at IS NULL
           AND o.file_pk IN ({placeholders})
        """,
        file_pk_params,
    ).fetchall()

    impacted: set[int] = set()
    for r in defn_tests:
        fp = conn.execute(
            """
            SELECT f.file_path FROM occurrences o
              JOIN files f ON f.file_pk = o.file_pk
             WHERE o.symbol_pk = ? AND o.role = 'definition'
             ORDER BY o.start_line ASC LIMIT 1
            """,
            (r["symbol_pk"],),
        ).fetchone()
        if fp and _is_test_path(fp["file_path"]):
            impacted.add(int(r["symbol_pk"]))

    # Test symbols whose existing edges target a symbol defined in a touched file.
    target_tests = conn.execute(
        f"""
        SELECT DISTINCT te.test_symbol_pk
          FROM test_edges te
          JOIN symbols s ON s.symbol_pk = te.test_symbol_pk
          JOIN occurrences o ON o.symbol_pk = te.target_symbol_pk
         WHERE o.role = 'definition'
           AND o.file_pk IN ({placeholders})
           AND s.deleted_at IS NULL
        """,
        file_pk_params,
    ).fetchall()
    for r in target_tests:
        impacted.add(int(r["test_symbol_pk"]))
    return impacted


def _rebuild_test_edges_for_test_symbols(
    conn: sqlite3.Connection,
    stats: Any,
    test_symbol_pks: set[int],
    *,
    max_depth: int = 4,
) -> None:
    """Recompute outbound affected-test edges for specific test symbols."""
    if not test_symbol_pks:
        return

    impacted_placeholders = ",".join("?" for _ in test_symbol_pks)
    impacted_params = list(test_symbol_pks)
    before = conn.execute(
        f"SELECT COUNT(*) FROM test_edges WHERE test_symbol_pk IN ({impacted_placeholders})",
        impacted_params,
    ).fetchone()[0]
    conn.execute(
        f"DELETE FROM test_edges WHERE test_symbol_pk IN ({impacted_placeholders})",
        impacted_params,
    )

    live_tests = conn.execute(
        f"""
        SELECT symbol_pk, canonical_name
          FROM symbols
         WHERE deleted_at IS NULL
           AND symbol_pk IN ({impacted_placeholders})
        """,
        impacted_params,
    ).fetchall()

    stats.test_edges_inserted += _rebuild_edges_for_test_rows(
        conn,
        live_tests,
        max_depth=max_depth,
    )
    stats.test_edges_removed += before


def _rebuild_test_edges_scoped(
    conn: sqlite3.Connection,
    stats: Any,
    touched_file_pks: set[int],
    *,
    max_depth: int = 4,
) -> None:
    impacted = _collect_scoped_test_symbols(conn, touched_file_pks)
    _rebuild_test_edges_for_test_symbols(
        conn,
        stats,
        impacted,
        max_depth=max_depth,
    )
    stats.test_edges_rebuilt_scope = "scoped"


def _rebuild_test_edges(
    conn: sqlite3.Connection,
    stats: Any,
    *,
    max_depth: int = 4,
) -> None:
    """Populate test_edges from the current relation graph.

    For each test symbol, BFS outward along `calls` edges up to `max_depth`
    hops. Any target whose own definition is in a non-test file becomes an
    edge, with `edge_type='direct'` at depth=1 and `edge_type='transitive'`
    beyond. `path_json` stores the ordered canonical-name chain from the
    test symbol to the target so downstream tools can render rationale.

    We also include depth=1 edges reached via `contains` from the test
    module → test function, so module-level targets of test funcs still
    register against the module symbol in DB-level queries.
    """
    before = conn.execute("SELECT COUNT(*) FROM test_edges").fetchone()[0]
    conn.execute("DELETE FROM test_edges")

    # Collect candidate test symbols: definitions in test-looking files.
    test_defs = conn.execute(
        """
        SELECT DISTINCT s.symbol_pk, s.canonical_name, f.file_path
          FROM symbols s
          JOIN occurrences o ON o.symbol_pk = s.symbol_pk
          JOIN files f ON f.file_pk = o.file_pk
         WHERE o.role = 'definition'
           AND s.deleted_at IS NULL
           AND f.deleted_at IS NULL
        """
    ).fetchall()

    test_rows = [r for r in test_defs if _is_test_path(r["file_path"])]
    if not test_rows:
        return

    stats.test_edges_inserted = _rebuild_edges_for_test_rows(
        conn,
        test_rows,
        max_depth=max_depth,
    )
    stats.test_edges_removed = before
