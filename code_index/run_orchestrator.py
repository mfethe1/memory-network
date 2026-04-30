"""Shared Agent Run lifecycle classification for Graph Agent Companion."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from code_index import agent_activity
from code_index import agent_swarm
from code_index import run_lifecycle


RUN_HEALTHS = {
    "healthy",
    "quiet",
    "stale",
    "orphaned",
    "blocked",
    "needs_review",
    "conflicted",
    "completed",
    "failed",
    "cancelled",
}


@dataclass(frozen=True)
class OrchestratorPolicy:
    quiet_after_seconds: float = 10 * 60
    stale_after_seconds: float = float(agent_activity.DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status(value: Any) -> str:
    return str(value or "working").strip().lower().replace("-", "_")


def _verification_state(run: dict[str, Any]) -> str:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    raw = metadata.get("verification_state") or metadata.get("verification")
    if isinstance(raw, dict):
        raw = raw.get("status") or raw.get("state")
    state = str(raw or "not_run").strip().lower().replace("-", "_")
    return state if state in {"passed", "failed", "blocked", "not_run"} else "not_run"


def _run_claims(
    active_claims: list[dict[str, Any]] | None,
    run_id: str,
) -> list[dict[str, Any]]:
    return [
        claim
        for claim in (active_claims or [])
        if str(claim.get("run_id") or "") == run_id
    ]


def classify_run(
    run: dict[str, Any],
    *,
    active_claims: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    policy: OrchestratorPolicy | None = None,
    process_liveness: str = "unknown",
) -> dict[str, Any]:
    """Return derived lifecycle facts for one Agent Run.

    The durable run status remains the source of truth. This function derives
    operational health without writing it back into the run row.
    """

    current_policy = policy or OrchestratorPolicy()
    current_time = now or _now()
    run_id = str(run.get("run_id") or "")
    status = _status(run.get("status"))
    updated_at = _parse_time(run.get("updated_at")) or _parse_time(run.get("started_at"))
    age_seconds = (
        max(0.0, (current_time - updated_at).total_seconds())
        if updated_at is not None
        else None
    )
    claims = _run_claims(active_claims, run_id)
    active_blockers = [
        blocker
        for blocker in run.get("blocked_by") or []
        if _status(blocker.get("status")) == "active"
    ]
    reasons: list[str] = []

    if status in agent_activity.REVIEW_STATUSES:
        health = "needs_review"
        reasons.append("run is in durable review status")
    elif status in agent_activity.TERMINAL_STATUSES:
        health = "cancelled" if status == "canceled" else status
        reasons.append(f"run is terminal: {status}")
    elif active_blockers or status == "blocked":
        health = "blocked"
        reasons.append("run has active blockers")
    elif process_liveness == "dead":
        health = "orphaned"
        reasons.append("process liveness is known dead")
    elif age_seconds is not None and age_seconds >= current_policy.stale_after_seconds:
        health = "stale"
        reasons.append(f"no update for {int(age_seconds)}s")
    elif age_seconds is not None and age_seconds >= current_policy.quiet_after_seconds:
        health = "quiet"
        reasons.append(f"quiet for {int(age_seconds)}s")
    else:
        health = "healthy"
        reasons.append("recent activity is within policy")

    if claims and health == "healthy":
        reasons.append(f"{len(claims)} active claim(s)")

    return {
        "run_id": run_id,
        "status": status,
        "health": health,
        "verification_state": _verification_state(run),
        "process_liveness": process_liveness,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "active_claim_count": len(claims),
        "active_files": [claim.get("file_path") for claim in claims if claim.get("file_path")],
        "reasons": reasons,
    }


def annotate_activity_snapshot(
    snapshot: dict[str, Any],
    *,
    policy: OrchestratorPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach derived health to active/recent runs in an activity snapshot."""

    out = dict(snapshot)
    active_claims = list(out.get("active_claims") or [])
    current_time = now or _now()
    current_policy = policy or OrchestratorPolicy()
    health_by_run: dict[str, dict[str, Any]] = {}

    def annotate_run(run: dict[str, Any]) -> dict[str, Any]:
        item = dict(run)
        run_id = str(item.get("run_id") or "")
        health = health_by_run.get(run_id)
        if health is None:
            health = classify_run(
                item,
                active_claims=active_claims,
                now=current_time,
                policy=current_policy,
            )
            health_by_run[health["run_id"]] = health
        item["run_health"] = health
        return item

    for key in ("active_runs", "recent_runs"):
        out[key] = [annotate_run(run) for run in out.get(key) or []]

    kanban = out.get("kanban")
    if isinstance(kanban, dict) and isinstance(kanban.get("columns"), dict):
        kanban = dict(kanban)
        columns: dict[str, Any] = {}
        for name, column in kanban["columns"].items():
            if not isinstance(column, dict):
                columns[name] = column
                continue
            column_copy = dict(column)
            column_copy["runs"] = [
                annotate_run(run)
                for run in column.get("runs") or []
                if isinstance(run, dict)
            ]
            columns[name] = column_copy
        kanban["columns"] = columns
        out["kanban"] = kanban

    review_runs = list(
        ((out.get("kanban") or {}).get("columns") or {})
        .get("review", {})
        .get("runs", [])
    )
    out["orchestrator"] = {
        "kind": "code_index_run_orchestrator_snapshot",
        "policy": _policy_payload(current_policy),
        "run_health": health_by_run,
        "review_runs": review_runs,
        "counts": _health_counts(health_by_run.values()),
    }
    return out


def snapshot(
    conn: sqlite3.Connection,
    *,
    policy: OrchestratorPolicy | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    row_limit = max(1, int(limit))
    activity = agent_activity.activity_snapshot(
        conn,
        event_limit=max(20, row_limit),
        file_limit=12,
        active_run_max_age_seconds=None,
    )
    activity["kanban"] = agent_activity.kanban_board(conn, limit=row_limit)
    return annotate_activity_snapshot(activity, policy=policy, now=now)


def apply(
    conn: sqlite3.Connection,
    *,
    known_dead_run_ids: set[str] | None = None,
    policy: OrchestratorPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply deterministic lifecycle actions and return an audit report."""

    dead_ids = set(known_dead_run_ids or set())
    process_liveness = run_lifecycle.process_liveness_by_run(conn)
    for run_id in dead_ids:
        process_liveness[run_id] = "dead"
    current_time = now or _now()
    active_claims = agent_activity.active_file_claims(conn, limit=500)
    released_claims: list[dict[str, Any]] = []
    for claim in active_claims:
        expires_at = _parse_time(claim.get("expires_at"))
        if expires_at is not None and expires_at <= current_time:
            released_claims.extend(
                agent_activity.release_claims(
                    conn,
                    run_id=str(claim["run_id"]),
                    file_path=str(claim["file_path"]),
                    status="expired",
                )
            )

    active_claims = agent_activity.active_file_claims(conn, limit=500)
    runs = agent_activity.active_runs(conn, limit=100, max_age_seconds=None)
    actions: list[dict[str, Any]] = []
    for run in runs:
        run_id = str(run.get("run_id") or "")
        health = classify_run(
            run,
            active_claims=active_claims,
            now=current_time,
            policy=policy,
            process_liveness=process_liveness.get(run_id, "unknown"),
        )
        if health["health"] == "orphaned" and health["active_claim_count"] == 0:
            agent_activity.end_run(conn, run_id=run_id, status="review")
            agent_activity.record_event(
                conn,
                run_id=run_id,
                event_type="status",
                message=(
                    "Run Orchestrator moved this run to review because the "
                    "process is no longer live and no active claims remain."
                ),
                payload={"status": "review", "run_health": health},
            )
            actions.append(
                {
                    "action": "move_to_review",
                    "run_id": run_id,
                    "reason": "known_dead_without_active_claims",
                }
            )

    actions.extend(_materialize_ready_swarm_parents(conn))
    swarm_reconciliations = _reconcile_active_swarm_parents(conn)
    result = snapshot(conn, policy=policy, now=current_time)
    result["actions"] = actions
    result["released_claims"] = released_claims
    result["swarm_reconciliations"] = swarm_reconciliations
    return result


def _reconcile_active_swarm_parents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    reconciliations: list[dict[str, Any]] = []
    for run in agent_activity.active_runs(conn, limit=100, max_age_seconds=None):
        if agent_swarm.is_swarm_parent(run):
            reconciliations.append(
                agent_swarm.reconcile_swarm_parent(
                    conn,
                    parent_run_id=str(run["run_id"]),
                )
            )
    return reconciliations


def _materialize_ready_swarm_parents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for run in agent_activity.active_runs(conn, limit=100, max_age_seconds=None):
        status = str(run.get("status") or "").strip().lower()
        if not agent_swarm.is_swarm_parent(run) or status not in {"queued", "ready"}:
            continue
        if agent_activity.active_run_blockers(conn, run_id=str(run["run_id"])):
            continue
        if agent_activity.child_runs(conn, parent_run_id=str(run["run_id"])):
            continue
        try:
            result = agent_swarm.materialize_swarm_children(
                conn,
                parent_run_id=str(run["run_id"]),
            )
        except ValueError as exc:
            actions.append(
                {
                    "action": "materialize_swarm_children_failed",
                    "run_id": str(run["run_id"]),
                    "reason": str(exc),
                }
            )
            continue
        if result.get("changed"):
            actions.append(
                {
                    "action": result["action"],
                    "run_id": result["run_id"],
                    "reason": result.get("reason"),
                    "status": result.get("status"),
                    "child_count": len(result.get("children") or []),
                    "expected_child_count": result.get("expected_child_count"),
                }
            )
    return actions


def _policy_payload(policy: OrchestratorPolicy) -> dict[str, Any]:
    return {
        "quiet_after_seconds": policy.quiet_after_seconds,
        "stale_after_seconds": policy.stale_after_seconds,
    }


def _health_counts(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        health = str((item or {}).get("health") or "unknown")
        counts[health] = counts.get(health, 0) + 1
    return dict(sorted(counts.items()))
