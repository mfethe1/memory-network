"""Agent task preflight logic for the live graph server."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any

from code_index import agent_providers
from code_index import agent_swarm
from code_index import config as cfg_mod
from code_index import task_gate
from code_index.commands.graph_server_dispatch import (
    _build_task_collaboration_packet,
    _build_task_context_packet,
    _build_task_graph_context,
)
from code_index.commands.graph_server_utils import (
    _canonical_json,
    _iso_after,
    _now_iso,
    _parse_iso,
    _sha256_json,
    _string_list,
)

PREFLIGHT_TTL_SECONDS = 10 * 60


def _task_request_from_payload(
    payload: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    provider = str(payload.get("provider") or "").strip().lower()
    provider_name = agent_providers.provider_display_name(
        provider,
        default=(provider.title() if provider else ""),
    )
    agent_name = str(
        payload.get("agent_name")
        or provider_name
        or args.agent_name
        or "Codex"
    )
    selected_nodes = _string_list(payload.get("selected_nodes"))
    if payload.get("node_id"):
        selected_nodes.extend(
            node
            for node in _string_list(payload.get("node_id"))
            if node not in selected_nodes
        )
    selected_paths = _string_list(payload.get("selected_paths"))
    if payload.get("path"):
        selected_paths.extend(
            path
            for path in _string_list(payload.get("path"))
            if path not in selected_paths
        )
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    node_path = str(node.get("path") or "").strip()
    if node_path and node.get("kind") == "file" and node_path not in selected_paths:
        selected_paths.append(node_path)
    parent_run_id = str(payload.get("parent_run_id") or "").strip()
    run_context = (
        payload.get("run_context")
        if isinstance(payload.get("run_context"), dict)
        else None
    )
    context = {
        "selected_paths": selected_paths,
        "node": node,
        "source": "graph-server",
    }
    if provider:
        context["provider"] = provider
    if parent_run_id:
        context["parent_run_id"] = parent_run_id
    if run_context is not None:
        context["run_context"] = run_context
    blocked_by_run_ids = _string_list(
        payload.get("blocked_by_run_ids")
        or payload.get("blocked_by_run_id")
        or payload.get("blocked_by")
    )
    if blocked_by_run_ids:
        context["blocked_by_run_ids"] = blocked_by_run_ids
    slice_payload = payload.get("slice") if isinstance(payload.get("slice"), dict) else {}
    if slice_payload:
        context["slice"] = slice_payload
    swarm_config = agent_swarm.normalize_swarm_config(
        payload,
        request_provider=provider or None,
    )
    execution_strategy = str(swarm_config.get("execution_strategy") or "single")
    if execution_strategy != "single":
        context["execution_strategy"] = execution_strategy
    public_swarm = agent_swarm.public_swarm_config(swarm_config)
    if public_swarm:
        context["swarm"] = public_swarm
    return {
        "message": str(payload.get("message") or payload.get("prompt") or "").strip(),
        "provider": provider,
        "agent_name": agent_name,
        "selected_nodes": selected_nodes,
        "selected_paths": selected_paths,
        "node": node,
        "parent_run_id": parent_run_id,
        "run_context": run_context,
        "blocked_by_run_ids": blocked_by_run_ids,
        "slice": slice_payload,
        "execution_strategy": execution_strategy,
        "swarm": swarm_config,
        "metadata": context,
        "preflight_confirmed": bool(payload.get("preflight_confirmed")),
    }


def _build_task_draft(
    config: cfg_mod.Config,
    request: dict[str, Any],
    *,
    callback_base_url: str,
) -> dict[str, Any]:
    try:
        context_budget = int(os.environ.get("CODE_INDEX_AGENT_CONTEXT_BUDGET") or 1600)
    except ValueError:
        context_budget = 1600
    task: dict[str, Any] = {
        "kind": "code_index_agent_task_draft",
        "root": str(config.root),
        "agent_name": request["agent_name"],
        "message": request["message"],
        "selected_nodes": request["selected_nodes"],
        "selected_paths": request["selected_paths"],
        "node": request["node"],
        "callback": {
            "agent_events_url": f"{callback_base_url}/api/agent-events",
        },
        "context_policy": {
            "initial_budget_tokens": context_budget,
            "runtime_retrieval": True,
            "retrieval_handles": {
                "selected_nodes": request["selected_nodes"],
                "selected_paths": request["selected_paths"],
            },
        },
    }
    if request.get("scope"):
        task["scope"] = request["scope"]
    scope = request.get("scope") or (request.get("metadata") or {}).get("scope")
    if isinstance(scope, dict):
        task["scope"] = scope
        task["context_policy"]["scope"] = scope.get("path")
    if request["provider"]:
        task["provider"] = request["provider"]
    if request["parent_run_id"]:
        task["parent_run_id"] = request["parent_run_id"]
    if request["run_context"] is not None:
        task["run_context"] = request["run_context"]
    if request.get("blocked_by_run_ids"):
        task["blocked_by_run_ids"] = request["blocked_by_run_ids"]
    if request.get("slice"):
        task["slice"] = request["slice"]
    if request.get("execution_strategy") == "swarm":
        task["execution_strategy"] = "swarm"
    public_swarm = agent_swarm.public_swarm_config(request.get("swarm"))
    if public_swarm:
        task["swarm"] = public_swarm
    task["graph_context"] = _build_task_graph_context(
        config,
        agent_name=request["agent_name"],
        selected_nodes=request["selected_nodes"],
        selected_paths=request["selected_paths"],
        node=request["node"],
    )
    return task


def _claim_overlaps(
    claims: list[dict[str, Any]], selected_paths: list[str], *, parent_run_id: str
) -> list[dict[str, Any]]:
    selected = {path for path in selected_paths if path}
    overlaps: list[dict[str, Any]] = []
    for claim in claims:
        path = claim.get("file_path")
        if not path or path not in selected:
            continue
        item = dict(claim)
        item["same_parent_run"] = bool(parent_run_id and claim.get("run_id") == parent_run_id)
        overlaps.append(item)
    return overlaps


def _care_levels_from_graph_context(graph_context: dict[str, Any]) -> list[str]:
    levels: list[str] = []
    for node in graph_context.get("selected_nodes") or []:
        if isinstance(node, dict) and node.get("care_level"):
            level = str(node["care_level"])
            if level not in levels:
                levels.append(level)
    return levels


def _preflight_from_draft(
    *,
    request: dict[str, Any],
    draft: dict[str, Any],
    active_claims: list[dict[str, Any]],
    blocking_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    graph_context = draft.get("graph_context") if isinstance(draft.get("graph_context"), dict) else {}
    care_levels = _care_levels_from_graph_context(graph_context)
    overlaps = _claim_overlaps(
        active_claims,
        list(request.get("selected_paths") or []),
        parent_run_id=str(request.get("parent_run_id") or ""),
    )
    foreign_overlaps = [
        claim for claim in overlaps if not claim.get("same_parent_run")
    ]
    warnings: list[dict[str, Any]] = []
    if not request.get("selected_paths"):
        warnings.append(
            {
                "kind": "no_selected_files",
                "severity": "warning",
                "message": "No concrete selected files were resolved for this task.",
            }
        )
    if "critical" in care_levels:
        warnings.append(
            {
                "kind": "critical_care",
                "severity": "warning",
                "message": "Selected context includes critical-care code.",
            }
        )
    elif "high" in care_levels:
        warnings.append(
            {
                "kind": "high_care",
                "severity": "info",
                "message": "Selected context includes high-care shared code.",
            }
        )
    if foreign_overlaps:
        warnings.append(
            {
                "kind": "overlapping_claims",
                "severity": "warning",
                "message": (
                    f"{len(foreign_overlaps)} active file claim(s) overlap this task."
                ),
                "claims": foreign_overlaps,
            }
        )
    unresolved_blockers = list(blocking_runs or [])
    if unresolved_blockers:
        warnings.append(
            {
                "kind": "active_blockers",
                "severity": "warning",
                "message": (
                    f"{len(unresolved_blockers)} blocker run(s) must complete before dispatch."
                ),
                "runs": [
                    {
                        "run_id": run.get("run_id"),
                        "agent_name": run.get("agent_name"),
                        "status": run.get("status"),
                        "prompt": run.get("prompt"),
                    }
                    for run in unresolved_blockers
                ],
            }
        )
    requires_confirmation = any(
        warning["kind"] in {"critical_care", "overlapping_claims"}
        for warning in warnings
    )
    return {
        "status": "blocked" if unresolved_blockers else ("needs_confirmation" if requires_confirmation else "clear"),
        "can_dispatch": not unresolved_blockers,
        "requires_confirmation": requires_confirmation,
        "warnings": warnings,
        "active_claims": active_claims,
        "overlapping_claims": overlaps,
        "blocking_runs": unresolved_blockers,
        "care_levels": care_levels,
        "selected_path_count": len(request.get("selected_paths") or []),
        "selected_node_count": len(request.get("selected_nodes") or []),
    }


def _preflight_required(request: dict[str, Any]) -> bool:
    return bool(
        request.get("selected_nodes")
        or request.get("selected_paths")
        or request.get("blocked_by_run_ids")
        or (request.get("node") or {}).get("id")
        or (request.get("node") or {}).get("path")
    )


def _preflight_hash_subject(
    *,
    request: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    return task_gate.preflight_hash_subject(request=request, draft=draft)


def _warning_fingerprint(preflight: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    for warning in preflight.get("warnings") or []:
        if not isinstance(warning, dict):
            continue
        claims = []
        for claim in warning.get("claims") or []:
            if isinstance(claim, dict):
                claims.append(
                    {
                        "claim_id": claim.get("claim_id"),
                        "file_path": claim.get("file_path"),
                        "mode": claim.get("mode"),
                        "run_id": claim.get("run_id"),
                        "fence_token": claim.get("fence_token"),
                    }
                )
        warnings.append(
            {
                "kind": warning.get("kind"),
                "severity": warning.get("severity"),
                "claims": sorted(claims, key=lambda c: _canonical_json(c)),
            }
        )
    return {
        "requires_confirmation": bool(preflight.get("requires_confirmation")),
        "warnings": sorted(warnings, key=lambda w: _canonical_json(w)),
    }


def _build_preflight_record(
    *,
    secret: str,
    request: dict[str, Any],
    draft: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    created_at = _now_iso()
    expires_at = _iso_after(PREFLIGHT_TTL_SECONDS)
    draft_hash = _sha256_json(_preflight_hash_subject(request=request, draft=draft))
    warning_hash = _sha256_json(_warning_fingerprint(preflight))
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{draft_hash}:{warning_hash}:{created_at}:{expires_at}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    preflight_id = f"pf_{signature[:32]}_{nonce}"
    return {
        "preflight_id": preflight_id,
        "draft_hash": draft_hash,
        "warning_hash": warning_hash,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def _store_preflight(
    conn,
    *,
    record: dict[str, Any],
    request: dict[str, Any],
    draft: dict[str, Any],
    preflight: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO agent_task_preflights(
            preflight_id, draft_hash, warning_hash, status, run_id,
            created_at, expires_at, payload_json
        )
        VALUES (?, ?, ?, 'active', NULL, ?, ?, ?)
        """,
        (
            record["preflight_id"],
            record["draft_hash"],
            record["warning_hash"],
            record["created_at"],
            record["expires_at"],
            _canonical_json(
                {
                    "request": request,
                    "draft": draft,
                    "preflight": preflight,
                }
            ),
        ),
    )


def _extract_preflight_id(payload: dict[str, Any]) -> str:
    direct = str(payload.get("preflight_id") or "").strip()
    if direct:
        return direct
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
    return str(preflight.get("preflight_id") or "").strip()


def _preflight_rejection(status: int, reason: str) -> tuple[int, dict[str, Any]]:
    return status, {
        "ok": False,
        "error": reason,
        "preflight_required": True,
        "kind": "code_index_graph_preflight_rejection",
    }


def _consume_preflight(
    conn,
    *,
    payload: dict[str, Any],
    request: dict[str, Any],
    draft: dict[str, Any],
    preflight: dict[str, Any],
    run_id: str,
) -> tuple[int, dict[str, Any]] | None:
    if not _preflight_required(request):
        return None
    preflight_id = _extract_preflight_id(payload)
    if not preflight_id:
        return _preflight_rejection(
            HTTPStatus.PRECONDITION_REQUIRED,
            "preflight_id is required for graph-scoped agent runs",
        )
    row = conn.execute(
        """
        SELECT *
          FROM agent_task_preflights
         WHERE preflight_id = ?
         LIMIT 1
        """,
        (preflight_id,),
    ).fetchone()
    if row is None:
        return _preflight_rejection(
            HTTPStatus.PRECONDITION_FAILED,
            "unknown preflight_id",
        )
    status = str(row["status"] or "")
    if status != "active":
        http_status = (
            HTTPStatus.CONFLICT
            if status == "consumed"
            else HTTPStatus.PRECONDITION_FAILED
        )
        return _preflight_rejection(http_status, f"preflight is {status}")
    expires_at = _parse_iso(row["expires_at"])
    if expires_at is not None and expires_at < datetime.now(timezone.utc):
        conn.execute(
            "UPDATE agent_task_preflights SET status = 'expired' WHERE preflight_id = ?",
            (preflight_id,),
        )
        return _preflight_rejection(
            HTTPStatus.PRECONDITION_FAILED,
            "preflight is expired",
        )
    draft_hash = _sha256_json(_preflight_hash_subject(request=request, draft=draft))
    warning_hash = _sha256_json(_warning_fingerprint(preflight))
    if not hmac.compare_digest(str(row["draft_hash"]), draft_hash):
        return _preflight_rejection(
            HTTPStatus.PRECONDITION_FAILED,
            "preflight draft does not match current task",
        )
    if not hmac.compare_digest(str(row["warning_hash"]), warning_hash):
        return _preflight_rejection(
            HTTPStatus.PRECONDITION_FAILED,
            "preflight warnings changed; run preflight again",
        )
    if preflight.get("requires_confirmation") and not request.get("preflight_confirmed"):
        return _preflight_rejection(
            HTTPStatus.PRECONDITION_REQUIRED,
            "preflight confirmation is required",
        )
    conn.execute(
        """
        UPDATE agent_task_preflights
           SET status = 'consumed',
               run_id = ?
         WHERE preflight_id = ?
           AND status = 'active'
        """,
        (run_id, preflight_id),
    )
    return None
