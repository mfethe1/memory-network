"""Tool and resource implementations for `code_index mcp-serve`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands.graph_cmd import build_graph
from code_index.commands.impact_cmd import _resolve_target, compute_impact
from code_index.locking import LockTimeoutError, writer_lock
from code_index.pipeline import reindex
from code_index.runners.pytest import build_pytest_invocation
from code_index.search import fts, lexical, symbol_search
from code_index.structural import ts_python




def _tool_search_text(
    config: cfg_mod.Config,
    pattern: str,
    path: str | None = None,
    max_count: int = 50,
    ignore_case: bool = False,
    fixed_strings: bool = False,
) -> dict:
    return lexical.grep(
        config,
        pattern=pattern,
        path_glob=path,
        max_count=max_count,
        case_insensitive=ignore_case,
        fixed_strings=fixed_strings,
    )


def _tool_search_query(
    config: cfg_mod.Config,
    query: str,
    limit: int = 20,
    language: str | None = None,
    chunk_type: str | None = None,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        results = fts.search(
            conn, query, limit=limit, language=language, chunk_type=chunk_type
        )
    finally:
        db_mod.close(conn)
    return {"engine": "fts5", "query": query, "results": results}


def _tool_search_ast(config: cfg_mod.Config, pattern: str, limit: int = 100) -> dict:
    if not ts_python.available():
        return {
            "error": "tree-sitter not available",
            "reason": ts_python._unavailable_reason(),
        }
    from code_index.ignore import build as build_matcher
    from code_index.scanner import iter_files

    matcher = build_matcher(
        config.root, extra=config.extra_ignore, include_hidden=config.include_hidden
    )
    files = [
        (s.path, s.rel_path)
        for s in iter_files(config.root, matcher, max_bytes=config.max_file_bytes)
        if s.rel_path.lower().endswith((".py", ".pyi"))
    ]
    try:
        result = ts_python.query_files(files, pattern)
    except Exception as exc:
        return {"error": "invalid tree-sitter query", "detail": repr(exc)}
    caps = [
        {
            "file": c.file_path,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "capture_name": c.capture_name,
            "node_kind": c.node_kind,
            "preview": c.text[:120].replace("\n", " "),
        }
        for c in result.captures[:limit]
    ]
    return {
        "engine": "tree-sitter",
        "query": result.query,
        "expanded_query": result.expanded_query,
        "total_captures": len(result.captures),
        "returned": len(caps),
        "results": caps,
    }


def _tool_find_symbol(
    config: cfg_mod.Config,
    name: str,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 50,
    references: bool = False,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        results = symbol_search.lookup(
            conn,
            name,
            kind=kind,
            language=language,
            limit=limit,
            include_references=references,
        )
    finally:
        db_mod.close(conn)
    return {"query": name, "results": results}


def _tool_impact(
    config: cfg_mod.Config,
    symbol: str,
    max_depth: int = 2,
    no_imports: bool = False,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        candidates = _resolve_target(conn, symbol)
        if not candidates:
            return {"error": "no matching symbol", "query": symbol}
        target_pk = int(candidates[0]["symbol_pk"])
        result = compute_impact(
            conn,
            target_pk,
            max_depth=max_depth,
            include_imports=not no_imports,
        )
        result["query"] = symbol
        return result
    finally:
        db_mod.close(conn)


def _tool_affected_tests(config: cfg_mod.Config, symbol: str) -> dict:
    from code_index.commands.tests_cmd import _affected, _resolve_input

    conn = db_mod.connect(config.db_path)
    try:
        candidates = _resolve_input(conn, symbol)
        if not candidates:
            return {"error": "no matching symbol", "query": symbol}
        target = candidates[0]
        affected = _affected(conn, int(target["symbol_pk"]))
    finally:
        db_mod.close(conn)
    return {
        "query": symbol,
        "target": {
            "symbol_uid": target["symbol_uid"],
            "canonical_name": target["canonical_name"],
            "kind": target["kind"],
        },
        "affected_tests": affected,
        "runner": build_pytest_invocation(affected),
    }


def _tool_doctor(config: cfg_mod.Config) -> dict:
    from code_index.commands.doctor_cmd import (
        _fts_consistency,
        _language_counts,
        _parse_status_counts,
        _relation_counts,
        _semantic_source_counts,
    )

    conn = db_mod.connect(config.db_path)
    try:
        return {
            "root": str(config.root),
            "schema_version": db_mod.get_schema_version(conn),
            "parse_status": _parse_status_counts(conn),
            "semantic_sources": _semantic_source_counts(conn),
            "languages": _language_counts(conn),
            "relations": _relation_counts(conn),
            "fts_consistency": _fts_consistency(conn),
            "test_edges": conn.execute("SELECT COUNT(*) FROM test_edges").fetchone()[0],
            "unresolved_calls_open": conn.execute(
                "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
            ).fetchone()[0],
        }
    finally:
        db_mod.close(conn)


def _tool_code_graph(
    config: cfg_mod.Config,
    include_code: bool = False,
    max_code_bytes: int = 200_000,
    focus_paths: list[str] | None = None,
    agent_name: str | None = None,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        return build_graph(
            conn,
            config.root,
            include_code=include_code,
            max_code_bytes=max(0, int(max_code_bytes)),
            focus_paths=focus_paths or [],
            agent_name=agent_name,
        )
    finally:
        db_mod.close(conn)


def _tool_agent_activity(config: cfg_mod.Config, limit: int = 100) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        return agent_activity.activity_snapshot(
            conn,
            event_limit=max(0, int(limit)),
            file_limit=8,
        )
    finally:
        db_mod.close(conn)


def _tool_agent_start(
    config: cfg_mod.Config,
    agent_name: str = "Agent",
    prompt: str = "",
    selected_nodes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict:
    with writer_lock(config):
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            run = agent_activity.start_run(
                conn,
                run_id=run_id,
                agent_name=agent_name,
                prompt=prompt,
                selected_nodes=selected_nodes or [],
                metadata=metadata or {},
            )
            return {"action": "start", "run": run}
        finally:
            db_mod.close(conn)


def _agent_run_for_event(
    conn: Any,
    *,
    run_id: str | None,
    agent_name: str,
) -> dict[str, Any]:
    if run_id:
        run = agent_activity.get_run(conn, run_id)
        if run is None:
            raise ValueError(f"unknown agent run_id: {run_id}")
        return run
    run = agent_activity.latest_active_run(conn, agent_name=agent_name)
    if run is not None:
        return run
    return agent_activity.start_run(
        conn,
        agent_name=agent_name,
        metadata={"implicit": True, "source": "mcp"},
    )


def _tool_agent_event(
    config: cfg_mod.Config,
    event_type: str,
    file_path: str | None = None,
    message: str | None = None,
    run_id: str | None = None,
    agent_name: str = "Agent",
    symbol_path: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict:
    with writer_lock(config):
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            run = _agent_run_for_event(
                conn,
                run_id=run_id,
                agent_name=agent_name,
            )
            event = agent_activity.record_event(
                conn,
                run_id=run["run_id"],
                event_type=event_type,
                file_path=file_path,
                symbol_path=symbol_path,
                message=message,
                payload=payload or {},
            )
            return {
                "action": "event",
                "run": agent_activity.get_run(conn, event["run_id"]),
                "event": event,
            }
        finally:
            db_mod.close(conn)


def _tool_agent_end(
    config: cfg_mod.Config,
    run_id: str | None = None,
    agent_name: str = "Agent",
    status: str = "completed",
) -> dict:
    with writer_lock(config):
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            run = _agent_run_for_event(conn, run_id=run_id, agent_name=agent_name)
            ended = agent_activity.end_run(
                conn,
                run_id=run["run_id"],
                status=status,
            )
            return {"action": "end", "run": ended}
        finally:
            db_mod.close(conn)


def _tool_update(
    config: cfg_mod.Config,
    files: list[str] | None = None,
    all: bool = False,
) -> dict:
    paths = [Path(p) for p in files] if files else (None if all else [])
    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                stats = reindex(conn, config, paths=paths, event_source="mcp")
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        return {
            "error": "another writer holds the lock",
            "lock_path": str(exc.lock_path),
            "timeout_s": exc.timeout_s,
        }
    return {"stats": stats.to_dict()}


def _tool_rebuild_fts(config: cfg_mod.Config) -> dict:
    from code_index.commands.rebuild_fts_cmd import _rebuild

    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                return _rebuild(conn)
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        return {
            "error": "another writer holds the lock",
            "lock_path": str(exc.lock_path),
            "timeout_s": exc.timeout_s,
        }


# ---------- Resource implementations ----------


def _resource_repo_map(config: cfg_mod.Config) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT canonical_name, kind, display_name
              FROM symbols
             WHERE deleted_at IS NULL
               AND kind IN ('module', 'class', 'function', 'method')
             ORDER BY canonical_name ASC
            """
        ).fetchall()
    finally:
        db_mod.close(conn)
    return {
        "symbols": [
            {
                "canonical_name": r["canonical_name"],
                "kind": r["kind"],
                "display_name": r["display_name"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


def _resource_symbol(config: cfg_mod.Config, name: str) -> dict:
    return _tool_find_symbol(config, name, references=True, limit=10)


def _resource_chunk(config: cfg_mod.Config, chunk_uid: str) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        row = conn.execute(
            """
            SELECT chunk_uid, file_path, chunk_type, symbol_name, symbol_path,
                   signature, start_line, end_line, context_json, content
              FROM chunks
             WHERE chunk_uid = ? AND deleted_at IS NULL
            """,
            (chunk_uid,),
        ).fetchone()
    finally:
        db_mod.close(conn)
    if row is None:
        return {"error": "chunk not found", "chunk_uid": chunk_uid}
    return dict(row)
