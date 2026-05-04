from __future__ import annotations

from pathlib import Path

from code_index.openclaw_context.handoff import HandoffRequest
from code_index.openclaw_context.handoff import maybe_propose_handoff
from code_index.openclaw_context.models import HostContextMetrics
from code_index.openclaw_context.store import SQLiteContextStore


def test_fake_run_at_80k_tokens_proposes_one_idempotent_handoff(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        metrics = HostContextMetrics(
            host_id="host-a",
            run_id="run-80k",
            task_id="task-80k",
            agent_id="agent-a",
            estimated_tokens=80_000,
            active_claims=({"file_path": "pkg/service.py", "mode": "edit"},),
            loaded_pointer_ids=("ptr-required",),
        )
        request = HandoffRequest(
            metrics=metrics,
            provider="codex",
            repo_root=r"E:\Projects\repo-a",
            current_goal="Finish passive context manager slice.",
            latest_state="Tests are being written first.",
            accepted_decisions=("Use local SQLite pointer store for M1.",),
            rejected_decisions=("Do not invoke live CMA models in M1.",),
            verification_state={"pytest": "not run yet"},
            unresolved_questions=("Confirm downstream controller wiring later.",),
            required_pointers=("ptr-required",),
            omitted_context=({"pointer_id": "ptr-soul", "reason": "auto_load_blocked"},),
            source_offsets={"transcript": 42},
        )

        first = maybe_propose_handoff(store, request)
        replay = maybe_propose_handoff(store, request)

        assert first is not None
        assert replay is not None
        assert replay.handoff_id == first.handoff_id
        assert replay.packet_hash == first.packet_hash
        assert first.status == "proposed"
        assert first.trigger_kind == "token_pressure"
        assert first.packet["current_goal"] == "Finish passive context manager slice."
        assert first.packet["active_claims"] == [
            {"file_path": "pkg/service.py", "mode": "edit"}
        ]
        assert first.packet["required_pointers"] == ["ptr-required"]
        assert [packet.handoff_id for packet in store.list_handoff_packets()] == [
            first.handoff_id
        ]
    finally:
        store.close()


def test_handoff_is_not_proposed_below_fresh_session_threshold(tmp_path: Path) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        metrics = HostContextMetrics(
            host_id="host-a",
            run_id="run-75k",
            task_id="task-75k",
            agent_id="agent-a",
            estimated_tokens=75_000,
        )

        packet = maybe_propose_handoff(
            store,
            HandoffRequest(
                metrics=metrics,
                provider="codex",
                repo_root=r"E:\Projects\repo-a",
                current_goal="Continue work.",
                latest_state="No critical context health yet.",
            ),
        )

        assert packet is None
        assert store.list_handoff_packets() == []
    finally:
        store.close()
