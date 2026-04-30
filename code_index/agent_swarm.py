"""Agent Swarm planning helpers.

An Agent Swarm is an execution strategy for one Agent Task. The durable data
model stays the existing agent_runs/agent_events tables; the swarm projection is
stored in run metadata and child runs point back to the parent run id.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from code_index import agent_providers


MAX_SWARM_AGENTS = 6
DEFAULT_COMPLETION_POLICY = "all_children_terminal"
SWARM_PARENT_TERMINAL_STATUSES = {
    "review",
    "needs_review",
    "needs-review",
    "completed",
    "failed",
    "cancelled",
    "canceled",
}
SWARM_RESUME_STATUSES = {"queued", "ready"}


@dataclass(frozen=True)
class SwarmRole:
    role: str
    title: str
    responsibility: str
    claim_mode: str = "review"


DEFAULT_ROLES: tuple[SwarmRole, ...] = (
    SwarmRole(
        role="coordinator",
        title="Coordinator",
        responsibility=(
            "Plan the work, keep the team aligned, identify dependencies, and "
            "summarize decisions. Avoid file edits unless explicitly necessary."
        ),
        claim_mode="read",
    ),
    SwarmRole(
        role="implementer",
        title="Implementer",
        responsibility=(
            "Make the primary code changes for the task. Claim files before "
            "editing and avoid overwriting peer work."
        ),
        claim_mode="edit",
    ),
    SwarmRole(
        role="reviewer",
        title="Reviewer",
        responsibility=(
            "Review the proposed implementation, look for regressions, and "
            "record concrete findings or approval."
        ),
        claim_mode="read",
    ),
    SwarmRole(
        role="tester",
        title="Tester",
        responsibility=(
            "Run the most relevant verification, diagnose failures, and report "
            "the exact commands and outcomes."
        ),
        claim_mode="test",
    ),
)

ROLE_BY_ID = {role.role: role for role in DEFAULT_ROLES}


def is_swarm_strategy(value: Any) -> bool:
    text = str(value or "").strip().lower().replace("-", "_")
    return text in {"swarm", "agent_swarm"}


def normalize_swarm_config(
    payload: dict[str, Any],
    *,
    request_provider: str | None = None,
) -> dict[str, Any]:
    raw = payload.get("swarm") if isinstance(payload.get("swarm"), dict) else {}
    strategy = (
        payload.get("execution_strategy")
        or payload.get("strategy")
        or raw.get("execution_strategy")
        or raw.get("strategy")
    )
    enabled = bool(raw.get("enabled")) or is_swarm_strategy(strategy)
    if not enabled:
        return {"enabled": False, "execution_strategy": "single"}

    provider = agent_providers.normalize_provider_id(
        raw.get("provider")
        or payload.get("swarm_provider")
        or request_provider
        or "kimi"
    )
    agent_providers.require_provider(provider)
    size = _bounded_size(raw.get("size") or payload.get("swarm_size"))
    roles = _normalize_roles(raw.get("agents") or raw.get("roles"), size=size)
    return {
        "enabled": True,
        "execution_strategy": "swarm",
        "provider": provider,
        "size": len(roles),
        "roles": roles,
        "completion_policy": _completion_policy(raw.get("completion_policy")),
        "coordination": str(raw.get("coordination") or "parallel").strip().lower()
        or "parallel",
    }


def public_swarm_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not config or not config.get("enabled"):
        return None
    return {
        "enabled": True,
        "execution_strategy": "swarm",
        "provider": config.get("provider"),
        "size": config.get("size"),
        "coordination": config.get("coordination"),
        "completion_policy": config.get("completion_policy")
        or DEFAULT_COMPLETION_POLICY,
        "roles": list(config.get("roles") or []),
    }


def swarm_parent_metadata(
    metadata: dict[str, Any] | None,
    swarm: dict[str, Any],
) -> dict[str, Any]:
    """Return run metadata for the first-class Swarm Lead parent."""

    out = dict(metadata or {})
    public = public_swarm_config(swarm) or {}
    existing_swarm = out.get("swarm") if isinstance(out.get("swarm"), dict) else {}
    swarm_meta = dict(existing_swarm or {})
    swarm_meta.update(public)
    swarm_meta.update(
        {
            "role": "lead",
            "title": "Swarm Lead",
            "completion_policy": _completion_policy(
                swarm_meta.get("completion_policy") or swarm.get("completion_policy")
            ),
            "child_count": int(swarm.get("size") or len(swarm.get("roles") or [])),
        }
    )
    out["execution_strategy"] = "swarm"
    out["swarm"] = swarm_meta
    out.pop("parent_run_id", None)
    return out


def child_run_specs(
    request: dict[str, Any],
    *,
    parent_run_id: str,
    swarm: dict[str, Any],
) -> list[dict[str, Any]]:
    roles = list(swarm.get("roles") or [])
    provider = str(swarm.get("provider") or request.get("provider") or "kimi")
    specs: list[dict[str, Any]] = []
    for index, role in enumerate(roles, start=1):
        role_id = str(role.get("role") or f"agent-{index}")
        title = str(role.get("title") or role_id.title())
        agent_name = str(role.get("agent_name") or f"{_provider_name(provider)} {title}")
        metadata = dict(request.get("metadata") or {})
        metadata.update(
            {
                "execution_strategy": "swarm",
                "parent_run_id": parent_run_id,
                "provider": provider,
                "swarm": {
                    "parent_run_id": parent_run_id,
                    "role": role_id,
                    "title": title,
                    "index": index,
                    "size": len(roles),
                    "coordination": swarm.get("coordination") or "parallel",
                },
            }
        )
        specs.append(
            {
                "agent_name": agent_name,
                "provider": provider,
                "role": role,
                "prompt": _child_prompt(request, role=role, index=index, total=len(roles)),
                "metadata": metadata,
                "claim_mode": str(role.get("claim_mode") or "review"),
            }
        )
    return specs


def is_swarm_parent(run: dict[str, Any]) -> bool:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    if metadata.get("parent_run_id"):
        return False
    swarm = metadata.get("swarm") if isinstance(metadata.get("swarm"), dict) else {}
    return (
        is_swarm_strategy(metadata.get("execution_strategy"))
        and str(swarm.get("role") or "").strip().lower() == "lead"
    )


def reconcile_swarm_parent(conn, *, parent_run_id: str) -> dict[str, Any]:
    from code_index import agent_activity

    parent = agent_activity.get_run(conn, parent_run_id)
    if parent is None:
        raise ValueError(f"unknown parent run_id: {parent_run_id}")
    parent_status = str(parent.get("status") or "").strip().lower()
    result: dict[str, Any] = {
        "run_id": parent_run_id,
        "status": parent_status,
        "children": [],
        "changed": False,
    }
    if not is_swarm_parent(parent):
        result["reason"] = "not_swarm_parent"
        return result

    metadata = parent.get("metadata") if isinstance(parent.get("metadata"), dict) else {}
    swarm_meta = metadata.get("swarm") if isinstance(metadata.get("swarm"), dict) else {}
    children = agent_activity.child_runs(conn, parent_run_id=parent_run_id)
    result["children"] = children
    expected_child_count = _expected_child_count(swarm_meta)
    visible_child_count = len(children)
    if expected_child_count and visible_child_count < expected_child_count:
        result.update(
            {
                "reason": "incomplete_swarm",
                "expected_child_count": expected_child_count,
                "visible_child_count": visible_child_count,
            }
        )
        return result
    if not children or parent_status in SWARM_PARENT_TERMINAL_STATUSES:
        return result

    completion_policy = _completion_policy(swarm_meta.get("completion_policy"))
    if completion_policy != DEFAULT_COMPLETION_POLICY:
        result["completion_policy"] = completion_policy
        return result

    child_statuses = [str(child.get("status") or "").strip().lower() for child in children]
    all_stopped = all(status in agent_activity.STOPPED_STATUSES for status in child_statuses)
    if not all_stopped:
        return result

    failed_children = [
        {
            "run_id": child.get("run_id"),
            "agent_name": child.get("agent_name"),
            "status": child.get("status"),
            "role": ((child.get("metadata") or {}).get("swarm") or {}).get("role"),
        }
        for child in children
        if str(child.get("status") or "").strip().lower() == "failed"
    ]
    attention_required = bool(failed_children)
    payload = {
        "status": "review",
        "swarm": {
            "role": "lead",
            "completion_policy": completion_policy,
            "child_count": len(children),
            "expected_child_count": expected_child_count or len(children),
            "failed_child_count": len(failed_children),
            "requires_attention": attention_required,
            "failed_children": failed_children,
        },
    }
    message = (
        "Swarm Lead moved parent run to review after all child runs reached "
        "a stopped status."
    )
    if attention_required:
        message = (
            "Swarm Lead moved parent run to review with failed child runs "
            "requiring attention."
        )

    updated = agent_activity.end_run(conn, run_id=parent_run_id, status="review")
    _update_parent_attention_metadata(
        conn,
        parent=updated,
        failed_children=failed_children,
        completion_policy=completion_policy,
    )
    updated = agent_activity.get_run(conn, parent_run_id) or updated
    agent_activity.record_event(
        conn,
        run_id=parent_run_id,
        event_type="status",
        message=message,
        payload=payload,
    )
    return {
        "run_id": parent_run_id,
        "status": updated["status"],
        "children": children,
        "changed": True,
        "requires_attention": attention_required,
        "failed_child_count": len(failed_children),
        "expected_child_count": expected_child_count or len(children),
        "visible_child_count": visible_child_count,
    }


def materialize_swarm_children(conn, *, parent_run_id: str) -> dict[str, Any]:
    """Create queued child runs for an unblocked Swarm Lead that was deferred."""

    from code_index import agent_activity

    with _transaction(conn):
        parent = agent_activity.get_run(conn, parent_run_id)
        if parent is None:
            raise ValueError(f"unknown parent run_id: {parent_run_id}")
        parent_status = str(parent.get("status") or "").strip().lower()
        result: dict[str, Any] = {
            "action": "materialize_swarm_children",
            "run_id": parent_run_id,
            "status": parent_status,
            "children": [],
            "changed": False,
        }
        if not is_swarm_parent(parent):
            result["reason"] = "not_swarm_parent"
            return result
        if parent_status not in SWARM_RESUME_STATUSES:
            result["reason"] = "parent_not_ready"
            return result
        active_blockers = agent_activity.active_run_blockers(
            conn,
            run_id=parent_run_id,
        )
        if active_blockers:
            result["reason"] = "active_blockers"
            result["active_blockers"] = active_blockers
            return result
        metadata = parent.get("metadata") if isinstance(parent.get("metadata"), dict) else {}
        swarm_meta = metadata.get("swarm") if isinstance(metadata.get("swarm"), dict) else {}
        materialized_ids = _string_list(swarm_meta.get("materialized_child_run_ids"))
        if swarm_meta.get("children_materialized") or materialized_ids:
            result["reason"] = "children_already_materialized"
            result["materialized_child_run_ids"] = materialized_ids
            return result
        visible_children = agent_activity.child_runs(conn, parent_run_id=parent_run_id)
        if visible_children:
            result["reason"] = "children_already_exist"
            result["children"] = visible_children
            return result

        roles = _roles_for_resume(swarm_meta)
        if not roles:
            result["reason"] = "missing_swarm_roles"
            return result
        provider = str(swarm_meta.get("provider") or metadata.get("provider") or "kimi")
        swarm = {
            "enabled": True,
            "execution_strategy": "swarm",
            "provider": provider,
            "size": len(roles),
            "roles": roles,
            "coordination": swarm_meta.get("coordination") or "parallel",
            "completion_policy": _completion_policy(swarm_meta.get("completion_policy")),
        }
        request = {
            "message": parent.get("prompt") or "",
            "provider": provider,
            "selected_nodes": list(parent.get("selected_nodes") or []),
            "selected_paths": _string_list(metadata.get("selected_paths")),
            "node": metadata.get("node") if isinstance(metadata.get("node"), dict) else {},
            "metadata": metadata,
        }
        child_events: list[dict[str, Any]] = []
        child_runs: list[dict[str, Any]] = []
        for spec in child_run_specs(request, parent_run_id=parent_run_id, swarm=swarm):
            child_role = spec.get("role") if isinstance(spec.get("role"), dict) else {}
            child_run = agent_activity.start_run(
                conn,
                run_id=secrets.token_hex(16),
                agent_name=str(spec["agent_name"]),
                prompt=str(spec["prompt"]),
                selected_nodes=request["selected_nodes"],
                metadata=spec.get("metadata") or {},
                status="queued",
            )
            child_events.append(
                agent_activity.record_event(
                    conn,
                    run_id=child_run["run_id"],
                    event_type="task",
                    file_path=(
                        request["selected_paths"][0]
                        if request["selected_paths"]
                        else request["node"].get("path")
                    ),
                    message=str(spec["prompt"]),
                    payload={
                        "status": "queued",
                        "provider": spec.get("provider"),
                        "execution_strategy": "swarm_child",
                        "durable_execution_strategy": "swarm",
                        "parent_run_id": parent_run_id,
                        "swarm": (spec.get("metadata") or {}).get("swarm"),
                        "swarm_role": child_role,
                        "selected_nodes": request["selected_nodes"],
                        "selected_paths": request["selected_paths"],
                    },
                )
            )
            if request["selected_paths"]:
                agent_activity.claim_files(
                    conn,
                    run_id=child_run["run_id"],
                    file_paths=request["selected_paths"],
                    mode=str(spec.get("claim_mode") or "review"),
                    reason="Agent Swarm child selected file.",
                    metadata={
                        "source": "agent-swarm-resume",
                        "parent_run_id": parent_run_id,
                        "role": child_role.get("role"),
                    },
                )
            refreshed = agent_activity.get_run(conn, child_run["run_id"]) or child_run
            child_runs.append(refreshed)

        _mark_children_materialized(
            conn,
            parent=parent,
            child_runs=child_runs,
        )
        updated_parent = agent_activity.get_run(conn, parent_run_id)
        agent_activity.record_event(
            conn,
            run_id=parent_run_id,
            event_type="status",
            message=(
                "Run Orchestrator materialized Agent Swarm children after "
                "blockers resolved."
            ),
            payload={
                "status": "working",
                "swarm": {
                    "role": "lead",
                    "child_count": len(child_runs),
                    "completion_policy": swarm["completion_policy"],
                    "children_materialized": True,
                },
            },
        )
        return {
            "action": "materialize_swarm_children",
            "run_id": parent_run_id,
            "status": (updated_parent or {}).get("status") or "working",
            "children": child_runs,
            "child_events": child_events,
            "changed": True,
            "reason": "blocked_swarm_resumed",
            "expected_child_count": len(roles),
            "visible_child_count": len(child_runs),
        }


def _bounded_size(value: Any) -> int:
    try:
        size = int(value or 3)
    except (TypeError, ValueError):
        size = 3
    return max(1, min(MAX_SWARM_AGENTS, size))


def _completion_policy(value: Any) -> str:
    policy = str(value or DEFAULT_COMPLETION_POLICY).strip().lower().replace("-", "_")
    if policy != DEFAULT_COMPLETION_POLICY:
        return DEFAULT_COMPLETION_POLICY
    return policy


def _expected_child_count(swarm_meta: dict[str, Any]) -> int:
    for key in ("child_count",):
        try:
            count = int(swarm_meta.get(key) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            return count
    roles = swarm_meta.get("roles")
    if isinstance(roles, list) and roles:
        return len(roles)
    try:
        size = int(swarm_meta.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return max(0, size)


@contextmanager
def _transaction(conn):
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _roles_for_resume(swarm_meta: dict[str, Any]) -> list[dict[str, Any]]:
    roles = swarm_meta.get("roles")
    if isinstance(roles, list) and roles:
        return [
            dict(role)
            for role in roles
            if isinstance(role, dict) and str(role.get("role") or "").strip()
        ]
    expected = _expected_child_count(swarm_meta)
    if expected <= 0:
        return []
    roles: list[dict[str, Any]] = []
    for index in range(expected):
        if index < len(DEFAULT_ROLES):
            roles.append(_role_payload(DEFAULT_ROLES[index]))
        else:
            roles.append(_role_payload(_custom_role(f"agent_{index + 1}", index + 1)))
    return roles


def _mark_children_materialized(
    conn,
    *,
    parent: dict[str, Any],
    child_runs: list[dict[str, Any]],
) -> None:
    metadata = dict(parent.get("metadata") or {})
    swarm = metadata.get("swarm") if isinstance(metadata.get("swarm"), dict) else {}
    swarm_meta = dict(swarm or {})
    swarm_meta["children_materialized"] = True
    swarm_meta["materialized_child_count"] = len(child_runs)
    swarm_meta["materialized_child_run_ids"] = [
        child["run_id"] for child in child_runs if child.get("run_id")
    ]
    swarm_meta["children_materialized_at"] = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    )
    metadata["swarm"] = swarm_meta
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    conn.execute(
        """
        UPDATE agent_runs
           SET status = 'working',
               metadata_json = ?,
               updated_at = ?
         WHERE run_id = ?
        """,
        (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            now,
            parent["run_id"],
        ),
    )


def _update_parent_attention_metadata(
    conn,
    *,
    parent: dict[str, Any],
    failed_children: list[dict[str, Any]],
    completion_policy: str,
) -> None:
    import json
    from datetime import datetime, timezone

    metadata = dict(parent.get("metadata") or {})
    swarm = metadata.get("swarm") if isinstance(metadata.get("swarm"), dict) else {}
    swarm_meta = dict(swarm or {})
    swarm_meta["completion_policy"] = completion_policy
    swarm_meta["failed_child_count"] = len(failed_children)
    swarm_meta["requires_attention"] = bool(failed_children)
    if failed_children:
        swarm_meta["failed_children"] = failed_children
    else:
        swarm_meta.pop("failed_children", None)
    metadata["swarm"] = swarm_meta
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    conn.execute(
        """
        UPDATE agent_runs
           SET metadata_json = ?,
               updated_at = ?
         WHERE run_id = ?
        """,
        (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            now,
            parent["run_id"],
        ),
    )


def _normalize_roles(raw_roles: Any, *, size: int) -> list[dict[str, Any]]:
    items = raw_roles if isinstance(raw_roles, list) else []
    roles: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            role = ROLE_BY_ID.get(item.strip().lower())
            roles.append(_role_payload(role or _custom_role(item, index)))
        elif isinstance(item, dict):
            role_id = str(item.get("role") or item.get("id") or "").strip().lower()
            base = ROLE_BY_ID.get(role_id)
            payload = _role_payload(base or _custom_role(role_id or "agent", index))
            payload.update({key: value for key, value in item.items() if value is not None})
            roles.append(payload)
    if not roles:
        roles = [_role_payload(role) for role in DEFAULT_ROLES[:size]]
    return roles[:MAX_SWARM_AGENTS]


def _role_payload(role: SwarmRole) -> dict[str, Any]:
    return {
        "role": role.role,
        "title": role.title,
        "responsibility": role.responsibility,
        "claim_mode": role.claim_mode,
    }


def _custom_role(value: str, index: int) -> SwarmRole:
    role = value.strip().lower().replace(" ", "_") or f"agent_{index}"
    title = role.replace("_", " ").title()
    return SwarmRole(
        role=role,
        title=title,
        responsibility="Contribute to the parent task and coordinate through events.",
    )


def _provider_name(provider: str) -> str:
    return agent_providers.provider_display_name(provider, default="Agent")


def _child_prompt(
    request: dict[str, Any],
    *,
    role: dict[str, Any],
    index: int,
    total: int,
) -> str:
    role_title = str(role.get("title") or role.get("role") or f"Agent {index}")
    responsibility = str(role.get("responsibility") or "Contribute to the task.")
    selected_paths = ", ".join(request.get("selected_paths") or []) or "No selected files"
    parent_message = str(request.get("message") or "").strip()
    return (
        f"Agent Swarm role {index}/{total}: {role_title}\n"
        f"Responsibility: {responsibility}\n"
        f"Parent task: {parent_message}\n"
        f"Selected files: {selected_paths}\n\n"
        "Coordinate through the graph task callback, emit read/edit/test/decision/status "
        "events with the provided run_id, respect active file claims, and do not revert "
        "or overwrite peer work. End with changed files, verification, and blockers."
    )
