"""Shared read-only retrieval broker.

The broker owns the small deterministic contract used by evals and future
context surfaces. It does not apply schema or write to SQLite; callers should
open and prepare the connection before invoking :func:`retrieve`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from code_index.search import fts


class SourceKind(str, Enum):
    FILE_PATH = "file_path"
    CODE_CHUNK = "code_chunk"
    TRANSCRIPT_EVENT = "transcript_event"
    DIAGNOSTIC = "diagnostic"
    AFFECTED_TEST = "affected_test"
    TASK_GRAPH = "task_graph"


class TruncationReason(str, Enum):
    NONE = "none"
    BYTE_BUDGET = "byte_budget"
    LIMIT = "limit"


DEFAULT_SOURCES: tuple[SourceKind, ...] = (
    SourceKind.FILE_PATH,
    SourceKind.CODE_CHUNK,
    SourceKind.TRANSCRIPT_EVENT,
)
SELECTED_CONTEXT_SOURCES: tuple[SourceKind, ...] = (
    SourceKind.DIAGNOSTIC,
    SourceKind.AFFECTED_TEST,
    SourceKind.TASK_GRAPH,
)
ALL_SOURCES: tuple[SourceKind, ...] = DEFAULT_SOURCES + SELECTED_CONTEXT_SOURCES
MAX_SELECTED_PATHS = 50
MAX_SELECTED_NODES = 50
SELECTED_PATH_SCORE_BOOST = 1_000.0
TASK_GRAPH_BUILDER = (
    "code_index.commands.graph_server_dispatch._build_task_graph_context"
)

SOURCE_KIND_ALIASES: dict[str, SourceKind] = {
    "file": SourceKind.FILE_PATH,
    "files": SourceKind.FILE_PATH,
    "file_path": SourceKind.FILE_PATH,
    "path": SourceKind.FILE_PATH,
    "paths": SourceKind.FILE_PATH,
    "chunk": SourceKind.CODE_CHUNK,
    "chunks": SourceKind.CODE_CHUNK,
    "code": SourceKind.CODE_CHUNK,
    "code_chunk": SourceKind.CODE_CHUNK,
    "fts": SourceKind.CODE_CHUNK,
    "symbol": SourceKind.CODE_CHUNK,
    "symbols": SourceKind.CODE_CHUNK,
    "transcript": SourceKind.TRANSCRIPT_EVENT,
    "transcripts": SourceKind.TRANSCRIPT_EVENT,
    "transcript_event": SourceKind.TRANSCRIPT_EVENT,
    "agent_event": SourceKind.TRANSCRIPT_EVENT,
    "diag": SourceKind.DIAGNOSTIC,
    "diagnostic": SourceKind.DIAGNOSTIC,
    "diagnostics": SourceKind.DIAGNOSTIC,
    "lint": SourceKind.DIAGNOSTIC,
    "lints": SourceKind.DIAGNOSTIC,
    "affected_test": SourceKind.AFFECTED_TEST,
    "affected_tests": SourceKind.AFFECTED_TEST,
    "test": SourceKind.AFFECTED_TEST,
    "tests": SourceKind.AFFECTED_TEST,
    "pytest": SourceKind.AFFECTED_TEST,
    "task_graph": SourceKind.TASK_GRAPH,
    "graph_context": SourceKind.TASK_GRAPH,
    "context_graph": SourceKind.TASK_GRAPH,
    "selected_graph": SourceKind.TASK_GRAPH,
}

SCOPE_SOURCES: dict[str, tuple[SourceKind, ...]] = {
    "all": ALL_SOURCES,
    "graph": ALL_SOURCES,
    "files": (SourceKind.FILE_PATH, SourceKind.CODE_CHUNK),
    "file": (SourceKind.FILE_PATH, SourceKind.CODE_CHUNK),
    "code": (SourceKind.FILE_PATH, SourceKind.CODE_CHUNK),
    "transcripts": (SourceKind.TRANSCRIPT_EVENT,),
    "transcript": (SourceKind.TRANSCRIPT_EVENT,),
    "diagnostics": (SourceKind.DIAGNOSTIC,),
    "diagnostic": (SourceKind.DIAGNOSTIC,),
    "affected_tests": (SourceKind.AFFECTED_TEST,),
    "affected_test": (SourceKind.AFFECTED_TEST,),
    "tests": (SourceKind.AFFECTED_TEST,),
    "task_graph": (SourceKind.TASK_GRAPH,),
    "graph_context": (SourceKind.TASK_GRAPH,),
    "activity": (
        SourceKind.TRANSCRIPT_EVENT,
        SourceKind.DIAGNOSTIC,
        SourceKind.AFFECTED_TEST,
    ),
}


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    limit: int = 10
    budget_bytes: int = 20_000
    byte_budget: int | None = None
    sources: tuple[SourceKind | str, ...] = DEFAULT_SOURCES
    include_kinds: tuple[SourceKind | str, ...] = ()
    scope: str | None = None
    selected_paths: tuple[str, ...] = ()
    selected_nodes: tuple[Any, ...] = ()
    graph_config: Any | None = None
    language: str | None = None
    chunk_type: str | None = None
    per_source_limit: int | None = None

    def normalized_sources(self) -> tuple[SourceKind, ...]:
        out: list[SourceKind] = []
        sources: tuple[SourceKind | str, ...]
        if self.include_kinds:
            sources = self.include_kinds
        elif self.scope and self.scope.lower() in SCOPE_SOURCES:
            sources = SCOPE_SOURCES[self.scope.lower()]
        else:
            sources = self.sources or DEFAULT_SOURCES
        for source in sources:
            normalized = _source_kind(source)
            if normalized not in out:
                out.append(normalized)
        return tuple(out)


@dataclass(frozen=True)
class RetrievalResult:
    handle: str
    source_kind: SourceKind
    byte_cost: int
    provenance: dict[str, Any]
    score: float
    why_included: str
    truncation_reason: TruncationReason
    payload: dict[str, Any]
    result_kind: str
    result_id: str
    file_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "handle": self.handle,
            "source_kind": self.source_kind.value,
            "kind": self.result_kind,
            "id": self.result_id,
            "byte_cost": self.byte_cost,
            "provenance": dict(self.provenance),
            "score": self.score,
            "why_included": self.why_included,
            "truncation_reason": self.truncation_reason.value,
            "payload": dict(self.payload),
            "source": self.source_kind.value,
        }
        if self.file_path:
            out["file_path"] = self.file_path
        return out


@dataclass(frozen=True)
class RetrievalResponse:
    query: str
    limit: int
    budget_bytes: int
    bytes_used: int
    results: tuple[RetrievalResult, ...]
    candidate_count: int
    truncation_reason: TruncationReason

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "code_index_retrieval",
            "query": self.query,
            "limit": self.limit,
            "budget_bytes": self.budget_bytes,
            "bytes_used": self.bytes_used,
            "candidate_count": self.candidate_count,
            "truncation_reason": self.truncation_reason.value,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class _Candidate:
    handle: str
    source_kind: SourceKind
    result_kind: str
    result_id: str
    score: float
    provenance: dict[str, Any]
    why_included: str
    payload: dict[str, Any]
    body: str
    sort_key: tuple[Any, ...]
    file_path: str | None = None


def retrieve(conn: sqlite3.Connection, request: RetrievalRequest) -> RetrievalResponse:
    query = str(request.query or "").strip()
    limit = max(0, int(request.limit or 0))
    budget_source = (
        request.byte_budget
        if request.byte_budget is not None
        else request.budget_bytes
    )
    budget_bytes = max(0, int(budget_source or 0))
    if not query or limit <= 0 or budget_bytes <= 0:
        return RetrievalResponse(
            query=query,
            limit=limit,
            budget_bytes=budget_bytes,
            bytes_used=0,
            results=(),
            candidate_count=0,
            truncation_reason=TruncationReason.NONE,
        )

    per_source_limit = (
        max(0, int(request.per_source_limit))
        if request.per_source_limit is not None
        else max(limit * 2, limit, 1)
    )
    selected_paths = _normalize_selected_paths(request.selected_paths)
    selected_nodes = _normalize_selected_nodes(request.selected_nodes)
    collectors: dict[SourceKind, Callable[[int], list[_Candidate]]] = {
        SourceKind.FILE_PATH: lambda source_rank: _collect_file_paths(
            conn,
            query,
            limit=per_source_limit,
            source_rank=source_rank,
            selected_paths=selected_paths,
        ),
        SourceKind.CODE_CHUNK: lambda source_rank: _collect_code_chunks(
            conn,
            query,
            limit=per_source_limit,
            source_rank=source_rank,
            language=request.language,
            chunk_type=request.chunk_type,
        ),
        SourceKind.TRANSCRIPT_EVENT: lambda source_rank: _collect_transcript_events(
            conn, query, limit=per_source_limit, source_rank=source_rank
        ),
        SourceKind.DIAGNOSTIC: lambda source_rank: _collect_diagnostics(
            conn,
            limit=per_source_limit,
            source_rank=source_rank,
            selected_paths=selected_paths,
        ),
        SourceKind.AFFECTED_TEST: lambda source_rank: _collect_affected_tests(
            conn,
            limit=per_source_limit,
            source_rank=source_rank,
            selected_paths=selected_paths,
        ),
        SourceKind.TASK_GRAPH: lambda source_rank: _collect_task_graph(
            limit=per_source_limit,
            source_rank=source_rank,
            selected_nodes=selected_nodes,
            selected_paths=selected_paths,
            graph_config=request.graph_config,
        ),
    }
    candidates: list[_Candidate] = []
    for source_rank, source in enumerate(request.normalized_sources()):
        collector = collectors.get(source)
        if collector is None:
            continue
        source_candidates = _apply_selected_path_boost(
            collector(source_rank), selected_paths
        )
        candidates.extend(_cap_candidates(source_candidates, per_source_limit))

    candidates = _dedupe_candidates(candidates)
    results, bytes_used = _apply_budget(candidates, limit=limit, budget_bytes=budget_bytes)
    reason = _response_truncation_reason(
        candidates, results, bytes_used=bytes_used, limit=limit, budget_bytes=budget_bytes
    )
    return RetrievalResponse(
        query=query,
        limit=limit,
        budget_bytes=budget_bytes,
        bytes_used=bytes_used,
        results=tuple(results),
        candidate_count=len(candidates),
        truncation_reason=reason,
    )


def _collect_file_paths(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    source_rank: int,
    selected_paths: tuple[str, ...],
) -> list[_Candidate]:
    if limit <= 0:
        return []
    path_query = _normalize_path_query(query)
    out: list[_Candidate] = []
    if selected_paths:
        for rank, row in enumerate(
            _selected_file_rows(conn, selected_paths, limit=limit)
        ):
            file_path = str(row["file_path"] or "")
            if not file_path:
                continue
            selected_path = _selected_path_match(file_path, selected_paths)
            relation = "selected_exact" if selected_path == file_path else "selected_descendant"
            score = -1.0 if relation == "selected_exact" else -0.9
            out.append(
                _file_path_candidate(
                    row,
                    source_rank=source_rank,
                    rank=rank,
                    match_kind=relation,
                    score=score,
                )
            )
    if not path_query:
        return out
    contains_pattern = f"%{_escape_like(path_query)}%"
    prefix_pattern = f"{_escape_like(path_query)}%"
    rows = conn.execute(
        """
        SELECT file_path, language, parse_status
          FROM files
         WHERE deleted_at IS NULL
           AND file_path LIKE ? ESCAPE '\\'
         ORDER BY
           CASE WHEN file_path = ? THEN 0
                WHEN file_path LIKE ? ESCAPE '\\' THEN 1
                ELSE 2 END,
           file_path ASC
         LIMIT ?
        """,
        (contains_pattern, path_query, prefix_pattern, int(limit)),
    ).fetchall()
    for rank, row in enumerate(rows):
        file_path = str(row["file_path"] or "")
        if not file_path:
            continue
        match_kind = (
            "exact"
            if file_path == path_query
            else "prefix"
            if file_path.startswith(path_query)
            else "contains"
        )
        score = {"exact": 0.0, "prefix": 0.1, "contains": 0.2}[match_kind]
        out.append(
            _file_path_candidate(
                row,
                source_rank=source_rank,
                rank=rank,
                match_kind=match_kind,
                score=score,
            )
        )
    return out


def _file_path_candidate(
    row: sqlite3.Row,
    *,
    source_rank: int,
    rank: int,
    match_kind: str,
    score: float,
) -> _Candidate:
    file_path = str(row["file_path"] or "")
    selected_match = match_kind.startswith("selected_")
    why = (
        "selected file path context"
        if selected_match
        else f"file path {match_kind} match"
    )
    return _Candidate(
        handle=f"file:{file_path}",
        source_kind=SourceKind.FILE_PATH,
        result_kind="file",
        result_id=file_path,
        score=score,
        provenance={
            "collector": "file_path",
            "table": "files",
            "match": match_kind,
            "file_path": file_path,
        },
        why_included=why,
        payload={
            "file_path": file_path,
            "language": row["language"],
            "parse_status": row["parse_status"],
        },
        body=file_path,
        sort_key=(source_rank, score, file_path, rank),
        file_path=file_path,
    )


def _collect_code_chunks(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    source_rank: int,
    language: str | None,
    chunk_type: str | None,
) -> list[_Candidate]:
    if limit <= 0:
        return []
    try:
        rows = fts.search(
            conn, query, limit=limit, language=language, chunk_type=chunk_type
        )
    except sqlite3.OperationalError:
        fallback = " ".join(re.findall(r"[A-Za-z0-9_.:-]+", query))
        if not fallback:
            return []
        try:
            rows = fts.search(
                conn,
                fallback,
                limit=limit,
                language=language,
                chunk_type=chunk_type,
            )
        except sqlite3.OperationalError:
            return []
    out: list[_Candidate] = []
    for rank, row in enumerate(rows):
        chunk_uid = str(row.get("chunk_uid") or "")
        file_path = str(row.get("file_path") or "")
        if not chunk_uid:
            continue
        score = _float_or_zero(row.get("score"))
        payload = {
            "chunk_uid": chunk_uid,
            "file_path": file_path,
            "language": row.get("language"),
            "chunk_type": row.get("chunk_type"),
            "symbol_name": row.get("symbol_name"),
            "symbol_path": row.get("symbol_path"),
            "signature": row.get("signature") or "",
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
        }
        out.append(
            _Candidate(
                handle=f"chunk:{chunk_uid}",
                source_kind=SourceKind.CODE_CHUNK,
                result_kind="chunk",
                result_id=chunk_uid,
                score=score,
                provenance={
                    "collector": "code_chunk_fts",
                    "engine": "sqlite_fts5",
                    "table": "chunks/chunks_fts",
                    "chunk_uid": chunk_uid,
                    "file_path": file_path,
                },
                why_included="code chunk full-text match",
                payload=payload,
                body=_render_chunk_text(row),
                sort_key=(
                    source_rank,
                    score,
                    file_path,
                    int(row.get("start_line") or 0),
                    chunk_uid,
                    rank,
                ),
                file_path=file_path or None,
            )
        )
    return out


def _collect_transcript_events(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    source_rank: int,
) -> list[_Candidate]:
    if limit <= 0:
        return []
    pattern = f"%{_escape_like(query)}%"
    try:
        rows = conn.execute(
            """
            SELECT e.event_pk, r.run_id, r.agent_name, r.status, r.prompt,
                   e.timestamp, e.event_type, e.file_path, e.symbol_path,
                   e.message
              FROM agent_events e
              JOIN agent_runs r ON r.run_pk = e.run_pk
             WHERE COALESCE(e.message, '') LIKE ? ESCAPE '\\'
                OR COALESCE(e.file_path, '') LIKE ? ESCAPE '\\'
                OR COALESCE(e.symbol_path, '') LIKE ? ESCAPE '\\'
                OR COALESCE(e.payload_json, '') LIKE ? ESCAPE '\\'
                OR COALESCE(r.prompt, '') LIKE ? ESCAPE '\\'
             ORDER BY e.event_pk DESC
             LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, pattern, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[_Candidate] = []
    for rank, row in enumerate(rows):
        event_pk = int(row["event_pk"])
        payload = {
            "event_pk": event_pk,
            "run_id": row["run_id"],
            "agent_name": row["agent_name"],
            "status": row["status"],
            "prompt": row["prompt"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "file_path": row["file_path"],
            "symbol_path": row["symbol_path"],
            "message": row["message"] or "",
        }
        out.append(
            _Candidate(
                handle=f"transcript_event:{event_pk}",
                source_kind=SourceKind.TRANSCRIPT_EVENT,
                result_kind="transcript_event",
                result_id=str(event_pk),
                score=float(rank),
                provenance={
                    "collector": "transcript_event",
                    "table": "agent_events",
                    "event_pk": event_pk,
                    "run_id": row["run_id"],
                },
                why_included="transcript event matches query",
                payload=payload,
                body=_render_transcript_text(row),
                sort_key=(source_rank, rank, -event_pk),
                file_path=str(row["file_path"] or "") or None,
            )
        )
    return out


def _collect_diagnostics(
    conn: sqlite3.Connection,
    *,
    limit: int,
    source_rank: int,
    selected_paths: tuple[str, ...],
) -> list[_Candidate]:
    if limit <= 0 or not selected_paths:
        return []
    where_sql, params = _selected_path_where("f.file_path", selected_paths)
    if not where_sql:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT d.diagnostic_pk, f.file_path, d.tool, d.code, d.severity,
                   d.start_line, d.end_line, d.message, d.observed_at
              FROM diagnostics d
              JOIN files f ON f.file_pk = d.file_pk
             WHERE ({where_sql})
               AND f.deleted_at IS NULL
             ORDER BY
               CASE COALESCE(d.severity, '')
                 WHEN 'error' THEN 0
                 WHEN 'warning' THEN 1
                 ELSE 2
               END,
               f.file_path ASC,
               d.start_line ASC,
               d.diagnostic_pk ASC
             LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[_Candidate] = []
    for rank, row in enumerate(rows):
        diagnostic_pk = int(row["diagnostic_pk"])
        severity = str(row["severity"] or "")
        severity_rank = {"error": 0, "warning": 1, "info": 2}.get(severity, 3)
        payload = {
            "diagnostic_pk": diagnostic_pk,
            "file_path": row["file_path"],
            "tool": row["tool"],
            "code": row["code"],
            "severity": row["severity"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "message": row["message"] or "",
            "observed_at": row["observed_at"],
        }
        out.append(
            _Candidate(
                handle=f"diagnostic:{diagnostic_pk}",
                source_kind=SourceKind.DIAGNOSTIC,
                result_kind="diagnostic",
                result_id=str(diagnostic_pk),
                score=float(severity_rank) + (rank / 1000),
                provenance={
                    "collector": "diagnostic",
                    "table": "diagnostics",
                    "diagnostic_pk": diagnostic_pk,
                    "file_path": row["file_path"],
                },
                why_included="diagnostic on selected path",
                payload=payload,
                body=_render_diagnostic_text(payload),
                sort_key=(
                    source_rank,
                    severity_rank,
                    str(row["file_path"] or ""),
                    int(row["start_line"] or 0),
                    diagnostic_pk,
                    rank,
                ),
                file_path=str(row["file_path"] or "") or None,
            )
        )
    return out


def _collect_affected_tests(
    conn: sqlite3.Connection,
    *,
    limit: int,
    source_rank: int,
    selected_paths: tuple[str, ...],
) -> list[_Candidate]:
    if limit <= 0 or not selected_paths:
        return []
    where_sql, params = _selected_path_where("target_file.file_path", selected_paths)
    if not where_sql:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT te.edge_pk,
                   test.symbol_uid, test.canonical_name, test.kind,
                   te.edge_type, te.depth, te.confidence, te.path_json,
                   te.provenance,
                   target.symbol_uid AS matched_symbol_uid,
                   target.canonical_name AS matched_canonical_name,
                   target.kind AS matched_kind,
                   (SELECT f2.file_path FROM occurrences o2
                      JOIN files f2 ON f2.file_pk = o2.file_pk
                     WHERE o2.symbol_pk = test.symbol_pk
                       AND o2.role = 'definition'
                       AND f2.deleted_at IS NULL
                     ORDER BY o2.start_line ASC LIMIT 1) AS def_file,
                   (SELECT o2.start_line FROM occurrences o2
                     WHERE o2.symbol_pk = test.symbol_pk
                       AND o2.role = 'definition'
                     ORDER BY o2.start_line ASC LIMIT 1) AS def_line,
                   (SELECT c.context_json FROM chunks c
                     WHERE c.primary_symbol_pk = test.symbol_pk
                       AND c.deleted_at IS NULL
                     ORDER BY c.chunk_pk ASC LIMIT 1) AS context_json
              FROM test_edges te
              JOIN symbols test ON test.symbol_pk = te.test_symbol_pk
              JOIN symbols target ON target.symbol_pk = te.target_symbol_pk
              JOIN occurrences target_def ON target_def.symbol_pk = target.symbol_pk
              JOIN files target_file ON target_file.file_pk = target_def.file_pk
             WHERE target_def.role = 'definition'
               AND ({where_sql})
               AND target_file.deleted_at IS NULL
               AND test.deleted_at IS NULL
               AND target.deleted_at IS NULL
             ORDER BY te.depth ASC, test.canonical_name ASC, target.canonical_name ASC
             LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[_Candidate] = []
    seen: set[str] = set()
    for rank, row in enumerate(rows):
        symbol_uid = str(row["symbol_uid"] or "")
        if not symbol_uid or symbol_uid in seen:
            continue
        seen.add(symbol_uid)
        raw_path = _json_loads(row["path_json"], [])
        path = [str(item) for item in raw_path] if isinstance(raw_path, list) else []
        context = _json_loads(row["context_json"], {})
        payload = {
            "edge_pk": row["edge_pk"],
            "symbol_uid": symbol_uid,
            "canonical_name": row["canonical_name"],
            "kind": row["kind"],
            "def_file": row["def_file"],
            "def_line": row["def_line"],
            "edge_type": row["edge_type"],
            "depth": row["depth"],
            "confidence": row["confidence"],
            "path": path,
            "rationale": " -> ".join(path) if path else row["canonical_name"],
            "parametrize": context.get("parametrize") if isinstance(context, dict) else None,
            "matched_target": {
                "symbol_uid": row["matched_symbol_uid"],
                "canonical_name": row["matched_canonical_name"],
                "kind": row["matched_kind"],
            },
        }
        out.append(
            _Candidate(
                handle=f"affected_test:{symbol_uid}",
                source_kind=SourceKind.AFFECTED_TEST,
                result_kind="affected_test",
                result_id=symbol_uid,
                score=float(row["depth"] or 0) + (rank / 1000),
                provenance={
                    "collector": "affected_test",
                    "table": "test_edges",
                    "edge_pk": row["edge_pk"],
                    "symbol_uid": symbol_uid,
                },
                why_included="affected test for selected path",
                payload=payload,
                body=_render_affected_test_text(payload),
                sort_key=(
                    source_rank,
                    int(row["depth"] or 0),
                    str(row["canonical_name"] or ""),
                    symbol_uid,
                    rank,
                ),
                file_path=str(row["def_file"] or "") or None,
            )
        )
    return out


def _collect_task_graph(
    *,
    limit: int,
    source_rank: int,
    selected_nodes: tuple[str, ...],
    selected_paths: tuple[str, ...],
    graph_config: Any | None,
) -> list[_Candidate]:
    if limit <= 0 or not selected_nodes:
        return []
    context: dict[str, Any]
    status = "deferred"
    why = "task graph context deferred because retrieval broker has no config"
    if graph_config is None:
        context = {
            "kind": "code_index_task_graph_context_deferred",
            "reason": "config_unavailable",
            "detail": (
                "Task graph context requires a repo config; retrieve(conn, request) "
                "only received a SQLite connection."
            ),
            "builder": TASK_GRAPH_BUILDER,
            "selected_nodes": list(selected_nodes),
            "selected_paths": list(selected_paths),
        }
    else:
        try:
            from code_index.commands.graph_server_dispatch import (
                _build_task_graph_context,
            )

            context = _build_task_graph_context(
                graph_config,
                agent_name="RetrievalBroker",
                selected_nodes=list(selected_nodes),
                selected_paths=list(selected_paths),
                max_nodes=max(1, min(24, int(limit))),
                max_bytes=24_000,
            )
            status = (
                "built"
                if context.get("kind") == "code_index_graph_context"
                else "error"
            )
            why = "task graph context for selected nodes"
        except Exception as exc:  # pragma: no cover - defensive best effort.
            context = {
                "kind": "code_index_graph_context_error",
                "error": str(exc),
                "builder": TASK_GRAPH_BUILDER,
            }
            status = "error"
            why = "task graph context builder failed"
    payload = {
        "status": status,
        "context_kind": context.get("kind"),
        "selected_nodes": list(selected_nodes),
        "selected_paths": list(selected_paths),
        "node_count": len(context.get("nodes") or []),
        "edge_count": len(context.get("edges") or []),
        "builder": TASK_GRAPH_BUILDER,
    }
    return [
        _Candidate(
            handle="task_graph:selected_nodes",
            source_kind=SourceKind.TASK_GRAPH,
            result_kind="task_graph_context",
            result_id="selected_nodes",
            score=0.0,
            provenance={
                "collector": "task_graph",
                "builder": TASK_GRAPH_BUILDER,
                "status": status,
            },
            why_included=why,
            payload=payload,
            body=json.dumps(context, sort_keys=True, separators=(",", ":")),
            sort_key=(source_rank, 0, status),
            file_path=None,
        )
    ]


def _cap_candidates(candidates: list[_Candidate], limit: int) -> list[_Candidate]:
    if limit <= 0:
        return []
    return sorted(candidates, key=lambda item: item.sort_key)[:limit]


def _apply_selected_path_boost(
    candidates: list[_Candidate], selected_paths: tuple[str, ...]
) -> list[_Candidate]:
    if not selected_paths:
        return candidates
    out: list[_Candidate] = []
    for candidate in candidates:
        source_rank = candidate.sort_key[0] if candidate.sort_key else 0
        rest = candidate.sort_key[1:] if candidate.sort_key else ()
        matched_path = _selected_path_match(candidate.file_path, selected_paths)
        if not matched_path:
            out.append(replace(candidate, sort_key=(1, source_rank, *rest)))
            continue
        relation = "exact" if candidate.file_path == matched_path else "descendant"
        provenance = dict(candidate.provenance)
        provenance["selected_path"] = matched_path
        provenance["selected_path_match"] = relation
        why = candidate.why_included
        if "selected path" not in why:
            why = f"{why}; selected path context"
        out.append(
            replace(
                candidate,
                score=candidate.score - SELECTED_PATH_SCORE_BOOST,
                provenance=provenance,
                why_included=why,
                sort_key=(0, source_rank, *rest),
            )
        )
    return out


def _selected_file_rows(
    conn: sqlite3.Connection,
    selected_paths: tuple[str, ...],
    *,
    limit: int,
) -> list[sqlite3.Row]:
    if limit <= 0 or not selected_paths:
        return []
    where_sql, params = _selected_path_where("file_path", selected_paths)
    if not where_sql:
        return []
    try:
        return conn.execute(
            f"""
            SELECT file_path, language, parse_status
              FROM files
             WHERE deleted_at IS NULL
               AND ({where_sql})
             ORDER BY file_path ASC
             LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _selected_path_where(
    column: str, selected_paths: tuple[str, ...]
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for path in selected_paths:
        if not path:
            continue
        prefix = f"{_escape_like(path.rstrip('/'))}/%"
        clauses.append(f"({column} = ? OR {column} LIKE ? ESCAPE '\\')")
        params.extend([path, prefix])
    return " OR ".join(clauses), params


def _dedupe_candidates(candidates: list[_Candidate]) -> list[_Candidate]:
    matched_paths = {
        str(candidate.file_path)
        for candidate in candidates
        if candidate.source_kind is SourceKind.FILE_PATH and candidate.file_path
    }
    out: list[_Candidate] = []
    seen_handles: set[str] = set()
    seen_chunk_spans: set[tuple[str, int, int, str]] = set()
    for candidate in sorted(candidates, key=lambda item: item.sort_key):
        if candidate.handle in seen_handles:
            continue
        if candidate.source_kind is SourceKind.CODE_CHUNK:
            file_path = str(candidate.payload.get("file_path") or "")
            chunk_type = str(candidate.payload.get("chunk_type") or "")
            if chunk_type == "file" and file_path in matched_paths:
                continue
            span = (
                file_path,
                int(candidate.payload.get("start_line") or 0),
                int(candidate.payload.get("end_line") or 0),
                str(candidate.payload.get("symbol_path") or ""),
            )
            if span[0] and span in seen_chunk_spans:
                continue
            if span[0]:
                seen_chunk_spans.add(span)
        seen_handles.add(candidate.handle)
        out.append(candidate)
    return out


def _apply_budget(
    candidates: list[_Candidate],
    *,
    limit: int,
    budget_bytes: int,
) -> tuple[list[RetrievalResult], int]:
    results: list[RetrievalResult] = []
    used = 0
    for candidate in candidates:
        if len(results) >= limit:
            break
        remaining = budget_bytes - used
        if remaining <= 0:
            break
        text, truncated = _clip_utf8(candidate.body, remaining)
        cost = _utf8_len(text)
        if cost <= 0:
            continue
        payload = dict(candidate.payload)
        payload["text"] = text
        results.append(
            RetrievalResult(
                handle=candidate.handle,
                source_kind=candidate.source_kind,
                byte_cost=cost,
                provenance=dict(candidate.provenance),
                score=candidate.score,
                why_included=candidate.why_included,
                truncation_reason=(
                    TruncationReason.BYTE_BUDGET
                    if truncated
                    else TruncationReason.NONE
                ),
                payload=payload,
                result_kind=candidate.result_kind,
                result_id=candidate.result_id,
                file_path=candidate.file_path,
            )
        )
        used += cost
        if truncated:
            break
    return results, used


def _response_truncation_reason(
    candidates: list[_Candidate],
    results: list[RetrievalResult],
    *,
    bytes_used: int,
    limit: int,
    budget_bytes: int,
) -> TruncationReason:
    if any(result.truncation_reason is TruncationReason.BYTE_BUDGET for result in results):
        return TruncationReason.BYTE_BUDGET
    if len(results) < len(candidates) and bytes_used >= budget_bytes:
        return TruncationReason.BYTE_BUDGET
    if len(results) >= limit and len(results) < len(candidates):
        return TruncationReason.LIMIT
    return TruncationReason.NONE


def _render_chunk_text(row: dict[str, Any]) -> str:
    file_path = str(row.get("file_path") or "")
    start_line = row.get("start_line")
    end_line = row.get("end_line")
    symbol = str(row.get("symbol_path") or row.get("symbol_name") or "").strip()
    signature = str(row.get("signature") or "").strip()
    snippet = str(row.get("snippet") or "").strip()
    parts = [_location(file_path, start_line, end_line)]
    if symbol:
        parts.append(symbol)
    if signature:
        parts.append(signature)
    if snippet:
        parts.append(snippet)
    return "\n".join(part for part in parts if part)


def _render_transcript_text(row: sqlite3.Row) -> str:
    parts = [
        str(row["timestamp"] or ""),
        str(row["agent_name"] or "Agent"),
        str(row["event_type"] or ""),
    ]
    file_path = str(row["file_path"] or "")
    symbol_path = str(row["symbol_path"] or "")
    message = str(row["message"] or row["prompt"] or "")
    if file_path:
        parts.append(file_path)
    if symbol_path:
        parts.append(symbol_path)
    if message:
        parts.append(message)
    return "\n".join(part for part in parts if part)


def _render_diagnostic_text(payload: dict[str, Any]) -> str:
    parts = [
        _location(
            str(payload.get("file_path") or ""),
            payload.get("start_line"),
            payload.get("end_line"),
        ),
        " ".join(
            part
            for part in (
                str(payload.get("severity") or "").strip(),
                str(payload.get("tool") or "").strip(),
                str(payload.get("code") or "").strip(),
            )
            if part
        ),
        str(payload.get("message") or "").strip(),
    ]
    return "\n".join(part for part in parts if part)


def _render_affected_test_text(payload: dict[str, Any]) -> str:
    target = payload.get("matched_target") or {}
    parts = [
        str(payload.get("canonical_name") or ""),
        _location(
            str(payload.get("def_file") or ""),
            payload.get("def_line"),
            payload.get("def_line"),
        ),
        " ".join(
            str(part)
            for part in (
                payload.get("edge_type"),
                f"depth={payload.get('depth')}",
                f"confidence={payload.get('confidence')}",
            )
            if part is not None and str(part)
        ),
        f"matches {target.get('canonical_name') or target.get('symbol_uid') or ''}",
        str(payload.get("rationale") or ""),
    ]
    return "\n".join(part for part in parts if part)


def _location(file_path: str, start_line: Any, end_line: Any) -> str:
    if not file_path:
        return ""
    if start_line is None:
        return file_path
    if end_line is None or end_line == start_line:
        return f"{file_path}:{start_line}"
    return f"{file_path}:{start_line}-{end_line}"


def _clip_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = str(text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text or ""), False
    if max_bytes <= 0:
        return "", bool(raw)
    clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
    return clipped, True


def _utf8_len(text: str) -> int:
    return len(str(text or "").encode("utf-8"))


def _escape_like(query: str) -> str:
    return str(query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_path_query(query: str) -> str:
    text = str(query or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _normalize_selected_paths(paths: Any) -> tuple[str, ...]:
    if not paths:
        return ()
    raw_paths = [paths] if isinstance(paths, str) else list(paths)
    out: list[str] = []
    for item in raw_paths:
        text = _normalize_path_query(str(item)).rstrip("/")
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_SELECTED_PATHS:
            break
    return tuple(out)


def _normalize_selected_nodes(nodes: Any) -> tuple[str, ...]:
    if not nodes:
        return ()
    raw_nodes = [nodes] if isinstance(nodes, str) else list(nodes)
    out: list[str] = []
    for item in raw_nodes:
        value: Any = item
        if isinstance(item, dict):
            value = item.get("id") or item.get("stable_id") or item.get("path")
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_SELECTED_NODES:
            break
    return tuple(out)


def _selected_path_match(
    file_path: str | None, selected_paths: tuple[str, ...]
) -> str | None:
    path = _normalize_path_query(file_path or "").rstrip("/")
    if not path:
        return None
    for selected_path in selected_paths:
        if path == selected_path or path.startswith(f"{selected_path}/"):
            return selected_path
    return None


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _source_kind(value: SourceKind | str) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    text = str(value or "").strip().lower().replace("-", "_")
    return SOURCE_KIND_ALIASES.get(text) or SourceKind(text)


def _json_loads(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return fallback
