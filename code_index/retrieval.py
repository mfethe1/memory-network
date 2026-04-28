"""Shared read-only retrieval broker.

The broker owns the small deterministic contract used by evals and future
context surfaces. It does not apply schema or write to SQLite; callers should
open and prepare the connection before invoking :func:`retrieve`.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any

from code_index.search import fts


class SourceKind(str, Enum):
    FILE_PATH = "file_path"
    CODE_CHUNK = "code_chunk"
    TRANSCRIPT_EVENT = "transcript_event"


class TruncationReason(str, Enum):
    NONE = "none"
    BYTE_BUDGET = "byte_budget"
    LIMIT = "limit"


DEFAULT_SOURCES: tuple[SourceKind, ...] = (
    SourceKind.FILE_PATH,
    SourceKind.CODE_CHUNK,
    SourceKind.TRANSCRIPT_EVENT,
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
}

SCOPE_SOURCES: dict[str, tuple[SourceKind, ...]] = {
    "all": DEFAULT_SOURCES,
    "graph": DEFAULT_SOURCES,
    "files": (SourceKind.FILE_PATH, SourceKind.CODE_CHUNK),
    "file": (SourceKind.FILE_PATH, SourceKind.CODE_CHUNK),
    "code": (SourceKind.FILE_PATH, SourceKind.CODE_CHUNK),
    "transcripts": (SourceKind.TRANSCRIPT_EVENT,),
    "transcript": (SourceKind.TRANSCRIPT_EVENT,),
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
    candidates: list[_Candidate] = []
    for source_rank, source in enumerate(request.normalized_sources()):
        if source is SourceKind.FILE_PATH:
            candidates.extend(
                _collect_file_paths(
                    conn, query, limit=per_source_limit, source_rank=source_rank
                )
            )
        elif source is SourceKind.CODE_CHUNK:
            candidates.extend(
                _collect_code_chunks(
                    conn,
                    query,
                    limit=per_source_limit,
                    source_rank=source_rank,
                    language=request.language,
                    chunk_type=request.chunk_type,
                )
            )
        elif source is SourceKind.TRANSCRIPT_EVENT:
            candidates.extend(
                _collect_transcript_events(
                    conn, query, limit=per_source_limit, source_rank=source_rank
                )
            )

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
) -> list[_Candidate]:
    if limit <= 0:
        return []
    path_query = _normalize_path_query(query)
    if not path_query:
        return []
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
    out: list[_Candidate] = []
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
            _Candidate(
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
                why_included=f"file path {match_kind} match",
                payload={
                    "file_path": file_path,
                    "language": row["language"],
                    "parse_status": row["parse_status"],
                },
                body=file_path,
                sort_key=(source_rank, score, file_path, rank),
                file_path=file_path,
            )
        )
    return out


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
