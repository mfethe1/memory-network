"""Task preflight gate helpers."""

from __future__ import annotations

from typing import Any

from code_index import agent_swarm


def preflight_hash_subject(
    *,
    request: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    public_swarm = agent_swarm.public_swarm_config(request.get("swarm")) or {
        "enabled": False,
        "execution_strategy": "single",
    }
    return {
        "request": {
            "agent_name": request.get("agent_name"),
            "message": request.get("message"),
            "provider": request.get("provider") or "",
            "selected_nodes": request.get("selected_nodes") or [],
            "selected_paths": request.get("selected_paths") or [],
            "node": request.get("node") or {},
            "parent_run_id": request.get("parent_run_id") or "",
            "run_context": request.get("run_context"),
            "blocked_by_run_ids": request.get("blocked_by_run_ids") or [],
            "slice": request.get("slice") or {},
            "execution_strategy": request.get("execution_strategy") or "single",
            "swarm": public_swarm,
        },
        "draft": {
            "root": draft.get("root"),
            "context_policy": draft.get("context_policy") or {},
        },
    }
