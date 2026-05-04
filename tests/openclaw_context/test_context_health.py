from __future__ import annotations

from pathlib import Path

from code_index.openclaw_context.health import ContextHealthInputs
from code_index.openclaw_context.health import evaluate_context_health
from code_index.openclaw_context.models import HostContextMetrics
from code_index.openclaw_context.policy import detect_quality_gate_flags
from code_index.openclaw_context.store import SQLiteContextStore
from code_index.openclaw_hostd.context_probe import HostContextProbe


def test_fake_run_at_70k_tokens_records_warning_health_event(tmp_path: Path) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        metrics = HostContextMetrics(
            host_id="host-a",
            run_id="run-70k",
            task_id="task-70k",
            agent_id="agent-a",
            estimated_tokens=70_000,
        )

        events = evaluate_context_health(
            ContextHealthInputs(metrics=metrics),
            store=store,
        )

        assert [(event.event_kind, event.severity) for event in events] == [
            ("token_pressure", "warning")
        ]
        persisted = store.list_context_health_events(run_id="run-70k")
        assert [(event.event_kind, event.severity) for event in persisted] == [
            ("token_pressure", "warning")
        ]
    finally:
        store.close()


def test_source_hash_mismatch_and_compaction_emit_stale_and_critical_events(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        metrics = HostContextMetrics(
            host_id="host-a",
            run_id="run-rot",
            task_id="task-rot",
            agent_id="agent-a",
            estimated_tokens=20_000,
            loaded_pointer_ids=("ptr-a",),
            provider_compaction_signals=("provider_compacted",),
        )

        events = evaluate_context_health(
            ContextHealthInputs(
                metrics=metrics,
                manifest_source_hashes={"ptr-a": "sha256:old"},
                current_source_hashes={"ptr-a": "sha256:new"},
                context_manager_handoff_present=False,
            ),
            store=store,
        )

        by_kind = {event.event_kind: event for event in events}
        assert by_kind["stale_context"].severity == "warning"
        assert by_kind["provider_compaction_without_handoff"].severity == "critical"
        assert "ptr-a" in by_kind["stale_context"].details["mismatched_pointer_ids"]
    finally:
        store.close()


def test_quality_gate_patterns_emit_passive_flags_without_llm_invocation() -> None:
    flags = detect_quality_gate_flags(
        {
            "task_id": "task-complex",
            "run_id": "run-complex",
            "task_complexity": "complex",
            "test_run_count": 0,
            "impact_call_count": 0,
            "edited_symbols": ["pkg.service.handle"],
            "run_status": "done",
            "verification_state": "",
            "approach_history_json": '["patch timeout", "patch timeout"]',
            "acceptance_criteria": ["handle cancelled jobs", "preserve retries"],
            "last_tool_calls": [
                "opened README",
                "edited docs",
                "reported done",
            ],
        }
    )

    assert {flag.flag_kind for flag in flags} == {
        "zero_test_runs",
        "missing_impact_before_edit",
        "premature_done_without_verification",
        "repeated_approach",
        "goal_drift",
    }
    assert all(flag.passive is True for flag in flags)
    assert all(flag.invoked_llm is False for flag in flags)


class FailingStore:
    def list_context_pointers(self, **kwargs: object) -> list[object]:
        raise RuntimeError("fumemory unavailable")


def test_context_probe_degrades_when_fumemory_store_is_unavailable() -> None:
    probe = HostContextProbe(context_store=FailingStore())

    metrics = probe.collect_run_metrics(
        {
            "host_id": "host-a",
            "agent_id": "agent-a",
            "task_id": "task-a",
            "run_id": "run-a",
            "estimated_tokens": 1234,
            "active_files": ["pkg/service.py"],
            "loaded_context_handles": [{"pointer_id": "ptr-a"}],
        }
    )

    assert metrics.estimated_tokens == 1234
    assert metrics.loaded_pointer_ids == ("ptr-a",)
    assert metrics.degraded_reasons == ("fumemory_unavailable",)
