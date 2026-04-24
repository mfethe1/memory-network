"""Jedi-augmented resolver tier.

Jedi is invoked from inside the pipeline resolver as an additional
candidate source, not as a standalone post-pass. This closes the
wrong-edges window where AST resolution would otherwise land a bad
suffix match before Jedi ever saw the call-site.

Contract:

- `is_available()` — returns True iff `jedi` is importable.
- `resolve_pending_via_jedi(config, conn, records)` — given a list of
  call-site records `{src_symbol_uid, file_pk, line, column}`, returns a
  mapping `{(src_symbol_uid, file_pk, line): [candidate_canonical, ...]}`
  that the pipeline's main resolver consumes as another candidate source.
  Column is used verbatim when present; when None, we pick the rightmost
  identifier on the line (typically the method name of `foo.bar()`),
  which is vastly cheaper and more accurate than the prior 20-column scan.
- `resolve_unresolved_calls(config, conn)` — compatibility wrapper that
  iterates `unresolved_calls`, calls the new API, inserts relations for
  resolved candidates, and returns a stats dict. Used by the legacy test
  surface and any caller that wants a one-shot retrofit pass.

Gate via `config.enable_jedi` (default False). When `enable_jedi=True`
but Jedi isn't installed the functions return a noop stats dict.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def is_available() -> bool:
    try:
        import jedi  # noqa: F401
    except Exception:
        return False
    return True


def _ensure_jedi():
    import jedi

    return jedi


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pick_column(line_text: str) -> int:
    """Pick a single column for Jedi goto when the parser didn't supply one.

    Strategy: for a call like `foo.method(args)`, the method token is what
    we want goto to resolve — that's the rightmost identifier before the
    first `(`. Fall back to position 0 if no identifier is found. This is
    deterministic and O(len(line)) — no scanning of 20 columns.
    """
    if not line_text:
        return 0
    # Truncate at the first `(` — everything past it is argument expressions.
    head = line_text.split("(", 1)[0]
    # Walk backwards to find the start of the trailing identifier.
    i = len(head)
    while i > 0 and (head[i - 1].isalnum() or head[i - 1] == "_"):
        i -= 1
    # If we found an identifier, that's our column.
    if i < len(head) and (head[i].isalpha() or head[i] == "_"):
        return i
    # Otherwise, first non-whitespace char of the full line.
    for idx, ch in enumerate(line_text):
        if not ch.isspace():
            return idx
    return 0


def _jedi_goto_candidates(
    project, source: str, path: Path, line: int, column: int | None
) -> list[str]:
    """Return ordered canonical-name candidates for a call-site."""
    jedi = _ensure_jedi()
    try:
        script = jedi.Script(code=source, path=str(path), project=project)
    except Exception:
        return []

    lines = source.splitlines()
    if line < 1 or line > len(lines):
        return []
    line_text = lines[line - 1]
    col = column if column is not None else _pick_column(line_text)

    try:
        defs = script.goto(line, col, follow_imports=True)
    except Exception:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for d in defs:
        module_name = d.module_name or ""
        if module_name in ("builtins", ""):
            continue
        if hasattr(d, "full_name") and d.full_name:
            candidate = d.full_name
        elif module_name and d.name:
            candidate = f"{module_name}.{d.name}"
        else:
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    # Longest dotted path first — the leaf attribute is what's actually
    # invoked for `foo.bar()`.
    out.sort(key=lambda c: -c.count("."))
    return out


def resolve_pending_via_jedi(
    config,
    conn: sqlite3.Connection,
    records: list[dict],
) -> dict[tuple[str, int, int], list[str]]:
    """Resolve a batch of pending call-sites via Jedi.

    Parameters
    ----------
    records : list of dicts with keys
        - ``src_symbol_uid`` (str)
        - ``file_pk`` (int)
        - ``line`` (int)
        - ``column`` (int | None)

    Returns
    -------
    dict
        Mapping from ``(src_symbol_uid, file_pk, line)`` to an ordered list
        of candidate canonical names. Entries with no Jedi candidates are
        omitted from the mapping. Callers merge these candidates with their
        own before running the main resolver.

    The function is bounded by ``len(records)`` — there is no fixed cap.
    Callers that want to cap work should slice the list before calling.
    """
    if not getattr(config, "enable_jedi", False):
        return {}
    if not is_available():
        return {}
    if not records:
        return {}

    jedi = _ensure_jedi()
    project = jedi.Project(str(config.root))

    # Cache file paths + source by file_pk.
    source_cache: dict[int, tuple[Path, str] | None] = {}
    out: dict[tuple[str, int, int], list[str]] = {}

    for rec in records:
        src_uid = rec.get("src_symbol_uid")
        file_pk = rec.get("file_pk")
        line = rec.get("line")
        column = rec.get("column")
        if src_uid is None or file_pk is None or line is None:
            continue
        file_pk = int(file_pk)
        line = int(line)

        if file_pk not in source_cache:
            file_row = conn.execute(
                "SELECT file_path FROM files WHERE file_pk = ?",
                (file_pk,),
            ).fetchone()
            if file_row is None:
                source_cache[file_pk] = None
            else:
                abs_path = config.root / file_row["file_path"]
                try:
                    source_cache[file_pk] = (
                        abs_path,
                        abs_path.read_text(encoding="utf-8", errors="replace"),
                    )
                except OSError:
                    source_cache[file_pk] = None
        entry = source_cache[file_pk]
        if entry is None:
            continue
        abs_path, source = entry

        try:
            cands = _jedi_goto_candidates(project, source, abs_path, line, column)
        except Exception:
            continue
        if cands:
            out[(src_uid, file_pk, line)] = cands

    return out


def resolve_unresolved_calls(config, conn: sqlite3.Connection) -> dict[str, Any]:
    """Legacy one-shot pass over `unresolved_calls`.

    Kept so callers that still invoke Jedi as a retrofit (e.g. migrations
    from older slices, or opt-in commands) keep a working API. Behaviour:
    iterate every open `unresolved_calls` row for kind='calls', ask Jedi
    to resolve via `resolve_pending_via_jedi`, land any resolved relations
    with provenance ``jedi:goto``, and return a stats dict.

    The scoped `test_edges` rebuild is the caller's responsibility — this
    function only writes relations. The pipeline path uses the dedicated
    `resolve_pending_via_jedi` from inside `_resolve_pending` and then
    relies on reindex()'s normal scoped rebuild.
    """
    enabled = bool(getattr(config, "enable_jedi", False))
    available = is_available()
    base = {
        "available": available,
        "enabled": enabled,
        "attempted": 0,
        "resolved_by_jedi": 0,
        "still_unresolved": 0,
        "jedi_errors": 0,
    }
    if not available:
        base["error"] = "jedi not installed (pip install 'code-index[jedi]')"
        return base
    if not enabled:
        base["note"] = "set config.enable_jedi=True to run this resolver"
        return base

    rows = conn.execute(
        """
        SELECT unresolved_pk, file_pk, src_symbol_uid, relation_kind,
               site_line, provenance
          FROM unresolved_calls
         WHERE resolved_at IS NULL
           AND relation_kind = 'calls'
        """
    ).fetchall()

    records = []
    row_index: dict[tuple[str, int, int], list] = {}
    for row in rows:
        if row["site_line"] is None or row["file_pk"] is None:
            continue
        key = (row["src_symbol_uid"], int(row["file_pk"]), int(row["site_line"]))
        records.append(
            {
                "src_symbol_uid": row["src_symbol_uid"],
                "file_pk": int(row["file_pk"]),
                "line": int(row["site_line"]),
                "column": None,
            }
        )
        row_index.setdefault(key, []).append(row)

    base["attempted"] = len(records)

    try:
        candidates_map = resolve_pending_via_jedi(config, conn, records)
    except Exception:
        base["jedi_errors"] = len(records)
        candidates_map = {}

    resolved = 0
    now = _now_iso()
    for key, candidates in candidates_map.items():
        for row in row_index.get(key, []):
            dst_pk = _match_candidate(conn, candidates)
            if dst_pk is None:
                continue
            src_row = conn.execute(
                "SELECT symbol_pk FROM symbols WHERE symbol_uid = ? AND deleted_at IS NULL",
                (row["src_symbol_uid"],),
            ).fetchone()
            if src_row is None:
                continue
            src_pk = int(src_row["symbol_pk"])
            if src_pk == dst_pk:
                continue
            provenance = f"{row['provenance'] or ''};jedi:goto".lstrip(";")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO relations(
                    src_symbol_pk, dst_symbol_pk, relation_kind, provenance, weight
                ) VALUES (?, ?, 'calls', ?, 1.0)
                """,
                (src_pk, dst_pk, provenance),
            )
            if cur.rowcount:
                resolved += 1
                conn.execute(
                    "UPDATE unresolved_calls SET resolved_at = ? WHERE unresolved_pk = ?",
                    (now, row["unresolved_pk"]),
                )

    still = conn.execute(
        "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
    ).fetchone()[0]
    base["resolved_by_jedi"] = resolved
    base["still_unresolved"] = int(still)
    return base


def _match_candidate(conn: sqlite3.Connection, candidates: list[str]) -> int | None:
    """Match Jedi candidates against live symbols by canonical_name only.

    Deliberately stricter than the pipeline resolver's suffix-match:
    Jedi already gave us a fully-qualified name, so accepting suffix
    matches here would re-introduce the wrong-edge risk we're trying
    to close.
    """
    for cand in candidates:
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE canonical_name = ? AND deleted_at IS NULL LIMIT 1",
            (cand,),
        ).fetchone()
        if row:
            return int(row["symbol_pk"])
    return None
