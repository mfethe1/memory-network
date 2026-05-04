"""Small JSON-friendly models for passive OpenClaw context management."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def json_tuple(value: Any) -> tuple[Any, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return (value,)
        if isinstance(parsed, list):
            return tuple(parsed)
        return (parsed,)
    return (value,)


def string_tuple(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in json_tuple(value):
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def mapping_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for item in json_tuple(value):
        if isinstance(item, dict):
            result.append(dict(item))
    return tuple(result)


@dataclass(frozen=True)
class ContextSource:
    source_id: str
    source_uri: str
    source_kind: str
    source_hash: str
    sensitivity: str = "repo"
    host_id: str | None = None
    repo_id: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_uri": self.source_uri,
            "source_kind": self.source_kind,
            "source_hash": self.source_hash,
            "sensitivity": self.sensitivity,
            "host_id": self.host_id,
            "repo_id": self.repo_id,
            "provider": self.provider,
            "metadata": dict(self.metadata or {}),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ContextPointer:
    pointer_id: str
    source_id: str
    source_uri: str
    source_kind: str
    pointer_kind: str
    content_hash: str
    locator: dict[str, Any]
    summary: str = ""
    tokens_estimate: int = 0
    sensitivity: str = "repo"
    host_id: str | None = None
    repo_id: str | None = None
    provider: str | None = None
    target_symbols: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    required: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pointer_id": self.pointer_id,
            "source_id": self.source_id,
            "source_uri": self.source_uri,
            "source_kind": self.source_kind,
            "pointer_kind": self.pointer_kind,
            "content_hash": self.content_hash,
            "locator": dict(self.locator),
            "summary": self.summary,
            "tokens_estimate": self.tokens_estimate,
            "sensitivity": self.sensitivity,
            "host_id": self.host_id,
            "repo_id": self.repo_id,
            "provider": self.provider,
            "target_symbols": list(self.target_symbols),
            "tags": list(self.tags),
            "required": self.required,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ContextHealthEvent:
    event_id: str
    run_id: str
    agent_id: str
    task_id: str
    event_kind: str
    severity: str
    observed_tokens: int
    budget_tokens: int
    details: dict[str, Any]
    host_id: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "host_id": self.host_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "event_kind": self.event_kind,
            "severity": self.severity,
            "observed_tokens": self.observed_tokens,
            "budget_tokens": self.budget_tokens,
            "details": dict(self.details),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class HostContextMetrics:
    host_id: str
    run_id: str
    task_id: str
    agent_id: str
    estimated_tokens: int = 0
    loaded_files: tuple[str, ...] = ()
    loaded_pointer_ids: tuple[str, ...] = ()
    file_hashes: dict[str, str] | None = None
    active_claims: tuple[dict[str, Any], ...] = ()
    recent_failures: tuple[str, ...] = ()
    tool_output_volume: int = 0
    provider_compaction_signals: tuple[str, ...] = ()
    approach_history: tuple[str, ...] = ()
    degraded_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "estimated_tokens": self.estimated_tokens,
            "loaded_files": list(self.loaded_files),
            "loaded_pointer_ids": list(self.loaded_pointer_ids),
            "file_hashes": dict(self.file_hashes or {}),
            "active_claims": [dict(claim) for claim in self.active_claims],
            "recent_failures": list(self.recent_failures),
            "tool_output_volume": self.tool_output_volume,
            "provider_compaction_signals": list(self.provider_compaction_signals),
            "approach_history": list(self.approach_history),
            "degraded_reasons": list(self.degraded_reasons),
        }


@dataclass(frozen=True)
class QualityGateFlag:
    flag_kind: str
    severity: str
    message: str
    passive: bool = True
    invoked_llm: bool = False
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "flag_kind": self.flag_kind,
            "severity": self.severity,
            "message": self.message,
            "passive": self.passive,
            "invoked_llm": self.invoked_llm,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class HoldDecision:
    status: str
    reason: str | None = None
    pointer_ids: tuple[str, ...] = ()
    task_id: str | None = None
    invoked_context_manager: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "pointer_ids": list(self.pointer_ids),
            "task_id": self.task_id,
            "invoked_context_manager": self.invoked_context_manager,
        }


@dataclass(frozen=True)
class HandoffPacket:
    handoff_id: str
    from_run_id: str
    task_id: str
    trigger_kind: str
    status: str
    packet: dict[str, Any]
    packet_hash: str
    host_id: str | None = None
    provider: str | None = None
    repo_root: str | None = None
    created_at: str | None = None
    consumed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "host_id": self.host_id,
            "from_run_id": self.from_run_id,
            "task_id": self.task_id,
            "trigger_kind": self.trigger_kind,
            "status": self.status,
            "provider": self.provider,
            "repo_root": self.repo_root,
            "packet": dict(self.packet),
            "packet_hash": self.packet_hash,
            "created_at": self.created_at,
            "consumed_at": self.consumed_at,
        }


@dataclass(frozen=True)
class CMAInvocationRecord:
    invocation_id: str
    run_id: str
    task_id: str
    trigger_event_kind: str
    tier: int
    model_id: str
    status: str
    decision_kind: str | None = None
    correction_pointer_ids: tuple[str, ...] = ()
    rationale: str = ""
    escalate: bool = False
    observed_tokens: int = 0
    budget_tokens: int = 0
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "trigger_event_kind": self.trigger_event_kind,
            "tier": self.tier,
            "model_id": self.model_id,
            "status": self.status,
            "decision_kind": self.decision_kind,
            "correction_pointer_ids": list(self.correction_pointer_ids),
            "rationale": self.rationale,
            "escalate": self.escalate,
            "observed_tokens": self.observed_tokens,
            "budget_tokens": self.budget_tokens,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ContextManifest:
    manifest_id: str
    status: str
    host_id: str
    repo_id: str
    task_id: str
    run_id: str
    provider: str
    route_scope: str = "local"
    pointer_ids: tuple[str, ...] = ()
    required_pointer_ids: tuple[str, ...] = ()
    load_order: tuple[str, ...] = ()
    omitted_context: tuple[dict[str, Any], ...] = ()
    token_budget: dict[str, Any] | None = None
    estimated_tokens: int = 0
    source_hashes: dict[str, str] | None = None
    peer_agent_states: tuple[dict[str, Any], ...] = ()
    expires_at: str | None = None
    signature_key_id: str | None = None
    signature: str | None = None
    signed_payload: str | None = None
    request_hash: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "status": self.status,
            "host_id": self.host_id,
            "repo_id": self.repo_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "route_scope": self.route_scope,
            "pointer_ids": list(self.pointer_ids),
            "required_pointer_ids": list(self.required_pointer_ids),
            "load_order": list(self.load_order),
            "omitted_context": [dict(item) for item in self.omitted_context],
            "token_budget": dict(self.token_budget or {}),
            "estimated_tokens": self.estimated_tokens,
            "source_hashes": dict(self.source_hashes or {}),
            "peer_agent_states": [dict(item) for item in self.peer_agent_states],
            "expires_at": self.expires_at,
            "signature_key_id": self.signature_key_id,
            "signature": self.signature,
            "signed_payload": self.signed_payload,
            "request_hash": self.request_hash,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "created_at": self.created_at,
        }
