from __future__ import annotations

from pathlib import Path
from typing import Any

from code_index.openclaw_context.health import ContextHealthInputs
from code_index.openclaw_context.live_cma import CMAOrchestrator
from code_index.openclaw_context.live_cma import StubLLMRunner
from code_index.openclaw_context.models import CMAInvocationRecord
from code_index.openclaw_context.models import HostContextMetrics
from code_index.openclaw_context.models import utc_now_iso
from code_index.openclaw_context.store import SQLiteContextStore


class SequenceRunner(StubLLMRunner):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__({})
        self.responses = list(responses)

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
        return self.responses.pop(0)


def _inputs(*, tokens: int = 70_000, run_id: str = "run-1") -> ContextHealthInputs:
    return ContextHealthInputs(
        metrics=HostContextMetrics(
            host_id="host-a",
            run_id=run_id,
            task_id="task-1",
            agent_id="agent-1",
            estimated_tokens=tokens,
            loaded_files=("pkg/service.py",),
        )
    )


def test_quality_gate_zero_tests_invokes_tier_one_kimi(tmp_path: Path) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        runner = StubLLMRunner({"decision_kind": "pass", "summary": "continue"})
        orchestrator = CMAOrchestrator(
            store=store,
            runners={1: runner},
            cooldown_seconds=0,
            dedup_window_seconds=0,
        )

        record = orchestrator.maybe_invoke(
            _inputs(tokens=12_000),
            agent_state={
                "run_id": "run-1",
                "task_id": "task-1",
                "agent_id": "agent-1",
                "task_complexity": "complex",
                "test_run_count": 0,
            },
        )

        assert record is not None
        assert record.tier == 1
        assert record.model_id == "kimi-k2.6"
        assert record.trigger_event_kind == "quality_gate_zero_test_runs"
        assert runner.calls[0]["model_id"] == "kimi-k2.6"
    finally:
        store.close()


def test_escalate_true_invokes_next_tier_and_marks_prior_escalated(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        tier1 = SequenceRunner(
            [{"decision_kind": "flag", "summary": "needs review", "escalate": True}]
        )
        tier2 = SequenceRunner(
            [{"decision_kind": "pass", "summary": "reviewed", "escalate": False}]
        )
        orchestrator = CMAOrchestrator(
            store=store,
            runners={1: tier1, 2: tier2},
            cooldown_seconds=0,
            dedup_window_seconds=0,
        )

        record = orchestrator.maybe_invoke(_inputs(tokens=70_000))

        assert record is not None
        assert record.tier == 2
        assert record.model_id == "claude-opus"
        invocations = store.list_cma_invocations(run_id="run-1")
        assert [item.status for item in invocations] == ["escalated", "completed"]
        assert [tier1.calls[0]["model_id"], tier2.calls[0]["model_id"]] == [
            "kimi-k2.6",
            "claude-opus",
        ]
    finally:
        store.close()


def test_live_cma_correction_persists_pointer_and_enforced_health_event(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        runner = StubLLMRunner(
            {
                "decision_kind": "correct",
                "summary": "Return to the acceptance criteria before editing.",
                "escalate": False,
            }
        )
        orchestrator = CMAOrchestrator(
            store=store,
            runners={3: runner},
            cooldown_seconds=0,
            dedup_window_seconds=0,
        )

        record = orchestrator.maybe_invoke(
            _inputs(tokens=12_000),
            agent_state={
                "run_id": "run-1",
                "task_id": "task-1",
                "agent_id": "agent-1",
                "acceptance_criteria": ["preserve retries"],
                "last_tool_calls": ["opened docs", "edited README", "reported done"],
            },
        )

        assert record is not None
        assert record.tier == 3
        assert record.correction_pointer_ids
        pointer = store.get_context_pointer(record.correction_pointer_ids[0])
        assert pointer is not None
        assert pointer.pointer_kind == "correction"
        events = store.list_context_health_events(run_id="run-1")
        enforced = [
            event
            for event in events
            if event.details.get("invoked_llm") is True
            and event.details.get("enforced") is True
        ]
        assert enforced
        assert enforced[0].details["pointer_id"] == pointer.pointer_id
    finally:
        store.close()


def test_live_cma_concurrency_guard_skips_new_invocation(tmp_path: Path) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        store.record_cma_invocation(
            CMAInvocationRecord(
                invocation_id="cma_active",
                run_id="run-active",
                task_id="task-active",
                trigger_event_kind="token_pressure",
                tier=1,
                model_id="kimi-k2.6",
                status="invoked",
                created_at=utc_now_iso(),
            )
        )
        runner = StubLLMRunner({"decision_kind": "pass"})
        orchestrator = CMAOrchestrator(
            store=store,
            runners={1: runner},
            max_concurrent=1,
            cooldown_seconds=0,
            dedup_window_seconds=0,
        )

        record = orchestrator.maybe_invoke(_inputs(tokens=70_000, run_id="run-2"))

        assert record is not None
        assert record.status == "skipped_budget"
        assert record.rationale == "concurrent_cap_reached"
        assert runner.calls == []
    finally:
        store.close()
