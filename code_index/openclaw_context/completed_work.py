"""Completed Work Index helpers for local fumemory-compatible sync."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from code_index.openclaw_context.models import canonical_json
from code_index.openclaw_context.models import json_tuple
from code_index.openclaw_context.models import string_tuple
from code_index.openclaw_context.models import utc_now_iso


RAW_TRANSCRIPT_KEYS = {
    "events",
    "messages",
    "raw_transcript",
    "raw_transcript_text",
    "terminal_output",
    "transcript",
    "transcript_text",
}


@dataclass(frozen=True)
class CompletedWorkEntry:
    work_id: str
    task_id: str
    run_id: str
    files_changed: tuple[str, ...]
    symbols_affected: tuple[str, ...]
    approach_taken: str = ""
    approaches_rejected: tuple[Any, ...] = ()
    verification_results: dict[str, Any] | None = None
    follow_up_pointers: tuple[dict[str, Any], ...] = ()
    trace_id: str | None = None
    idempotency_key: str | None = None
    completed_at: str | None = None
    host_id: str | None = None
    repo_id: str | None = None
    source_event_offsets: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "idempotency_key": self.idempotency_key,
            "host_id": self.host_id,
            "repo_id": self.repo_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "completed_at": self.completed_at,
            "files_changed": list(self.files_changed),
            "symbols_affected": list(self.symbols_affected),
            "approach_taken": self.approach_taken,
            "approaches_rejected": list(self.approaches_rejected),
            "verification_results": dict(self.verification_results or {}),
            "follow_up_pointers": [dict(pointer) for pointer in self.follow_up_pointers],
            "trace_id": self.trace_id,
            "source_event_offsets": dict(self.source_event_offsets or {}),
            "metadata": dict(self.metadata or {}),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CompletedWorkRecordResult:
    stored: bool
    entry: CompletedWorkEntry | None = None
    idempotency_key: str | None = None
    degraded_reason: str | None = None
    error_message: str | None = None


def build_completed_work_entry(
    *,
    task_id: str,
    run_id: str,
    files_changed: list[str] | tuple[str, ...] = (),
    symbols_affected: list[str] | tuple[str, ...] = (),
    approach_taken: str = "",
    approaches_rejected: Any = (),
    verification_results: dict[str, Any] | None = None,
    follow_up_pointers: Any = (),
    trace_id: str | None = None,
    idempotency_key: str | None = None,
    work_id: str | None = None,
    completed_at: str | None = None,
    host_id: str | None = None,
    repo_id: str | None = None,
    source_event_offsets: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    include_raw_transcript: bool = False,
    **raw_payload: Any,
) -> CompletedWorkEntry:
    """Build the compact Completed Work Index row from an allowlisted payload."""

    safe_metadata = _sanitize_mapping(metadata or {}, include_raw_transcript)
    if include_raw_transcript:
        safe_metadata.update(raw_payload)
    task_id = _required_text(task_id, "task_id")
    run_id = _required_text(run_id, "run_id")
    files = _file_tuple(files_changed)
    symbols = string_tuple(symbols_affected)
    rejected = _json_item_tuple(approaches_rejected)
    pointers = _mapping_tuple(follow_up_pointers)
    verification = _sanitize_mapping(verification_results or {}, include_raw_transcript)
    offsets = _sanitize_mapping(source_event_offsets or {}, include_raw_transcript)
    key = idempotency_key or _idempotency_key(
        {
            "host_id": host_id,
            "repo_id": repo_id,
            "task_id": task_id,
            "run_id": run_id,
            "files_changed": files,
            "symbols_affected": symbols,
            "approach_taken": approach_taken,
            "approaches_rejected": rejected,
            "verification_results": verification,
            "follow_up_pointers": pointers,
            "trace_id": trace_id,
            "source_event_offsets": offsets,
        }
    )
    return CompletedWorkEntry(
        work_id=work_id or _work_id(key),
        idempotency_key=key,
        host_id=host_id,
        repo_id=repo_id,
        task_id=task_id,
        run_id=run_id,
        completed_at=completed_at or utc_now_iso(),
        files_changed=files,
        symbols_affected=symbols,
        approach_taken=str(approach_taken or ""),
        approaches_rejected=rejected,
        verification_results=verification,
        follow_up_pointers=pointers,
        trace_id=trace_id,
        source_event_offsets=offsets,
        metadata=safe_metadata,
    )


def record_completed_work_index(store: Any, **payload: Any) -> CompletedWorkRecordResult:
    """Record completed work without letting fumemory/store outages fail the run."""

    entry = build_completed_work_entry(**payload)
    try:
        saved = store.record_completed_work(entry)
    except Exception as exc:  # run completion must not depend on fumemory health
        return CompletedWorkRecordResult(
            stored=False,
            entry=None,
            idempotency_key=entry.idempotency_key,
            degraded_reason="fumemory_unavailable",
            error_message=str(exc),
        )
    return CompletedWorkRecordResult(
        stored=True,
        entry=saved,
        idempotency_key=saved.idempotency_key,
    )


def normalize_completed_work_file_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lower()


def _idempotency_key(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"completed_work:{digest}"


def _work_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"cwi_{digest[:24]}"


def _sanitize_mapping(
    value: dict[str, Any],
    include_raw_transcript: bool,
) -> dict[str, Any]:
    if include_raw_transcript:
        return dict(value)
    return {
        str(key): _sanitize_value(item)
        for key, item in dict(value).items()
        if str(key).lower() not in RAW_TRANSCRIPT_KEYS
    }


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_mapping(value, include_raw_transcript=False)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in value)
    return value


def _file_tuple(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in json_tuple(value):
        text = str(item or "").strip().replace("\\", "/")
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _json_item_tuple(value: Any) -> tuple[Any, ...]:
    out: list[Any] = []
    for item in json_tuple(value):
        if isinstance(item, dict):
            normalized: Any = _sanitize_mapping(item, include_raw_transcript=False)
        else:
            normalized = str(item)
        if normalized and normalized not in out:
            out.append(normalized)
    return tuple(out)


def _mapping_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for item in json_tuple(value):
        if not isinstance(item, dict):
            continue
        normalized = _sanitize_mapping(item, include_raw_transcript=False)
        if normalized not in out:
            out.append(normalized)
    return tuple(out)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text
