from __future__ import annotations

import json
from pathlib import Path

from code_index.openclaw_context.completed_work import record_completed_work_index
from code_index.openclaw_context.store import SQLiteContextStore


class UnavailableCompletedWorkStore:
    def record_completed_work(self, entry: object) -> object:
        raise RuntimeError("local fumemory store is unavailable")


def test_completed_work_index_records_and_queries_by_symbol_and_file(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        result = record_completed_work_index(
            store,
            host_id="host-a",
            repo_id="repo-a",
            task_id="task-7",
            run_id="run-7",
            files_changed=[
                "code_index/openclaw_context/completed_work.py",
                "tests/openclaw_context/test_completed_work.py",
            ],
            symbols_affected=[
                "code_index.openclaw_context.completed_work.CompletedWorkEntry",
                "code_index.openclaw_context.store.SQLiteContextStore",
            ],
            approach_taken="Keep a compact local index and expose lookup helpers.",
            approaches_rejected=[
                {
                    "approach": "Store raw Agent Run transcript text",
                    "reason": "Completed Work Index should keep compact pointers only.",
                }
            ],
            verification_results={
                "pytest": {
                    "status": "passed",
                    "node_ids": [
                        "tests/openclaw_context/test_completed_work.py::test_completed_work_index_records_and_queries_by_symbol_and_file"
                    ],
                }
            },
            follow_up_pointers=[
                {
                    "kind": "manifest",
                    "uri": "codeindex://manifest/run-7",
                }
            ],
            trace_id="trace-7",
        )

        by_symbol = store.list_completed_work_by_symbol(
            "code_index.openclaw_context.completed_work.CompletedWorkEntry"
        )
        by_file = store.list_completed_work_by_file(
            "code_index\\openclaw_context\\completed_work.py"
        )

        assert result.stored is True
        assert result.entry is not None
        assert by_symbol == [result.entry]
        assert by_file == [result.entry]
        assert by_symbol[0].approach_taken == (
            "Keep a compact local index and expose lookup helpers."
        )
        assert by_symbol[0].verification_results["pytest"]["status"] == "passed"
        assert by_symbol[0].follow_up_pointers == (
            {
                "kind": "manifest",
                "uri": "codeindex://manifest/run-7",
            },
        )
        assert by_symbol[0].trace_id == "trace-7"
    finally:
        store.close()


def test_completed_work_index_store_outage_does_not_block_run_completion() -> None:
    result = record_completed_work_index(
        UnavailableCompletedWorkStore(),
        task_id="task-outage",
        run_id="run-outage",
        files_changed=["pkg/service.py"],
        symbols_affected=["pkg.service.handle"],
        approach_taken="Keep the local completion path nonblocking.",
        verification_results={"status": "blocked"},
        trace_id="trace-outage",
    )

    assert result.stored is False
    assert result.entry is None
    assert result.idempotency_key is not None
    assert result.degraded_reason == "fumemory_unavailable"
    assert "unavailable" in (result.error_message or "")


def test_completed_work_index_dedupes_replay_and_omits_raw_transcript_by_default(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        payload = {
            "host_id": "host-a",
            "repo_id": "repo-a",
            "task_id": "task-replay",
            "run_id": "run-replay",
            "files_changed": ["pkg/service.py"],
            "symbols_affected": ["pkg.service.handle"],
            "approach_taken": "Patch the service boundary and keep retry policy.",
            "approaches_rejected": ["Move retry state into the caller"],
            "verification_results": {
                "pytest": {
                    "status": "passed",
                    "raw_transcript": "SECRET TRANSCRIPT BODY",
                },
            },
            "follow_up_pointers": [{"kind": "cma", "uri": "fumemory://cma/run-replay"}],
            "trace_id": "trace-replay",
            "raw_transcript": "SECRET TRANSCRIPT BODY",
        }

        first = record_completed_work_index(store, **payload)
        replay = record_completed_work_index(store, **payload)
        by_symbol = store.list_completed_work_by_symbol("pkg.service.handle")

        assert first.stored is True
        assert replay.stored is True
        assert first.entry is not None
        assert replay.entry is not None
        assert replay.entry.work_id == first.entry.work_id
        assert by_symbol == [replay.entry]
        assert "SECRET TRANSCRIPT BODY" not in json.dumps(replay.entry.to_dict())
        assert "raw_transcript" not in replay.entry.verification_results["pytest"]
    finally:
        store.close()
