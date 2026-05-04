"""Passive context-health heuristics for Slice 7A."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from code_index.openclaw_context.models import ContextHealthEvent
from code_index.openclaw_context.models import HostContextMetrics


WARNING_TOKEN_FLOOR = 65_000
HANDOFF_PREPARE_TOKENS = 75_000
FRESH_SESSION_TOKENS = 80_000


@dataclass(frozen=True)
class ContextHealthInputs:
    metrics: HostContextMetrics
    manifest_source_hashes: dict[str, str] | None = None
    current_source_hashes: dict[str, str] | None = None
    required_pointer_ids: tuple[str, ...] = ()
    contradiction_signals: tuple[dict[str, Any], ...] = ()
    drift_signals: tuple[dict[str, Any], ...] = ()
    context_manager_handoff_present: bool = False
    budget_tokens: int = FRESH_SESSION_TOKENS


def evaluate_context_health(
    inputs: ContextHealthInputs,
    *,
    store: Any | None = None,
) -> tuple[ContextHealthEvent, ...]:
    metrics = inputs.metrics
    events: list[ContextHealthEvent] = []

    if WARNING_TOKEN_FLOOR <= metrics.estimated_tokens < HANDOFF_PREPARE_TOKENS:
        events.append(
            _event(
                metrics,
                event_kind="token_pressure",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"threshold": WARNING_TOKEN_FLOOR},
                store=store,
            )
        )
    elif HANDOFF_PREPARE_TOKENS <= metrics.estimated_tokens < FRESH_SESSION_TOKENS:
        events.append(
            _event(
                metrics,
                event_kind="handoff_prepare",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"threshold": HANDOFF_PREPARE_TOKENS},
                store=store,
            )
        )
    elif metrics.estimated_tokens >= FRESH_SESSION_TOKENS:
        events.append(
            _event(
                metrics,
                event_kind="fresh_session_recommended",
                severity="critical",
                budget_tokens=inputs.budget_tokens,
                details={"threshold": FRESH_SESSION_TOKENS},
                store=store,
            )
        )

    mismatched = _source_hash_mismatches(
        inputs.manifest_source_hashes or {},
        inputs.current_source_hashes or {},
    )
    if mismatched:
        events.append(
            _event(
                metrics,
                event_kind="stale_context",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"mismatched_pointer_ids": mismatched},
                store=store,
            )
        )

    missing_required = sorted(
        set(inputs.required_pointer_ids).difference(metrics.loaded_pointer_ids)
    )
    if missing_required:
        events.append(
            _event(
                metrics,
                event_kind="missing_required_instructions",
                severity="critical",
                budget_tokens=inputs.budget_tokens,
                details={"missing_pointer_ids": missing_required},
                store=store,
            )
        )

    duplicates = _duplicates(metrics.loaded_pointer_ids)
    if duplicates:
        events.append(
            _event(
                metrics,
                event_kind="duplicate_context",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"duplicate_pointer_ids": duplicates},
                store=store,
            )
        )

    repeated_approaches = _duplicates(metrics.approach_history)
    if repeated_approaches:
        events.append(
            _event(
                metrics,
                event_kind="repeated_failed_approach",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"approaches": repeated_approaches},
                store=store,
            )
        )

    if inputs.contradiction_signals:
        events.append(
            _event(
                metrics,
                event_kind="contradiction",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={
                    "signals": [
                        dict(signal) for signal in inputs.contradiction_signals
                    ],
                    "passive": True,
                },
                store=store,
            )
        )

    if inputs.drift_signals:
        drift_details = {
            "signals": [dict(signal) for signal in inputs.drift_signals],
            "passive": True,
        }
        events.append(
            _event(
                metrics,
                event_kind="drift",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details=drift_details,
                store=store,
            )
        )
        events.append(
            _event(
                metrics,
                event_kind="correction_needed",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={
                    **drift_details,
                    "source_event_kind": "drift",
                    "enforced": False,
                    "invoked_llm": False,
                },
                store=store,
            )
        )

    if (
        metrics.estimated_tokens >= HANDOFF_PREPARE_TOKENS
        and _has_pending_edit_claim(metrics.active_claims)
    ):
        events.append(
            _event(
                metrics,
                event_kind="pending_edit_under_pressure",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"active_claims": [dict(claim) for claim in metrics.active_claims]},
                store=store,
            )
        )

    if metrics.provider_compaction_signals and not inputs.context_manager_handoff_present:
        events.append(
            _event(
                metrics,
                event_kind="provider_compaction_without_handoff",
                severity="critical",
                budget_tokens=inputs.budget_tokens,
                details={
                    "signals": list(metrics.provider_compaction_signals),
                    "degraded_fallback": True,
                },
                store=store,
            )
        )

    if metrics.degraded_reasons:
        events.append(
            _event(
                metrics,
                event_kind="context_manager_degraded",
                severity="warning",
                budget_tokens=inputs.budget_tokens,
                details={"reasons": list(metrics.degraded_reasons)},
                store=store,
            )
        )

    return tuple(events)


def _event(
    metrics: HostContextMetrics,
    *,
    event_kind: str,
    severity: str,
    budget_tokens: int,
    details: dict[str, Any],
    store: Any | None,
) -> ContextHealthEvent:
    if store is not None:
        return store.record_health_event(
            host_id=metrics.host_id,
            run_id=metrics.run_id,
            agent_id=metrics.agent_id,
            task_id=metrics.task_id,
            event_kind=event_kind,
            severity=severity,
            observed_tokens=metrics.estimated_tokens,
            budget_tokens=budget_tokens,
            details=details,
        )
    from code_index.openclaw_context.store import _id
    from code_index.openclaw_context.models import canonical_json
    from code_index.openclaw_context.models import utc_now_iso

    return ContextHealthEvent(
        event_id=_id(
            "che",
            canonical_json(
                {
                    "host_id": metrics.host_id,
                    "run_id": metrics.run_id,
                    "event_kind": event_kind,
                    "severity": severity,
                    "details": details,
                }
            ),
        ),
        host_id=metrics.host_id,
        run_id=metrics.run_id,
        agent_id=metrics.agent_id,
        task_id=metrics.task_id,
        event_kind=event_kind,
        severity=severity,
        observed_tokens=metrics.estimated_tokens,
        budget_tokens=budget_tokens,
        details=details,
        created_at=utc_now_iso(),
    )


def _source_hash_mismatches(
    manifest_hashes: dict[str, str],
    current_hashes: dict[str, str],
) -> list[str]:
    mismatched: list[str] = []
    for pointer_id, expected in manifest_hashes.items():
        current = current_hashes.get(pointer_id)
        if current is not None and current != expected:
            mismatched.append(pointer_id)
    return sorted(mismatched)


def _duplicates(values: tuple[str, ...]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _has_pending_edit_claim(claims: tuple[dict[str, Any], ...]) -> bool:
    for claim in claims:
        mode = str(claim.get("mode") or "").strip().lower()
        if mode in {"edit", "exclusive"}:
            return True
    return False
