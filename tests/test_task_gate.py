from __future__ import annotations

from typing import Any

from code_index import task_gate


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "agent_name": "Codex",
        "message": "Implement the selected graph task.",
        "provider": "custom",
        "selected_nodes": ["file:pkg/a.py"],
        "selected_paths": ["pkg/a.py"],
        "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
        "parent_run_id": "",
        "run_context": {"source": "test"},
        "blocked_by_run_ids": [],
        "slice": {"id": "slice-1"},
        "execution_strategy": "single",
        "swarm": {"enabled": False, "execution_strategy": "single"},
    }
    request.update(overrides)
    return request


def _draft() -> dict[str, Any]:
    return {
        "root": "E:/Projects/example",
        "context_policy": {
            "initial_budget_tokens": 1600,
            "runtime_retrieval": True,
        },
    }


def test_preflight_hash_subject_binds_execution_strategy():
    single = task_gate.preflight_hash_subject(request=_request(), draft=_draft())
    swarm = task_gate.preflight_hash_subject(
        request=_request(
            execution_strategy="swarm",
            swarm={
                "enabled": True,
                "execution_strategy": "swarm",
                "provider": "custom",
                "size": 2,
                "coordination": "parallel",
                "roles": [
                    {"role": "coordinator", "title": "Coordinator"},
                    {"role": "tester", "title": "Tester"},
                ],
            },
        ),
        draft=_draft(),
    )

    assert single != swarm


def test_preflight_hash_subject_binds_public_swarm_shape():
    size_two = task_gate.preflight_hash_subject(
        request=_request(
            execution_strategy="swarm",
            swarm={
                "enabled": True,
                "execution_strategy": "swarm",
                "provider": "custom",
                "size": 2,
                "coordination": "parallel",
                "roles": [
                    {"role": "coordinator", "title": "Coordinator"},
                    {"role": "tester", "title": "Tester"},
                ],
            },
        ),
        draft=_draft(),
    )
    size_three = task_gate.preflight_hash_subject(
        request=_request(
            execution_strategy="swarm",
            swarm={
                "enabled": True,
                "execution_strategy": "swarm",
                "provider": "custom",
                "size": 3,
                "coordination": "parallel",
                "roles": [
                    {"role": "coordinator", "title": "Coordinator"},
                    {"role": "implementer", "title": "Implementer"},
                    {"role": "tester", "title": "Tester"},
                ],
            },
        ),
        draft=_draft(),
    )

    assert size_two != size_three
