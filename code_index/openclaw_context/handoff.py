"""Passive handoff packet generation for fresh-session proposals."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from code_index.openclaw_context.health import FRESH_SESSION_TOKENS
from code_index.openclaw_context.models import HandoffPacket
from code_index.openclaw_context.models import HostContextMetrics
from code_index.openclaw_context.models import canonical_json


@dataclass(frozen=True)
class HandoffRequest:
    metrics: HostContextMetrics
    provider: str
    repo_root: str
    current_goal: str
    latest_state: str
    accepted_decisions: tuple[str, ...] = ()
    rejected_decisions: tuple[str, ...] = ()
    verification_state: dict[str, Any] | None = None
    unresolved_questions: tuple[str, ...] = ()
    required_pointers: tuple[str, ...] = ()
    omitted_context: tuple[dict[str, Any], ...] = ()
    source_offsets: dict[str, Any] | None = None
    trigger_kind: str = "token_pressure"
    critical_context_health: bool = False


def maybe_propose_handoff(store: Any, request: HandoffRequest) -> HandoffPacket | None:
    metrics = request.metrics
    if (
        metrics.estimated_tokens < FRESH_SESSION_TOKENS
        and not request.critical_context_health
    ):
        return None

    packet = _packet_json(request)
    packet_hash = hashlib.sha256(canonical_json(packet).encode("utf-8")).hexdigest()
    handoff_id = f"handoff_{packet_hash[:24]}"
    return store.upsert_handoff_packet(
        handoff_id=handoff_id,
        host_id=metrics.host_id,
        from_run_id=metrics.run_id,
        task_id=metrics.task_id,
        trigger_kind=request.trigger_kind,
        status="proposed",
        provider=request.provider,
        repo_root=request.repo_root,
        packet=packet,
        packet_hash=packet_hash,
    )


def _packet_json(request: HandoffRequest) -> dict[str, Any]:
    metrics = request.metrics
    return {
        "schema_version": 1,
        "current_goal": request.current_goal,
        "latest_state": request.latest_state,
        "accepted_decisions": list(request.accepted_decisions),
        "rejected_decisions": list(request.rejected_decisions),
        "active_claims": [dict(claim) for claim in metrics.active_claims],
        "verification_state": dict(request.verification_state or {}),
        "unresolved_questions": list(request.unresolved_questions),
        "required_pointers": list(request.required_pointers),
        "omitted_context": [dict(item) for item in request.omitted_context],
        "source_offsets": dict(request.source_offsets or {}),
        "host_id": metrics.host_id,
        "task_id": metrics.task_id,
        "run_id": metrics.run_id,
        "agent_id": metrics.agent_id,
        "provider": request.provider,
        "repo_root": request.repo_root,
        "observed_tokens": metrics.estimated_tokens,
        "trigger_kind": request.trigger_kind,
    }
