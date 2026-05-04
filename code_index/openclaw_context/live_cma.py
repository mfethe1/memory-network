"""Live Context Manager Agent orchestration for OpenClaw Milestone 2."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_context.health import ContextHealthInputs
from code_index.openclaw_context.health import evaluate_context_health
from code_index.openclaw_context.models import CMAInvocationRecord
from code_index.openclaw_context.models import ContextHealthEvent
from code_index.openclaw_context.models import canonical_json
from code_index.openclaw_context.models import utc_now_iso
from code_index.openclaw_context.policy import detect_quality_gate_flags
from code_index.openclaw_context.store import SQLiteContextStore


DEFAULT_MAX_CONCURRENT = 5
DEFAULT_COOLDOWN_SECONDS = 90.0
DEFAULT_DEDUP_WINDOW_SECONDS = 30.0
DEFAULT_MAX_ESCALATION_HOPS = 2

TIER_MODELS: dict[int, str] = {
    1: "kimi-k2.6",
    2: "claude-opus",
    3: "gpt-5.5",
}

TIER_2_TRIGGERS = frozenset(
    {
        "quality_gate_missing_impact_before_edit",
        "quality_gate_premature_done_without_verification",
        "dependency_review",
        "conflicting_instructions",
    }
)
TIER_3_TRIGGERS = frozenset(
    {
        "quality_gate_goal_drift",
        "goal_drift",
        "repeated_handoff_failure",
        "cross_host_conflict",
    }
)
CORRECTION_DECISIONS = frozenset({"flag", "block", "handoff", "correct"})


class LLMRunner(ABC):
    """Injectable live-LLM call boundary."""

    @abstractmethod
    def invoke(
        self,
        *,
        prompt: str,
        model_id: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Return a JSON-like CMA decision payload."""


class StubLLMRunner(LLMRunner):
    """Deterministic runner for tests and dry-run smoke checks."""

    def __init__(self, response: Mapping[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = dict(
            response
            or {
                "escalate": False,
                "decision_kind": "pass",
                "summary": "stub pass",
                "confidence": 1.0,
            }
        )

    def invoke(
        self,
        *,
        prompt: str,
        model_id: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "prompt": prompt,
                "model_id": model_id,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return dict(self.response)


class CommandLLMRunner(LLMRunner):
    """Runs a configured local command and parses its JSON decision output."""

    def __init__(self, command_template: str, *, timeout_seconds: float = 120.0) -> None:
        self.command_template = command_template
        self.timeout_seconds = timeout_seconds

    def invoke(
        self,
        *,
        prompt: str,
        model_id: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="openclaw-cma-") as temp_dir:
            prompt_path = Path(temp_dir) / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            command = self.command_template.format(
                model_id=model_id,
                max_tokens=max_tokens,
                temperature=temperature,
                prompt_file=str(prompt_path),
            )
            completed = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        return _decision_from_stdout(completed.stdout)


@dataclass(frozen=True)
class CMAOrchestrator:
    """Live CMA layer with tiering, budget guardrails, and correction pointers."""

    store: SQLiteContextStore
    runners: Mapping[int, LLMRunner]
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS
    max_escalation_hops: int = DEFAULT_MAX_ESCALATION_HOPS

    def maybe_invoke(
        self,
        inputs: ContextHealthInputs,
        *,
        agent_state: Mapping[str, Any] | None = None,
    ) -> CMAInvocationRecord | None:
        trigger = self._select_trigger(inputs, agent_state)
        if trigger is None:
            return None

        metrics = inputs.metrics
        trigger_event_kind = trigger["event_kind"]
        gated = self._apply_guardrails(
            run_id=metrics.run_id,
            task_id=metrics.task_id,
            trigger_event_kind=trigger_event_kind,
            observed_tokens=metrics.estimated_tokens,
            budget_tokens=inputs.budget_tokens,
        )
        if gated is not None:
            return gated

        prompt = self._build_prompt(inputs, agent_state, trigger_event_kind)
        tier = self._select_tier(trigger_event_kind)
        record = self._invoke_tier(
            run_id=metrics.run_id,
            task_id=metrics.task_id,
            trigger_event_kind=trigger_event_kind,
            tier=tier,
            prompt=prompt,
            observed_tokens=metrics.estimated_tokens,
            budget_tokens=inputs.budget_tokens,
        )
        record = self._escalate_if_requested(
            record,
            prompt=prompt,
            observed_tokens=metrics.estimated_tokens,
        )
        if record.decision_kind in CORRECTION_DECISIONS and record.status == "completed":
            record = self._inject_correction(record, inputs)
        return record

    def _select_trigger(
        self,
        inputs: ContextHealthInputs,
        agent_state: Mapping[str, Any] | None,
    ) -> dict[str, str] | None:
        if agent_state is not None:
            flags = detect_quality_gate_flags(dict(agent_state))
            for name in (
                "goal_drift",
                "premature_done_without_verification",
                "missing_impact_before_edit",
                "zero_test_runs",
                "repeated_approach",
                "correction_needed",
            ):
                for flag in flags:
                    if flag.flag_kind == name:
                        event_kind = (
                            "correction_needed"
                            if name == "correction_needed"
                            else f"quality_gate_{name}"
                        )
                        return {"event_kind": event_kind, "severity": flag.severity}

        events = evaluate_context_health(inputs, store=None)
        for severity in ("critical", "warning"):
            for event in events:
                if event.severity == severity:
                    return {
                        "event_kind": event.event_kind,
                        "severity": event.severity,
                    }
        return None

    def _select_tier(self, trigger_event_kind: str) -> int:
        if trigger_event_kind in TIER_3_TRIGGERS:
            return 3
        if trigger_event_kind in TIER_2_TRIGGERS:
            return 2
        return 1

    def _apply_guardrails(
        self,
        *,
        run_id: str,
        task_id: str,
        trigger_event_kind: str,
        observed_tokens: int,
        budget_tokens: int,
    ) -> CMAInvocationRecord | None:
        if self.store.count_active_cma_invocations() >= self.max_concurrent:
            return self._skipped_record(
                run_id=run_id,
                task_id=task_id,
                trigger_event_kind=trigger_event_kind,
                status="skipped_budget",
                rationale="concurrent_cap_reached",
                observed_tokens=observed_tokens,
                budget_tokens=budget_tokens,
            )

        last = self.store.last_cma_invocation_for_run(run_id)
        if last is not None:
            elapsed = _seconds_since(last.created_at)
            if elapsed is not None and elapsed < self.cooldown_seconds:
                return self._skipped_record(
                    run_id=run_id,
                    task_id=task_id,
                    trigger_event_kind=trigger_event_kind,
                    status="skipped_cooldown",
                    rationale="run_cooldown_active",
                    observed_tokens=observed_tokens,
                    budget_tokens=budget_tokens,
                )

        for record in reversed(self.store.list_cma_invocations(run_id=run_id)):
            if record.status.startswith("skipped_"):
                continue
            if (
                record.trigger_event_kind == trigger_event_kind
                and record.observed_tokens == observed_tokens
            ):
                elapsed = _seconds_since(record.created_at)
                if elapsed is not None and elapsed < self.dedup_window_seconds:
                    return self._skipped_record(
                        run_id=run_id,
                        task_id=task_id,
                        trigger_event_kind=trigger_event_kind,
                        status="skipped_dedup",
                        rationale="identical_trigger_recent",
                        observed_tokens=observed_tokens,
                        budget_tokens=budget_tokens,
                    )
        return None

    def _invoke_tier(
        self,
        *,
        run_id: str,
        task_id: str,
        trigger_event_kind: str,
        tier: int,
        prompt: str,
        observed_tokens: int,
        budget_tokens: int,
    ) -> CMAInvocationRecord:
        model_id = TIER_MODELS[tier]
        runner = self.runners.get(tier)
        if runner is None:
            return self._skipped_record(
                run_id=run_id,
                task_id=task_id,
                trigger_event_kind=trigger_event_kind,
                tier=tier,
                model_id=model_id,
                status="skipped_budget",
                rationale="no_runner_for_tier",
                observed_tokens=observed_tokens,
                budget_tokens=budget_tokens,
            )

        record = CMAInvocationRecord(
            invocation_id=_invocation_id(run_id, trigger_event_kind, tier),
            run_id=run_id,
            task_id=task_id,
            trigger_event_kind=trigger_event_kind,
            tier=tier,
            model_id=model_id,
            status="invoked",
            observed_tokens=observed_tokens,
            budget_tokens=budget_tokens,
            created_at=utc_now_iso(),
        )
        record = self.store.record_cma_invocation(record)
        try:
            raw = runner.invoke(
                prompt=prompt,
                model_id=model_id,
                max_tokens=1024,
                temperature=0.0,
            )
        except Exception as exc:
            return self.store.record_cma_invocation(
                replace(record, status="error", rationale=str(exc), escalate=False)
            )

        return self.store.record_cma_invocation(
            replace(
                record,
                status="completed",
                decision_kind=_decision_kind(raw),
                rationale=str(raw.get("summary") or raw.get("rationale") or ""),
                escalate=bool(raw.get("escalate", False)),
            )
        )

    def _escalate_if_requested(
        self,
        record: CMAInvocationRecord,
        *,
        prompt: str,
        observed_tokens: int,
    ) -> CMAInvocationRecord:
        hops = 0
        current = record
        while current.status == "completed" and current.escalate:
            if current.tier >= max(TIER_MODELS) or hops >= self.max_escalation_hops:
                return self.store.record_cma_invocation(
                    replace(
                        current,
                        status="rejected",
                        rationale="max_escalation_tiers_reached",
                        escalate=False,
                    )
                )
            self.store.record_cma_invocation(replace(current, status="escalated"))
            current = self._invoke_tier(
                run_id=current.run_id,
                task_id=current.task_id,
                trigger_event_kind=current.trigger_event_kind,
                tier=current.tier + 1,
                prompt=prompt,
                observed_tokens=observed_tokens,
                budget_tokens=current.budget_tokens,
            )
            hops += 1
        return current

    def _inject_correction(
        self,
        record: CMAInvocationRecord,
        inputs: ContextHealthInputs,
    ) -> CMAInvocationRecord:
        pointer = self.store.upsert_context_pointer(
            source_uri=f"cma://{record.run_id}/{record.invocation_id}",
            source_kind="decision",
            pointer_kind="correction",
            content_hash=hashlib.sha256(
                canonical_json(record.to_dict()).encode("utf-8")
            ).hexdigest(),
            locator={
                "invocation_id": record.invocation_id,
                "trigger_event_kind": record.trigger_event_kind,
                "decision_kind": record.decision_kind,
            },
            summary=record.rationale or f"Live CMA decision: {record.decision_kind}",
            sensitivity="repo",
            host_id=inputs.metrics.host_id,
            target_symbols=tuple(inputs.metrics.loaded_files) or ("*",),
            tags=("live_cma", record.trigger_event_kind, record.decision_kind or ""),
        )
        self.store.record_health_event(
            host_id=inputs.metrics.host_id,
            run_id=record.run_id,
            agent_id=inputs.metrics.agent_id,
            task_id=record.task_id,
            event_kind="correction_needed",
            severity="warning",
            observed_tokens=record.observed_tokens,
            budget_tokens=record.budget_tokens,
            details={
                "invoked_llm": True,
                "enforced": True,
                "source_event_kind": record.trigger_event_kind,
                "decision_kind": record.decision_kind,
                "pointer_id": pointer.pointer_id,
                "model_id": record.model_id,
                "tier": record.tier,
            },
        )
        return self.store.record_cma_invocation(
            replace(record, correction_pointer_ids=(pointer.pointer_id,))
        )

    def _build_prompt(
        self,
        inputs: ContextHealthInputs,
        agent_state: Mapping[str, Any] | None,
        trigger_event_kind: str,
    ) -> str:
        return "\n".join(
            [
                "You are the OpenClaw Context Manager Agent.",
                "Return exactly one JSON object with keys: escalate, decision_kind, summary, confidence.",
                f"Trigger: {trigger_event_kind}",
                "Host metrics:",
                json.dumps(inputs.metrics.to_dict(), sort_keys=True, default=str),
                "Agent state:",
                json.dumps(dict(agent_state or {}), sort_keys=True, default=str),
            ]
        )

    def _skipped_record(
        self,
        *,
        run_id: str,
        task_id: str,
        trigger_event_kind: str,
        status: str,
        rationale: str,
        observed_tokens: int,
        budget_tokens: int,
        tier: int = 1,
        model_id: str | None = None,
    ) -> CMAInvocationRecord:
        return self.store.record_cma_invocation(
            CMAInvocationRecord(
                invocation_id=_invocation_id(run_id, trigger_event_kind, tier, status),
                run_id=run_id,
                task_id=task_id,
                trigger_event_kind=trigger_event_kind,
                tier=tier,
                model_id=model_id or TIER_MODELS[tier],
                status=status,
                rationale=rationale,
                observed_tokens=observed_tokens,
                budget_tokens=budget_tokens,
                created_at=utc_now_iso(),
            )
        )


def evaluate_and_maybe_invoke_cma(
    inputs: ContextHealthInputs,
    *,
    store: SQLiteContextStore,
    orchestrator: CMAOrchestrator | None = None,
    agent_state: Mapping[str, Any] | None = None,
) -> tuple[tuple[ContextHealthEvent, ...], CMAInvocationRecord | None]:
    """Persist passive health events, then optionally invoke the live CMA."""

    events = evaluate_context_health(inputs, store=store)
    if orchestrator is None:
        return events, None
    return events, orchestrator.maybe_invoke(inputs, agent_state=agent_state)


def _decision_kind(payload: Mapping[str, Any]) -> str:
    decision = str(payload.get("decision_kind") or payload.get("decision") or "pass")
    decision = decision.strip().lower()
    if decision not in {"pass", "flag", "block", "handoff", "correct"}:
        return "pass"
    return decision


def _decision_from_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("CMA runner did not emit a JSON decision")


def _invocation_id(
    run_id: str,
    trigger_event_kind: str,
    tier: int,
    suffix: str = "",
) -> str:
    value = canonical_json(
        {
            "run_id": run_id,
            "trigger_event_kind": trigger_event_kind,
            "tier": tier,
            "suffix": suffix,
            "created_at": utc_now_iso(),
        }
    )
    return f"cma_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _seconds_since(iso_timestamp: str | None) -> float | None:
    parsed = _parse_iso(iso_timestamp)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
