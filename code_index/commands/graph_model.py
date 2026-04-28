"""Graph payload builder for `code_index graph`."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index.commands.graph_notes import graph_notes_block


RELATION_KINDS = ("calls", "imports", "inherits", "implements", "overrides")

CARE_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

CARE_GUIDANCE = {
    "critical": (
        "Very low freedom: core indexing infrastructure. Prefer narrow edits, "
        "read connected files first, and run targeted tests."
    ),
    "high": (
        "Low freedom: central or shared behavior. Make scoped changes and "
        "verify nearby commands or tests."
    ),
    "medium": (
        "Moderate freedom: normal product code. Follow local patterns and "
        "check direct dependents."
    ),
    "low": (
        "Higher freedom: docs, tests, fixtures, or leaf files. Keep intent "
        "clear and avoid unrelated churn."
    ),
}

TYPE_COLORS = {
    "python": "#3972b8",
    "sql": "#8b5a2b",
    "markdown": "#61707d",
    "toml": "#7b5ab6",
    "json": "#8a6f28",
    "text": "#6b7280",
    "directory": "#3e5872",
}

ROLE_LABELS = {
    "schema": "database schema",
    "storage": "sqlite/storage",
    "pipeline": "index pipeline",
    "identity": "identity model",
    "locking": "writer locking",
    "config": "configuration",
    "cli": "CLI entrypoint",
    "command": "CLI command",
    "mcp": "agent interface",
    "parser": "parser",
    "search": "search",
    "structural": "structural search",
    "embedding": "embedding projection",
    "runner": "test runner",
    "test": "test",
    "docs": "documentation",
    "benchmark": "benchmark",
    "package": "package metadata",
    "support": "support code",
    "directory": "directory",
}

CRITICAL_ROLES = {"schema", "storage", "pipeline", "identity", "locking", "config"}
HIGH_ROLES = {"cli", "mcp", "parser", "search", "structural", "command"}
LOW_ROLES = {"docs", "test", "benchmark"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _node_id(kind: str, path: str) -> str:
    return f"{kind}:{path}"


def _normal_path(path: str) -> str:
    out = path.replace("\\", "/").strip()
    while out.startswith("./"):
        out = out[2:]
    return out


def _dir_path(path: str) -> str:
    parts = _normal_path(path).split("/")[:-1]
    return "/".join(parts) if parts else "."


def _parent_dir(path: str) -> str | None:
    if path == ".":
        return None
    parts = path.split("/")
    if len(parts) <= 1:
        return "."
    return "/".join(parts[:-1])


def _dir_chain_for_file(path: str) -> list[str]:
    parts = _normal_path(path).split("/")[:-1]
    chain = ["."]
    cur: list[str] = []
    for part in parts:
        cur.append(part)
        chain.append("/".join(cur))
    return chain


def _file_type(path: str, language: str | None) -> str:
    if language:
        return language
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".sql":
        return "sql"
    if suffix == ".md":
        return "markdown"
    if suffix == ".toml":
        return "toml"
    if suffix == ".json":
        return "json"
    return "text"


def _role_for_file(path: str, language: str | None) -> str:
    p = _normal_path(path).lower()
    name = p.rsplit("/", 1)[-1]
    if p == "code_index/schema.sql" or name == "schema.sql":
        return "schema"
    if p == "code_index/db.py":
        return "storage"
    if p == "code_index/pipeline.py":
        return "pipeline"
    if p == "code_index/symbols.py":
        return "identity"
    if p == "code_index/locking.py":
        return "locking"
    if p == "code_index/config.py":
        return "config"
    if p == "code_index/cli.py":
        return "cli"
    if p.startswith("code_index/commands/mcp") or name == "mcp_serve_cmd.py":
        return "mcp"
    if p.startswith("code_index/commands/"):
        return "command"
    if p.startswith("code_index/parsers/"):
        return "parser"
    if p.startswith("code_index/search/"):
        return "search"
    if p.startswith("code_index/structural/"):
        return "structural"
    if p.startswith("code_index/embeddings/"):
        return "embedding"
    if p.startswith("code_index/runners/"):
        return "runner"
    if p.startswith("tests/") or name.startswith("test_"):
        return "test"
    if p.startswith("docs/") or p.startswith("plans/") or name in {
        "readme.md",
        "claude.md",
    }:
        return "docs"
    if p.startswith("bench/") or p.startswith("benchmarks/"):
        return "benchmark"
    if name in {"pyproject.toml", "package.json", "requirements.txt"}:
        return "package"
    if language == "python" and p.startswith("code_index/"):
        return "support"
    return "support"


def _read_code(
    root: Path, rel_path: str, *, include_code: bool, max_code_bytes: int
) -> tuple[int | None, dict[str, Any]]:
    path = root / rel_path
    if not path.is_file():
        return None, {
            "included": False,
            "content": "",
            "reason": "file is not present on disk",
        }
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, {"included": False, "content": "", "reason": str(exc)}
    if size > max_code_bytes:
        return None, {
            "included": False,
            "content": "",
            "reason": f"file is larger than max_code_bytes ({max_code_bytes})",
        }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, {"included": False, "content": "", "reason": str(exc)}
    line_count = len(text.splitlines()) or 1
    if not include_code:
        return line_count, {
            "included": False,
            "content": "",
            "reason": "code embedding disabled",
        }
    return line_count, {"included": True, "content": text, "reason": None}


def _collect_file_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.file_pk,
               f.file_path,
               f.language,
               f.size_bytes,
               f.parse_status,
               f.parse_error,
               f.semantic_source,
               f.parser_confidence,
               f.git_committed_at,
               f.git_author,
               (
                   SELECT COUNT(DISTINCT s.symbol_pk)
                     FROM occurrences o
                     JOIN symbols s ON s.symbol_pk = o.symbol_pk
                    WHERE o.file_pk = f.file_pk
                      AND o.role = 'definition'
                      AND s.deleted_at IS NULL
               ) AS symbol_count,
               (
                   SELECT COUNT(*)
                     FROM chunks c
                    WHERE c.file_pk = f.file_pk
                      AND c.deleted_at IS NULL
               ) AS chunk_count,
               (
                   SELECT COALESCE(SUM(c.edit_count), 0)
                     FROM chunks c
                    WHERE c.file_pk = f.file_pk
                      AND c.deleted_at IS NULL
               ) AS edit_count,
               (
                   SELECT COUNT(*)
                     FROM diagnostics d
                    WHERE d.file_pk = f.file_pk
               ) AS diagnostic_count,
               (
                   SELECT COUNT(*)
                     FROM test_edges te
                     JOIN occurrences o
                       ON o.symbol_pk = te.target_symbol_pk
                      AND o.role = 'definition'
                    WHERE o.file_pk = f.file_pk
               ) AS test_count
          FROM files f
         WHERE f.deleted_at IS NULL
         ORDER BY f.file_path
        """
    ).fetchall()


def _collect_symbols_by_file(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT f.file_path,
               s.canonical_name,
               s.display_name,
               s.kind,
               o.start_line
          FROM occurrences o
          JOIN files f ON f.file_pk = o.file_pk
          JOIN symbols s ON s.symbol_pk = o.symbol_pk
         WHERE f.deleted_at IS NULL
           AND s.deleted_at IS NULL
           AND o.role = 'definition'
         ORDER BY f.file_path, o.start_line, s.canonical_name
        """
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        items = out[row["file_path"]]
        if len(items) >= 16:
            continue
        items.append(
            {
                "canonical_name": row["canonical_name"],
                "display_name": row["display_name"],
                "kind": row["kind"],
                "line": row["start_line"],
            }
        )
    return out


def _collect_context_by_file(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT file_path, context_json
          FROM chunks
         WHERE deleted_at IS NULL
           AND chunk_type IN ('module', 'file')
         ORDER BY file_path, start_line
        """
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = row["file_path"]
        if path in out:
            continue
        raw = row["context_json"] or "{}"
        try:
            out[path] = json.loads(raw)
        except json.JSONDecodeError:
            out[path] = {}
    return out


def _collect_recent_edits(
    conn: sqlite3.Connection, *, limit: int = 250
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = conn.execute(
        """
        SELECT c.file_path,
               c.symbol_path,
               c.chunk_type,
               ce.chunk_uid,
               ce.timestamp,
               ce.event_source,
               ce.change_type,
               ce.changed_lines,
               ce.diff_summary
          FROM chunk_edits ce
          LEFT JOIN chunks c ON c.chunk_pk = ce.chunk_pk
         WHERE c.file_path IS NOT NULL
         ORDER BY ce.timestamp DESC, ce.edit_pk DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    edits: list[dict[str, Any]] = []
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = {
            "file_path": row["file_path"],
            "symbol_path": row["symbol_path"],
            "chunk_type": row["chunk_type"],
            "chunk_uid": row["chunk_uid"],
            "timestamp": row["timestamp"],
            "event_source": row["event_source"],
            "change_type": row["change_type"],
            "changed_lines": row["changed_lines"],
            "diff_summary": row["diff_summary"],
        }
        edits.append(item)
        if len(by_file[row["file_path"]]) < 12:
            by_file[row["file_path"]].append(item)
    return edits, by_file


def _agent_events_as_edits(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    for event in events:
        path = event.get("file_path")
        if not path:
            continue
        edits.append(
            {
                "file_path": path,
                "symbol_path": event.get("symbol_path"),
                "chunk_type": "agent-event",
                "chunk_uid": None,
                "timestamp": event.get("timestamp"),
                "event_source": f"agent:{event.get('agent_name') or 'Agent'}",
                "change_type": event.get("event_type") or "activity",
                "changed_lines": (event.get("payload") or {}).get("changed_lines"),
                "diff_summary": event.get("message") or "Agent activity event.",
                "run_id": event.get("run_id"),
                "agent_name": event.get("agent_name") or "Agent",
            }
        )
    return edits


def _sort_recent_edits(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        edits,
        key=lambda edit: (
            str(edit.get("timestamp") or ""),
            1 if str(edit.get("event_source") or "").startswith("agent:") else 0,
        ),
        reverse=True,
    )


def _group_edits_by_file(
    edits: list[dict[str, Any]], *, per_file_limit: int = 12
) -> dict[str, list[dict[str, Any]]]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edit in edits:
        path = edit.get("file_path")
        if not path:
            continue
        if len(by_file[path]) < per_file_limit:
            by_file[path].append(edit)
    return by_file


def _recent_files_from_edits(
    recent_edits: list[dict[str, Any]], *, limit: int = 8
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for edit in recent_edits:
        path = edit.get("file_path")
        if not path:
            continue
        if path not in grouped:
            grouped[path] = {
                "file_path": path,
                "last_edited_at": edit.get("timestamp"),
                "event_source": edit.get("event_source"),
                "edit_count": 0,
                "change_types": Counter(),
                "symbols": [],
            }
            order.append(path)
        item = grouped[path]
        item["edit_count"] += 1
        if edit.get("change_type"):
            item["change_types"][edit["change_type"]] += 1
        symbol = edit.get("symbol_path")
        if symbol and symbol not in item["symbols"] and len(item["symbols"]) < 6:
            item["symbols"].append(symbol)
    out: list[dict[str, Any]] = []
    for rank, path in enumerate(order[:limit], start=1):
        item = dict(grouped[path])
        item["rank"] = rank
        item["change_types"] = dict(sorted(item["change_types"].items()))
        out.append(item)
    return out


def _collect_relation_edges(conn: sqlite3.Connection) -> dict[tuple[str, str], Counter]:
    rows = conn.execute(
        f"""
        SELECT sf.file_path AS src_file,
               df.file_path AS dst_file,
               r.relation_kind,
               COUNT(*) AS weight
          FROM relations r
          JOIN symbols ss ON ss.symbol_pk = r.src_symbol_pk
          JOIN symbols ds ON ds.symbol_pk = r.dst_symbol_pk
          JOIN occurrences so
            ON so.symbol_pk = r.src_symbol_pk
           AND so.role = 'definition'
          JOIN occurrences do
            ON do.symbol_pk = r.dst_symbol_pk
           AND do.role = 'definition'
          JOIN files sf ON sf.file_pk = so.file_pk
          JOIN files df ON df.file_pk = do.file_pk
         WHERE ss.deleted_at IS NULL
           AND ds.deleted_at IS NULL
           AND sf.deleted_at IS NULL
           AND df.deleted_at IS NULL
           AND r.relation_kind IN ({",".join("?" for _ in RELATION_KINDS)})
         GROUP BY sf.file_path, df.file_path, r.relation_kind
        """,
        RELATION_KINDS,
    ).fetchall()
    pairs: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for row in rows:
        src = row["src_file"]
        dst = row["dst_file"]
        if not src or not dst or src == dst:
            continue
        pairs[(src, dst)][row["relation_kind"]] += int(row["weight"] or 0)
    return pairs


def _import_names(context: dict[str, Any]) -> list[str]:
    imports = context.get("imports") or []
    names: list[str] = []
    if not isinstance(imports, list):
        return names
    for item in imports:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "import":
            mod = item.get("module")
            if mod:
                names.append(str(mod))
        elif item.get("kind") == "import_from":
            mod = item.get("module")
            name = item.get("name")
            if mod and name:
                names.append(f"{mod}.{name}")
            elif mod:
                names.append(str(mod))
    return names


def _importance_score(
    *,
    role: str,
    incoming: int,
    outgoing: int,
    symbol_count: int,
    edit_count: int,
    test_count: int,
    diagnostic_count: int,
) -> float:
    role_boost = {
        "schema": 14,
        "storage": 14,
        "pipeline": 14,
        "identity": 12,
        "locking": 11,
        "config": 11,
        "cli": 9,
        "mcp": 9,
        "parser": 8,
        "search": 7,
        "structural": 7,
        "command": 5,
        "embedding": 4,
        "runner": 4,
        "package": 4,
        "support": 3,
        "test": 1,
        "docs": 0,
        "benchmark": 0,
    }.get(role, 1)
    # Raw relation counts can get large in a well-connected repo. Use
    # logarithmic weighting so centrality matters without making half the
    # graph look equally untouchable.
    return round(
        role_boost
        + math.log1p(incoming) * 5.0
        + math.log1p(outgoing) * 2.5
        + math.sqrt(max(symbol_count, 0)) * 1.2
        + math.log1p(edit_count) * 2.0
        + math.log1p(test_count) * 3.0
        + min(diagnostic_count, 5) * 0.7,
        2,
    )


def _care_level(role: str, score: float) -> str:
    if role in CRITICAL_ROLES or score >= 34:
        return "critical"
    if role in HIGH_ROLES or score >= 23:
        return "high"
    if role in LOW_ROLES and score < 10:
        return "low"
    if score >= 11:
        return "medium"
    return "low"


def _care_reasons(
    *,
    role: str,
    incoming: int,
    outgoing: int,
    symbol_count: int,
    edit_count: int,
    test_count: int,
    diagnostic_count: int,
) -> list[str]:
    reasons: list[str] = []
    label = ROLE_LABELS.get(role, role)
    if role in CRITICAL_ROLES:
        reasons.append(f"{label} is critical infrastructure")
    elif role in HIGH_ROLES:
        reasons.append(f"{label} is shared infrastructure")
    elif role in LOW_ROLES:
        reasons.append(f"{label} usually has lower blast radius")
    if incoming:
        reasons.append(f"{incoming} inbound cross-file relation(s)")
    if outgoing:
        reasons.append(f"{outgoing} outbound cross-file relation(s)")
    if symbol_count >= 8:
        reasons.append(f"{symbol_count} defined symbol(s)")
    if edit_count:
        reasons.append(f"{edit_count} recorded chunk edit(s)")
    if test_count:
        reasons.append(f"{test_count} affected-test edge(s)")
    if diagnostic_count:
        reasons.append(f"{diagnostic_count} parser diagnostic(s)")
    if not reasons:
        reasons.append("leaf or lightly connected file")
    return reasons


def _file_summary(
    *,
    path: str,
    role: str,
    language: str,
    semantic_source: str | None,
    symbols: list[dict[str, Any]],
    import_names: list[str],
    incoming: int,
    outgoing: int,
    care: str,
    reasons: list[str],
) -> str:
    role_label = ROLE_LABELS.get(role, role)
    parser = semantic_source or "unknown parser"
    pieces = [
        f"{path} is a {role_label} file indexed as {language} by {parser}.",
    ]
    if symbols:
        sample = ", ".join(s["canonical_name"] for s in symbols[:5])
        more = "" if len(symbols) <= 5 else f", plus {len(symbols) - 5} more"
        pieces.append(f"It defines {sample}{more}.")
    else:
        pieces.append("It has no structured symbols in the current index.")
    if import_names:
        sample_imports = ", ".join(import_names[:6])
        more_imports = (
            "" if len(import_names) <= 6 else f", plus {len(import_names) - 6} more"
        )
        pieces.append(f"Imports include {sample_imports}{more_imports}.")
    if incoming or outgoing:
        pieces.append(
            f"Graph connectivity: {incoming} inbound and {outgoing} outbound cross-file relation(s)."
        )
    pieces.append(f"Care level is {care}: {reasons[0]}.")
    return " ".join(pieces)


def _edge_kind(counter: Counter) -> str:
    if not counter:
        return "related"
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[0][0]


def build_graph(
    conn: sqlite3.Connection,
    root: Path,
    *,
    include_code: bool = True,
    max_code_bytes: int = 200_000,
    focus_paths: list[str] | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable graph payload from the current index."""
    focus = {_normal_path(p) for p in (focus_paths or []) if p}
    file_rows = _collect_file_rows(conn)
    symbols_by_file = _collect_symbols_by_file(conn)
    context_by_file = _collect_context_by_file(conn)
    index_recent_edits, _index_recent_edits_by_file = _collect_recent_edits(conn)
    activity_snapshot = agent_activity.activity_snapshot(
        conn, event_limit=120, file_limit=8
    )
    agent_recent_events = activity_snapshot["recent_events"]
    active_claims = activity_snapshot.get("active_claims", [])
    agent_recent_edits = _agent_events_as_edits(agent_recent_events)
    recent_edits = _sort_recent_edits(index_recent_edits + agent_recent_edits)
    recent_edits_by_file = _group_edits_by_file(recent_edits)
    recent_files = _recent_files_from_edits(recent_edits)
    recent_file_by_path = {item["file_path"]: item for item in recent_files}
    relation_pairs = _collect_relation_edges(conn)
    active_files_from_runs: list[str] = []
    for run in activity_snapshot["active_runs"]:
        for path in run.get("active_files", []):
            if path and path not in active_files_from_runs:
                active_files_from_runs.append(path)
    active_files_from_claims: list[str] = []
    for claim in active_claims:
        path = claim.get("file_path")
        if path and path not in active_files_from_claims:
            active_files_from_claims.append(path)
    active_paths = set(focus) | set(active_files_from_runs) | set(active_files_from_claims)

    incoming_by_file: dict[str, int] = defaultdict(int)
    outgoing_by_file: dict[str, int] = defaultdict(int)
    relation_neighbors_in: dict[str, set[str]] = defaultdict(set)
    relation_neighbors_out: dict[str, set[str]] = defaultdict(set)
    for (src, dst), kinds in relation_pairs.items():
        total = sum(kinds.values())
        outgoing_by_file[src] += total
        incoming_by_file[dst] += total
        relation_neighbors_out[src].add(dst)
        relation_neighbors_in[dst].add(src)

    nodes: list[dict[str, Any]] = []
    file_nodes_by_path: dict[str, dict[str, Any]] = {}

    for row in file_rows:
        path = row["file_path"]
        language = _file_type(path, row["language"])
        role = _role_for_file(path, row["language"])
        incoming = int(incoming_by_file[path])
        outgoing = int(outgoing_by_file[path])
        symbol_count = int(row["symbol_count"] or 0)
        chunk_count = int(row["chunk_count"] or 0)
        edit_count = int(row["edit_count"] or 0)
        test_count = int(row["test_count"] or 0)
        diagnostic_count = int(row["diagnostic_count"] or 0)
        score = _importance_score(
            role=role,
            incoming=incoming,
            outgoing=outgoing,
            symbol_count=symbol_count,
            edit_count=edit_count,
            test_count=test_count,
            diagnostic_count=diagnostic_count,
        )
        care = _care_level(role, score)
        reasons = _care_reasons(
            role=role,
            incoming=incoming,
            outgoing=outgoing,
            symbol_count=symbol_count,
            edit_count=edit_count,
            test_count=test_count,
            diagnostic_count=diagnostic_count,
        )
        line_count, code = _read_code(
            root,
            path,
            include_code=include_code,
            max_code_bytes=max_code_bytes,
        )
        context = context_by_file.get(path, {})
        imports = _import_names(context)
        symbols = symbols_by_file.get(path, [])
        recent_file = recent_file_by_path.get(path)
        summary = _file_summary(
            path=path,
            role=role,
            language=language,
            semantic_source=row["semantic_source"],
            symbols=symbols,
            import_names=imports,
            incoming=incoming,
            outgoing=outgoing,
            care=care,
            reasons=reasons,
        )
        node = {
            "id": _node_id("file", path),
            "kind": "file",
            "path": path,
            "label": path.rsplit("/", 1)[-1],
            "directory": _dir_path(path),
            "language": language,
            "file_type": language,
            "role": role,
            "role_label": ROLE_LABELS.get(role, role),
            "care_level": care,
            "freedom": CARE_GUIDANCE[care],
            "active_work": path in active_paths,
            "importance": {"score": score, "rank": None, "reasons": reasons},
            "metrics": {
                "size_bytes": int(row["size_bytes"] or 0),
                "line_count": line_count,
                "symbol_count": symbol_count,
                "chunk_count": chunk_count,
                "edit_count": edit_count,
                "test_count": test_count,
                "diagnostic_count": diagnostic_count,
                "incoming_relations": incoming,
                "outgoing_relations": outgoing,
                "incoming_files": sorted(relation_neighbors_in.get(path, set())),
                "outgoing_files": sorted(relation_neighbors_out.get(path, set())),
                "recent_edit_rank": recent_file["rank"] if recent_file else None,
                "last_edited_at": recent_file["last_edited_at"] if recent_file else None,
                "recent_edit_count": recent_file["edit_count"] if recent_file else 0,
            },
            "index": {
                "parse_status": row["parse_status"],
                "parse_error": row["parse_error"],
                "semantic_source": row["semantic_source"],
                "parser_confidence": row["parser_confidence"],
                "git_committed_at": row["git_committed_at"],
                "git_author": row["git_author"],
            },
            "symbols": symbols,
            "imports": imports,
            "recent_edits": recent_edits_by_file.get(path, []),
            "recent_activity": recent_file,
            "summary": summary,
            "code": code,
        }
        nodes.append(node)
        file_nodes_by_path[path] = node

    ranked_files = sorted(
        file_nodes_by_path.values(),
        key=lambda n: (-float(n["importance"]["score"]), n["path"]),
    )
    for idx, node in enumerate(ranked_files, start=1):
        node["importance"]["rank"] = idx

    dir_stats: dict[str, dict[str, Any]] = {}
    for file_node in file_nodes_by_path.values():
        for directory in _dir_chain_for_file(file_node["path"]):
            stats = dir_stats.setdefault(
                directory,
                {
                    "file_count": 0,
                    "max_score": 0.0,
                    "care_counts": Counter(),
                    "languages": Counter(),
                    "roles": Counter(),
                    "active_files": [],
                    "recent_files": [],
                },
            )
            stats["file_count"] += 1
            stats["max_score"] = max(
                float(stats["max_score"]), float(file_node["importance"]["score"])
            )
            stats["care_counts"][file_node["care_level"]] += 1
            stats["languages"][file_node["language"]] += 1
            stats["roles"][file_node["role"]] += 1
            if file_node["active_work"]:
                stats["active_files"].append(file_node["path"])
            if file_node["metrics"].get("recent_edit_rank"):
                stats["recent_files"].append(file_node["path"])

    dir_nodes: list[dict[str, Any]] = []
    for directory, stats in sorted(dir_stats.items(), key=lambda item: item[0]):
        care = max(stats["care_counts"], key=lambda c: CARE_ORDER.get(c, 0))
        label = "repo" if directory == "." else directory.rsplit("/", 1)[-1]
        role_counts = dict(sorted(stats["roles"].items()))
        lang_counts = dict(sorted(stats["languages"].items()))
        dir_nodes.append(
            {
                "id": _node_id("dir", directory),
                "kind": "directory",
                "path": directory,
                "label": label,
                "directory": _parent_dir(directory),
                "language": "directory",
                "file_type": "directory",
                "role": "directory",
                "role_label": ROLE_LABELS["directory"],
                "care_level": care,
                "freedom": CARE_GUIDANCE[care],
                "active_work": bool(stats["active_files"]),
                "importance": {
                    "score": round(float(stats["max_score"]), 2),
                    "rank": None,
                    "reasons": [f"contains {stats['file_count']} indexed file(s)"],
                },
                "metrics": {
                    "file_count": stats["file_count"],
                    "care_counts": dict(stats["care_counts"]),
                    "languages": lang_counts,
                    "roles": role_counts,
                    "active_files": stats["active_files"][:8],
                    "recent_files": stats["recent_files"][:8],
                    "recent_edit_rank": 1 if stats["recent_files"] else None,
                },
                "symbols": [],
                "imports": [],
                "recent_edits": [
                    edit
                    for edit in recent_edits
                    if directory == "."
                    or (edit.get("file_path") or "").startswith(f"{directory}/")
                ][:12],
                "recent_activity": {
                    "file_path": directory,
                    "edit_count": len(stats["recent_files"]),
                    "last_edited_at": None,
                }
                if stats["recent_files"]
                else None,
                "summary": (
                    f"{directory} contains {stats['file_count']} indexed file(s). "
                    f"Highest contained care level is {care}."
                ),
                "code": {
                    "included": False,
                    "content": "",
                    "reason": "directory nodes do not embed code",
                },
            }
        )

    edges: list[dict[str, Any]] = []
    edge_id = 0
    for directory in sorted(dir_stats):
        parent = _parent_dir(directory)
        if parent is None:
            continue
        edge_id += 1
        edges.append(
            {
                "id": f"edge:{edge_id}",
                "source": _node_id("dir", parent),
                "target": _node_id("dir", directory),
                "kind": "contains",
                "weight": 1,
                "label": "contains",
                "detail": {"contains": 1},
            }
        )
    for path, node in sorted(file_nodes_by_path.items()):
        edge_id += 1
        edges.append(
            {
                "id": f"edge:{edge_id}",
                "source": _node_id("dir", node["directory"]),
                "target": node["id"],
                "kind": "contains",
                "weight": 1,
                "label": "contains",
                "detail": {"contains": 1},
            }
        )
    for (src, dst), kinds in sorted(relation_pairs.items()):
        if src not in file_nodes_by_path or dst not in file_nodes_by_path:
            continue
        total = sum(kinds.values())
        edge_id += 1
        top_kind = _edge_kind(kinds)
        edges.append(
            {
                "id": f"edge:{edge_id}",
                "source": _node_id("file", src),
                "target": _node_id("file", dst),
                "kind": top_kind,
                "weight": total,
                "label": "+".join(sorted(kinds)),
                "detail": dict(sorted(kinds.items())),
            }
        )

    all_nodes = dir_nodes + nodes
    care_counts = Counter(n["care_level"] for n in nodes)
    language_counts = Counter(n["language"] for n in nodes)
    role_counts = Counter(n["role"] for n in nodes)
    relation_counts = Counter()
    for edge in edges:
        if edge["kind"] != "contains":
            relation_counts[edge["kind"]] += int(edge["weight"])
    active_file_list: list[str] = []
    for path in sorted(focus):
        if path not in active_file_list:
            active_file_list.append(path)
    for path in active_files_from_runs:
        if path not in active_file_list:
            active_file_list.append(path)
    for path in active_files_from_claims:
        if path not in active_file_list:
            active_file_list.append(path)
    active_agent_names = sorted(
        {
            run.get("agent_name") or "Agent"
            for run in activity_snapshot["active_runs"]
        }
    )
    agent_status = (
        "working"
        if active_file_list or activity_snapshot["active_runs"]
        else "idle"
    )

    return {
        "kind": "code_index_graph",
        "schema_version": 1,
        "root": str(root),
        "generated_at": _now_iso(),
        "focus_paths": sorted(focus),
        "live": {
            "server": False,
            "events_path": None,
            "notes_path": None,
            "search_path": None,
            "agent_preflight_path": None,
            "agent_runs_path": None,
            "agent_events_path": None,
        },
        "agent": {
            "name": agent_name or (", ".join(active_agent_names) or "Agent"),
            "active_agents": active_agent_names,
            "active_files": active_file_list,
            "active_runs": activity_snapshot["active_runs"],
            "recent_runs": activity_snapshot.get("recent_runs", []),
            "active_claims": active_claims,
            "status": agent_status,
        },
        "summary": {
            "node_count": len(all_nodes),
            "file_count": len(nodes),
            "directory_count": len(dir_nodes),
            "edge_count": len(edges),
            "relation_edge_count": sum(1 for e in edges if e["kind"] != "contains"),
            "care_counts": dict(sorted(care_counts.items())),
            "language_counts": dict(sorted(language_counts.items())),
            "role_counts": dict(sorted(role_counts.items())),
            "relation_counts": dict(sorted(relation_counts.items())),
            "top_files": [
                {
                    "path": n["path"],
                    "score": n["importance"]["score"],
                    "care_level": n["care_level"],
                    "role": n["role"],
                }
                for n in ranked_files[:12]
            ],
            "recent_edits": recent_edits[:50],
            "recent_files": recent_files[:5],
        },
        "activity": {
            "recent_files": recent_files,
            "agent_recent_files": activity_snapshot["recent_files"],
            "agent_events": agent_recent_events[:80],
            "active_claims": active_claims,
            "trail": [
                {"from": recent_files[idx]["file_path"], "to": recent_files[idx + 1]["file_path"]}
                for idx in range(max(0, len(recent_files) - 1))
            ],
        },
        "notes": graph_notes_block(root),
        "legend": {
            "care_levels": CARE_GUIDANCE,
            "type_colors": TYPE_COLORS,
            "edge_kinds": {
                "contains": "directory/file organization",
                "imports": "source file imports symbols or modules from target file",
                "calls": "source file calls symbols defined in target file",
                "inherits": "source file inherits from symbols in target file",
                "implements": "source file implements symbols in target file",
                "overrides": "source file overrides symbols in target file",
            },
        },
        "nodes": all_nodes,
        "edges": edges,
        "limitations": [
            "Importance is a deterministic heuristic over indexed relations, symbols, tests, edits, and path role.",
            "External dependencies and dynamic runtime calls are not graph nodes.",
            "Unsupported files use heuristic file-level chunks, so their summaries are less precise.",
        ],
    }
