"""Search broker helpers for the live graph server."""

from __future__ import annotations

from typing import Any

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import retrieval


def _search_sources_for_scope(scope: str) -> tuple[retrieval.SourceKind, ...]:
    if scope == "files":
        return (retrieval.SourceKind.FILE_PATH, retrieval.SourceKind.CODE_CHUNK)
    if scope == "transcripts":
        return (retrieval.SourceKind.TRANSCRIPT_EVENT,)
    return retrieval.DEFAULT_SOURCES


def _broker_file_result(
    item: dict[str, Any],
    *,
    path_match_also: bool = False,
) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    source_kind = str(item.get("source_kind") or "")
    if source_kind == retrieval.SourceKind.FILE_PATH.value:
        return {
            "kind": "file_path",
            "file_path": payload.get("file_path") or item.get("file_path"),
            "language": payload.get("language"),
            "parse_status": payload.get("parse_status"),
            "score": item.get("score"),
            "snippet": payload.get("text") or payload.get("file_path") or "",
            "handle": item.get("handle"),
            "byte_cost": item.get("byte_cost"),
        }
    out = {
        "kind": "file_content",
        "file_path": payload.get("file_path") or item.get("file_path"),
        "language": payload.get("language"),
        "chunk_type": payload.get("chunk_type"),
        "symbol_name": payload.get("symbol_name"),
        "symbol_path": payload.get("symbol_path"),
        "signature": payload.get("signature"),
        "start_line": payload.get("start_line"),
        "end_line": payload.get("end_line"),
        "score": item.get("score"),
        "snippet": payload.get("text") or "",
        "handle": item.get("handle"),
        "byte_cost": item.get("byte_cost"),
    }
    if path_match_also:
        out["path_match_also"] = True
    return out


def _broker_transcript_result(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return {
        "kind": "transcript_event",
        "event_pk": payload.get("event_pk"),
        "run_id": payload.get("run_id"),
        "agent_name": payload.get("agent_name"),
        "status": payload.get("status"),
        "prompt": payload.get("prompt"),
        "timestamp": payload.get("timestamp"),
        "event_type": payload.get("event_type"),
        "file_path": payload.get("file_path") or item.get("file_path"),
        "symbol_path": payload.get("symbol_path"),
        "message": payload.get("message") or "",
        "snippet": payload.get("text") or "",
        "handle": item.get("handle"),
        "byte_cost": item.get("byte_cost"),
    }


def _build_search_payload(
    config: cfg_mod.Config,
    *,
    query: str,
    scope: str,
    limit: int,
) -> dict[str, Any]:
    normalized_scope = scope if scope in {"all", "files", "transcripts"} else "all"
    safe_limit = max(1, min(50, int(limit or 12)))
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        broker_response = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query=query,
                limit=safe_limit,
                budget_bytes=20_000,
                sources=_search_sources_for_scope(normalized_scope),
                per_source_limit=safe_limit,
            ),
        ).to_dict()
        file_results: list[dict[str, Any]] = []
        transcript_results: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for item in broker_response.get("results") or []:
            if not isinstance(item, dict):
                continue
            source_kind = str(item.get("source_kind") or "")
            if source_kind == retrieval.SourceKind.FILE_PATH.value:
                result = _broker_file_result(item)
                path = str(result.get("file_path") or "")
                if path:
                    seen_paths.add(path)
                file_results.append(result)
            elif source_kind == retrieval.SourceKind.CODE_CHUNK.value:
                path = str((item.get("payload") or {}).get("file_path") or "")
                file_results.append(
                    _broker_file_result(item, path_match_also=path in seen_paths)
                )
            elif source_kind == retrieval.SourceKind.TRANSCRIPT_EVENT.value:
                transcript_results.append(_broker_transcript_result(item))
    finally:
        db_mod.close(conn)
    return {
        "ok": True,
        "kind": "code_index_graph_search",
        "query": query,
        "scope": normalized_scope,
        "limit": safe_limit,
        "files": file_results,
        "transcripts": transcript_results,
        "counts": {
            "files": len(file_results),
            "transcripts": len(transcript_results),
        },
        "retrieval": {
            "kind": broker_response.get("kind"),
            "bytes_used": broker_response.get("bytes_used"),
            "budget_bytes": broker_response.get("budget_bytes"),
            "candidate_count": broker_response.get("candidate_count"),
            "truncation_reason": broker_response.get("truncation_reason"),
        },
    }
