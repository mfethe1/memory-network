"""HTTP handler factory for the live graph server."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from code_index import agent_activity
from code_index import agent_providers
from code_index import agent_swarm
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_orchestrator
from code_index import task_gate
from code_index import retrieval
from code_index.agent_collaboration import append_event_jsonl
from code_index.commands.graph_html import render_html
from code_index.commands.graph_notes import graph_notes_block, upsert_note
from code_index.commands.graph_server_dispatch import (
    _build_task_collaboration_packet,
    _build_task_context_packet,
    _build_task_graph_context,
    _cancel_local_agent_task,
    _dispatch_agent_task,
)
from code_index.commands.graph_server_state import (
    _agent_stream_payload,
    _build_debug_payload,
    _build_payload,
    _record_user_note_event,
    _state_signature,
)
from code_index.commands.graph_server_utils import (
    GRAPH_TOKEN_ENV_VAR,
    _json_bytes,
    _string_list,
    _validate_bearer,
)
from code_index.locking import writer_lock

PREFLIGHT_TTL_SECONDS = 10 * 60
GRAPH_SESSION_COOKIE = "code_index_graph_session"
GRAPH_SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _preflight_secret() -> str:
    env_secret = os.environ.get("CODE_INDEX_GRAPH_PREFLIGHT_SECRET", "").strip()
    if env_secret:
        return env_secret
    token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    return secrets.token_hex(32)


def _session_cookie_value(secret: str, graph_token: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"graph-session:{graph_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _cookie_value(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else None


def _make_perf_state() -> dict[str, Any]:
    return {
        "lock": threading.Lock(),
        "counters": {
            "preflight_rejections": {},
            "auth_failures": {},
            "sse_dropped_events": 0,
            "claim_conflicts": 0,
            "stale_runs": 0,
            "retrieval_budget": {
                "broker_configured": True,
                "requests": 0,
                "budget_rejections": 0,
            },
            "search_latency_ms": {
                "count": 0,
                "last": None,
                "max": None,
                "avg": None,
                "by_scope": {},
            },
        },
    }


def _inc_counter(perf: dict[str, Any], group: str, key: str | None = None) -> None:
    lock = perf.get("lock")
    counters = perf.get("counters")
    if not isinstance(counters, dict):
        return
    if lock:
        lock.acquire()
    try:
        if key is None:
            counters[group] = int(counters.get(group) or 0) + 1
            return
        bucket = counters.setdefault(group, {})
        if isinstance(bucket, dict):
            bucket[key] = int(bucket.get(key) or 0) + 1
    finally:
        if lock:
            lock.release()


def _observe_latency(
    perf: dict[str, Any], group: str, elapsed_ms: float, key: str | None = None
) -> None:
    lock = perf.get("lock")
    counters = perf.get("counters")
    if not isinstance(counters, dict):
        return
    if lock:
        lock.acquire()
    try:
        bucket = counters.setdefault(
            group,
            {"count": 0, "last": None, "max": None, "avg": None, "by_scope": {}},
        )
        if not isinstance(bucket, dict):
            return
        count = int(bucket.get("count") or 0) + 1
        previous_avg = float(bucket.get("avg") or 0)
        value = round(float(elapsed_ms), 2)
        bucket["count"] = count
        bucket["last"] = value
        bucket["max"] = value if bucket.get("max") is None else max(float(bucket["max"]), value)
        bucket["avg"] = round(previous_avg + ((value - previous_avg) / count), 2)
        if key:
            by_scope = bucket.setdefault("by_scope", {})
            if not isinstance(by_scope, dict):
                by_scope = {}
                bucket["by_scope"] = by_scope
            scoped = by_scope.setdefault(
                key, {"count": 0, "last": None, "max": None, "avg": None}
            )
            if not isinstance(scoped, dict):
                scoped = {"count": 0, "last": None, "max": None, "avg": None}
                by_scope[key] = scoped
            scoped_count = int(scoped.get("count") or 0) + 1
            scoped_avg = float(scoped.get("avg") or 0)
            scoped["count"] = scoped_count
            scoped["last"] = value
            scoped["max"] = (
                value
                if scoped.get("max") is None
                else max(float(scoped["max"]), value)
            )
            scoped["avg"] = round(scoped_avg + ((value - scoped_avg) / scoped_count), 2)
    finally:
        if lock:
            lock.release()


def _observe_retrieval_budget(perf: dict[str, Any], payload: dict[str, Any]) -> None:
    lock = perf.get("lock")
    counters = perf.get("counters")
    if not isinstance(counters, dict):
        return
    if lock:
        lock.acquire()
    try:
        bucket = counters.setdefault(
            "retrieval_budget",
            {"broker_configured": True, "requests": 0, "budget_rejections": 0},
        )
        if not isinstance(bucket, dict):
            return
        bucket["broker_configured"] = True
        bucket["requests"] = int(bucket.get("requests") or 0) + 1
        retrieval_payload = payload.get("retrieval")
        if (
            isinstance(retrieval_payload, dict)
            and retrieval_payload.get("truncation_reason") == "byte_budget"
        ):
            bucket["budget_rejections"] = int(bucket.get("budget_rejections") or 0) + 1
    finally:
        if lock:
            lock.release()


def _perf_snapshot(perf: dict[str, Any]) -> dict[str, Any]:
    lock = perf.get("lock")
    if lock:
        lock.acquire()
    try:
        counters = json.loads(json.dumps(perf.get("counters") or {}))
    finally:
        if lock:
            lock.release()
    return {
        "kind": "code_index_graph_debug_perf",
        "generated_at": _now_iso(),
        "counters": counters,
    }


def _perf_tick_payload(perf: dict[str, Any]) -> dict[str, Any]:
    payload = _perf_snapshot(perf)
    payload["type"] = "perf:tick"
    return payload


def _auth_page_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Graph Auth</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f8fafc; color: #111827; }
    main { width: min(420px, calc(100vw - 32px)); border: 1px solid #d1d5db; background: white; padding: 24px; border-radius: 8px; box-shadow: 0 12px 32px rgba(15, 23, 42, 0.12); }
    h1 { font-size: 20px; margin: 0 0 12px; }
    p { color: #4b5563; line-height: 1.5; }
    label { display: block; font-size: 13px; font-weight: 600; margin: 16px 0 6px; }
    input { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid #9ca3af; border-radius: 6px; font: inherit; }
    button { margin-top: 14px; padding: 9px 14px; border: 1px solid #111827; border-radius: 6px; background: #111827; color: white; font: inherit; cursor: pointer; }
    .status { min-height: 20px; margin-top: 12px; color: #b91c1c; font-size: 13px; }
  </style>
</head>
<body>
  <main>
    <h1>Graph server token</h1>
    <p>Enter the local graph token to create a same-origin browser session.</p>
    <form id="auth-form">
      <label for="token">Token</label>
      <input id="token" name="token" type="password" autocomplete="current-password" autofocus>
      <button type="submit">Continue</button>
      <div class="status" id="status"></div>
    </form>
  </main>
  <script>
    try {
      const current = new URL(window.location.href);
      ["token", "graph_token", "access_token"].forEach((name) => current.searchParams.delete(name));
      window.history.replaceState({}, document.title, `${current.pathname}${current.search}${current.hash}`);
    } catch (_err) {}
    document.getElementById("auth-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = document.getElementById("token").value.trim();
      const status = document.getElementById("status");
      if (!token) {
        status.textContent = "Token required";
        return;
      }
      const response = await fetch("/api/auth/browser-session", {
        method: "POST",
        credentials: "same-origin",
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!response.ok) {
        status.textContent = "Invalid token";
        return;
      }
      window.location.replace("/repo-graph.html");
    });
  </script>
</body>
</html>"""


def _task_request_from_payload(
    payload: dict[str, Any], args: argparse.Namespace
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


def _search_sources_for_scope(scope: str) -> tuple[retrieval.SourceKind, ...]:
    if scope == "files":
        return (retrieval.SourceKind.FILE_PATH, retrieval.SourceKind.CODE_CHUNK)
    if scope == "transcripts":
        return (retrieval.SourceKind.TRANSCRIPT_EVENT,)
    return retrieval.DEFAULT_SOURCES


def _broker_file_result(
    item: dict[str, Any],
    *,
    path_match_also: bool = False,
) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    source_kind = str(item.get("source_kind") or "")
    if source_kind == retrieval.SourceKind.FILE_PATH.value:
        return {
            "kind": "file_path",
            "file_path": payload.get("file_path") or item.get("file_path"),
            "language": payload.get("language"),
            "parse_status": payload.get("parse_status"),
            "score": item.get("score"),
            "snippet": payload.get("text") or payload.get("file_path") or "",
            "handle": item.get("handle"),
            "byte_cost": item.get("byte_cost"),
        }
    out = {
        "kind": "file_content",
        "file_path": payload.get("file_path") or item.get("file_path"),
        "language": payload.get("language"),
        "chunk_type": payload.get("chunk_type"),
        "symbol_name": payload.get("symbol_name"),
        "symbol_path": payload.get("symbol_path"),
        "signature": payload.get("signature"),
        "start_line": payload.get("start_line"),
        "end_line": payload.get("end_line"),
        "score": item.get("score"),
        "snippet": payload.get("text") or "",
        "handle": item.get("handle"),
        "byte_cost": item.get("byte_cost"),
    }
    if path_match_also:
        out["path_match_also"] = True
    return out


def _broker_transcript_result(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return {
        "kind": "transcript_event",
        "event_pk": payload.get("event_pk"),
        "run_id": payload.get("run_id"),
        "agent_name": payload.get("agent_name"),
        "status": payload.get("status"),
        "prompt": payload.get("prompt"),
        "timestamp": payload.get("timestamp"),
        "event_type": payload.get("event_type"),
        "file_path": payload.get("file_path") or item.get("file_path"),
        "symbol_path": payload.get("symbol_path"),
        "message": payload.get("message") or "",
        "snippet": payload.get("text") or "",
        "handle": item.get("handle"),
        "byte_cost": item.get("byte_cost"),
    }


def _build_search_payload(
    config: cfg_mod.Config,
    *,
    query: str,
    scope: str,
    limit: int,
) -> dict[str, Any]:
    normalized_scope = scope if scope in {"all", "files", "transcripts"} else "all"
    safe_limit = max(1, min(50, int(limit or 12)))
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        broker_response = retrieval.retrieve(
            conn,
            retrieval.RetrievalRequest(
                query=query,
                limit=safe_limit,
                budget_bytes=20_000,
                sources=_search_sources_for_scope(normalized_scope),
                per_source_limit=safe_limit,
            ),
        ).to_dict()
        file_results: list[dict[str, Any]] = []
        transcript_results: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for item in broker_response.get("results") or []:
            if not isinstance(item, dict):
                continue
            source_kind = str(item.get("source_kind") or "")
            if source_kind == retrieval.SourceKind.FILE_PATH.value:
                result = _broker_file_result(item)
                path = str(result.get("file_path") or "")
                if path:
                    seen_paths.add(path)
                file_results.append(result)
            elif source_kind == retrieval.SourceKind.CODE_CHUNK.value:
                path = str((item.get("payload") or {}).get("file_path") or "")
                file_results.append(
                    _broker_file_result(item, path_match_also=path in seen_paths)
                )
            elif source_kind == retrieval.SourceKind.TRANSCRIPT_EVENT.value:
                transcript_results.append(_broker_transcript_result(item))
    finally:
        db_mod.close(conn)
    return {
        "ok": True,
        "kind": "code_index_graph_search",
        "query": query,
        "scope": normalized_scope,
        "limit": safe_limit,
        "files": file_results,
        "transcripts": transcript_results,
        "counts": {
            "files": len(file_results),
            "transcripts": len(transcript_results),
        },
        "retrieval": {
            "kind": broker_response.get("kind"),
            "bytes_used": broker_response.get("bytes_used"),
            "budget_bytes": broker_response.get("budget_bytes"),
            "candidate_count": broker_response.get("candidate_count"),
            "truncation_reason": broker_response.get("truncation_reason"),
        },
    }


def _make_handler(config: cfg_mod.Config, args: argparse.Namespace):
    preflight_secret = _preflight_secret()
    perf_state = _make_perf_state()

    class GraphHandler(BaseHTTPRequestHandler):
        server_version = "code_index-graph/1"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            if getattr(self.server, "quiet", False):
                return
            super().log_message(format, *args)

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str = "application/json",
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_unauthorized(self) -> None:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", "Bearer")
            body = _json_bytes({"error": "unauthorized"})
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _is_authorized(self) -> bool:
            token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
            if not token:
                return True
            if _validate_bearer(self.headers.get("Authorization"), token):
                return True
            expected_cookie = _session_cookie_value(preflight_secret, token)
            cookie = _cookie_value(self.headers.get("Cookie"), GRAPH_SESSION_COOKIE)
            if cookie and hmac.compare_digest(cookie, expected_cookie):
                return True
            return False

        def _authorized(self) -> bool:
            if self._is_authorized():
                return True
            route = urlparse(self.path).path
            _inc_counter(perf_state, "auth_failures", route or "unknown")
            self._send_unauthorized()
            return False

        def _send_auth_page(self) -> None:
            self._send_bytes(
                HTTPStatus.OK,
                _auth_page_html().encode("utf-8"),
                "text/html",
            )

        def _create_browser_session(self) -> None:
            token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
            if not token:
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes({"ok": True, "auth_required": False}),
                )
                return
            if not _validate_bearer(self.headers.get("Authorization"), token):
                _inc_counter(perf_state, "auth_failures", "/api/auth/browser-session")
                self._send_unauthorized()
                return
            cookie_value = _session_cookie_value(preflight_secret, token)
            cookie = (
                f"{GRAPH_SESSION_COOKIE}={cookie_value}; "
                f"Max-Age={GRAPH_SESSION_MAX_AGE_SECONDS}; "
                "Path=/; HttpOnly; SameSite=Strict"
            )
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "auth_required": True,
                        "auth": "browser-session-cookie",
                        "cookie_name": GRAPH_SESSION_COOKIE,
                    }
                ),
                headers={"Set-Cookie": cookie},
            )

        def _read_json_payload(self) -> dict[str, Any] | None:
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid Content-Length"}),
                )
                return None
            if length < 0:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid Content-Length"}),
                )
                return None
            try:
                body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(body or "{}")
            except UnicodeDecodeError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "body must be UTF-8 JSON"}),
                )
                return None
            except json.JSONDecodeError:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "invalid JSON body"}),
                )
                return None
            if not isinstance(payload, dict):
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "JSON body must be an object"}),
                )
                return None
            return payload

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            if not self._is_authorized():
                if route in {"/", "/repo-graph.html"} and os.environ.get(
                    GRAPH_TOKEN_ENV_VAR, ""
                ).strip():
                    self._send_auth_page()
                    return
                _inc_counter(perf_state, "auth_failures", route or "unknown")
                self._send_unauthorized()
                return
            if route in {"/", "/repo-graph.html"}:
                payload = _build_payload(config, args)
                self._send_bytes(
                    HTTPStatus.OK,
                    render_html(payload).encode("utf-8"),
                    "text/html",
                )
                return
            if route == "/repo-graph.json":
                self._send_bytes(HTTPStatus.OK, _json_bytes(_build_payload(config, args)))
                return
            if route == "/api/debug":
                perf = _perf_snapshot(perf_state)
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(_build_debug_payload(config, args, perf)),
                )
                return
            if route == "/api/debug/perf":
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(_perf_snapshot(perf_state)),
                )
                return
            if route == "/api/agent-providers":
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(
                        {
                            "ok": True,
                            "kind": "code_index_agent_provider_registry",
                            "providers": agent_providers.provider_registry_payload(),
                        }
                    ),
                )
                return
            if route == "/api/agent-board":
                self._send_agent_board()
                return
            if route == "/api/file-claims":
                self._send_file_claims()
                return
            if route == "/api/search":
                self._send_search(parsed.query)
                return
            if route == "/notes.json":
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_bytes(graph_notes_block(config.root)),
                )
                return
            if route.startswith("/api/agent-runs/"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 3:
                    self._send_agent_run(parts[2])
                    return
            if route == "/events":
                self._stream_events()
                return
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found", "path": route}),
            )

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/api/auth/browser-session":
                self._create_browser_session()
                return
            if not self._authorized():
                return
            payload = self._read_json_payload()
            if payload is None:
                return
            if route == "/api/notes":
                try:
                    saved = upsert_note(config.root, payload)
                    _record_user_note_event(config, payload, saved)
                except ValueError as exc:
                    self._send_bytes(
                        HTTPStatus.BAD_REQUEST,
                        _json_bytes({"error": str(exc)}),
                    )
                    return
                self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "note": saved}))
                return
            if route == "/api/agent-runs":
                self._start_agent_run(payload)
                return
            if route == "/api/agent-task-preflight":
                self._preflight_agent_task(payload)
                return
            if route.startswith("/api/agent-runs/") and route.endswith("/cancel"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 4:
                    self._cancel_agent_run(parts[2])
                    return
            if route.startswith("/api/agent-runs/") and route.endswith("/archive"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 4:
                    self._archive_agent_run(parts[2])
                    return
            if route == "/api/agent-events":
                self._record_agent_event(payload)
                return
            if route.startswith("/api/file-claims/") and route.endswith("/renew"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 4:
                    self._renew_file_claim(parts[2], payload)
                    return
            if route.startswith("/api/file-claims/") and route.endswith("/release"):
                parts = [part for part in route.split("/") if part]
                if len(parts) == 4:
                    self._release_file_claim(parts[2], payload)
                    return
            if route == "/api/file-claims":
                self._manage_file_claims(payload)
                return
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                _json_bytes({"error": "not found", "path": route}),
            )

        def _send_file_claims(self) -> None:
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                claims = agent_activity.active_file_claims(conn, limit=200)
            finally:
                db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "kind": "code_index_agent_file_claims",
                        "active_claims": claims,
                        "count": len(claims),
                    }
                ),
            )

        def _send_agent_board(self) -> None:
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                activity = run_orchestrator.snapshot(conn, limit=80)
                board = activity.get("kanban") or agent_activity.kanban_board(
                    conn, limit=25
                )
                board["orchestrator"] = activity.get("orchestrator")
            finally:
                db_mod.close(conn)
            self._send_bytes(HTTPStatus.OK, _json_bytes(board))

        def _send_search(self, query_string: str) -> None:
            started = time.perf_counter()
            params = parse_qs(query_string, keep_blank_values=False)
            query = str((params.get("q") or params.get("query") or [""])[0]).strip()
            if not query:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "q is required"}),
                )
                return
            scope = str((params.get("scope") or ["all"])[0]).strip().lower()
            try:
                limit = int((params.get("limit") or ["12"])[0])
            except ValueError:
                limit = 12
            result = _build_search_payload(
                config,
                query=query,
                scope=scope,
                limit=limit,
            )
            _observe_retrieval_budget(perf_state, result)
            _observe_latency(
                perf_state,
                "search_latency_ms",
                (time.perf_counter() - started) * 1000,
                str(result.get("scope") or scope or "all"),
            )
            self._send_bytes(HTTPStatus.OK, _json_bytes(result))

        def _renew_file_claim(self, claim_id: str, payload: dict[str, Any]) -> None:
            from code_index import lease_manager

            try:
                ttl_raw = payload.get("ttl_seconds", 1800)
                ttl_seconds = None if ttl_raw is None else float(ttl_raw)
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        claim = lease_manager.renew_lease(
                            conn,
                            claim_id=claim_id,
                            lease_token=str(payload.get("lease_token") or ""),
                            fence_token=payload.get("fence_token"),
                            ttl_seconds=ttl_seconds,
                        )
                    finally:
                        db_mod.close(conn)
            except (TypeError, ValueError) as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "claim": claim}))

        def _release_file_claim(self, claim_id: str, payload: dict[str, Any]) -> None:
            from code_index import lease_manager

            try:
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        claim = lease_manager.release_lease(
                            conn,
                            claim_id=claim_id,
                            lease_token=str(payload.get("lease_token") or ""),
                            status=str(payload.get("status") or "released"),
                        )
                    finally:
                        db_mod.close(conn)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            self._send_bytes(HTTPStatus.OK, _json_bytes({"ok": True, "claim": claim}))

        def _manage_file_claims(self, payload: dict[str, Any]) -> None:
            run_id = str(payload.get("run_id") or "").strip()
            if not run_id:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "run_id is required"}),
                )
                return
            file_paths = _string_list(
                payload.get("file_paths") or payload.get("file_path") or payload.get("path")
            )
            action = str(payload.get("action") or "claim").strip().lower()
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    if action == "claim":
                        claims = agent_activity.claim_files(
                            conn,
                            run_id=run_id,
                            file_paths=file_paths,
                            mode=str(payload.get("mode") or "edit"),
                            reason=payload.get("reason"),
                            ttl_seconds=payload.get(
                                "ttl_seconds",
                                agent_activity.DEFAULT_CLAIM_TTL_SECONDS,
                            ),
                            metadata={"source": "graph-server-api"},
                        )
                    elif action == "release":
                        if file_paths:
                            claims = []
                            for path in file_paths:
                                claims.extend(
                                    agent_activity.release_claims(
                                        conn,
                                        run_id=run_id,
                                        file_path=path,
                                        mode=payload.get("mode"),
                                    )
                                )
                        else:
                            claims = agent_activity.release_claims(conn, run_id=run_id)
                    else:
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": f"unknown claim action: {action}"}),
                        )
                        return
                    active_claims = agent_activity.active_file_claims(conn, limit=200)
                except ValueError as exc:
                    message = str(exc)
                    if "claim conflict" in message:
                        _inc_counter(perf_state, "claim_conflicts")
                        status = HTTPStatus.CONFLICT
                    else:
                        status = HTTPStatus.BAD_REQUEST
                    self._send_bytes(
                        status,
                        _json_bytes({"error": message}),
                    )
                    return
                finally:
                    db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "action": action,
                        "claims": claims,
                        "active_claims": active_claims,
                    }
                ),
            )

        def _preflight_agent_task(self, payload: dict[str, Any]) -> None:
            try:
                request = _task_request_from_payload(payload, args)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            if not request["message"]:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "message is required"}),
                )
                return
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    active_claims = agent_activity.active_file_claims(conn, limit=200)
                    draft = _build_task_draft(
                        config,
                        request,
                        callback_base_url=self._callback_base_url(),
                    )
                    try:
                        blocking_runs = agent_activity.blocking_runs(
                            conn,
                            run_ids=request.get("blocked_by_run_ids") or [],
                        )
                    except ValueError as exc:
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": str(exc)}),
                        )
                        return
                    preflight = _preflight_from_draft(
                        request=request,
                        draft=draft,
                        active_claims=active_claims,
                        blocking_runs=blocking_runs,
                    )
                    record = _build_preflight_record(
                        secret=preflight_secret,
                        request=request,
                        draft=draft,
                        preflight=preflight,
                    )
                    preflight.update(record)
                    _store_preflight(
                        conn,
                        record=record,
                        request=request,
                        draft=draft,
                        preflight=preflight,
                    )
                finally:
                    db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "kind": "code_index_graph_agent_task_preflight",
                        "draft": draft,
                        "preflight": preflight,
                        "dispatch_path": "/api/agent-runs",
                    }
                ),
            )

        def _send_agent_run(self, run_id: str) -> None:
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                transcript = agent_activity.run_transcript(conn, run_id)
            finally:
                db_mod.close(conn)
            if transcript is None:
                self._send_bytes(
                    HTTPStatus.NOT_FOUND,
                    _json_bytes({"error": f"unknown run_id: {run_id}"}),
                )
                return
            self._send_bytes(HTTPStatus.OK, _json_bytes(transcript))

        def _cancel_agent_run(self, run_id: str) -> None:
            local_cancel_requested = _cancel_local_agent_task(run_id)
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    run = agent_activity.get_run(conn, run_id)
                    if run is None:
                        self._send_bytes(
                            HTTPStatus.NOT_FOUND,
                            _json_bytes({"error": f"unknown run_id: {run_id}"}),
                        )
                        return
                    status = str(run.get("status") or "").lower()
                    if status in agent_activity.STOPPED_STATUSES:
                        self._send_bytes(
                            HTTPStatus.CONFLICT,
                            _json_bytes(
                                {
                                    "error": "run already terminal",
                                    "run": run,
                                }
                            ),
                        )
                        return
                    event = agent_activity.record_event(
                        conn,
                        run_id=run_id,
                        event_type="status",
                        message=(
                            "Run cancelled from graph UI; local command adapter signalled."
                            if local_cancel_requested
                            else "Run cancelled from graph UI."
                        ),
                        payload={
                            "status": "cancelled",
                            "local_cancel_requested": local_cancel_requested,
                        },
                    )
                    updated = agent_activity.get_run(conn, run_id)
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": updated,
                        "event": event,
                        "local_cancel_requested": local_cancel_requested,
                    }
                ),
            )

        def _archive_agent_run(self, run_id: str) -> None:
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    run = agent_activity.get_run(conn, run_id)
                    if run is None:
                        self._send_bytes(
                            HTTPStatus.NOT_FOUND,
                            _json_bytes({"error": f"unknown run_id: {run_id}"}),
                        )
                        return
                    event = agent_activity.record_event(
                        conn,
                        run_id=run_id,
                        event_type="status",
                        message="Run archived from graph UI.",
                        payload={"archived": True},
                    )
                    updated = agent_activity.archive_run(conn, run_id=run_id)
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes({"ok": True, "run": updated, "event": event}),
            )

        def _callback_base_url(self) -> str:
            host = self.headers.get("Host")
            if not host:
                address = self.server.server_address  # type: ignore[attr-defined]
                host = f"{address[0]}:{address[1]}"
            return f"http://{host}"

        def _start_agent_run(self, payload: dict[str, Any]) -> None:
            try:
                request = _task_request_from_payload(payload, args)
            except ValueError as exc:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": str(exc)}),
                )
                return
            if not request["message"]:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "message is required"}),
                )
                return
            planned_run_id = secrets.token_hex(16)
            swarm_config = (
                request.get("swarm") if isinstance(request.get("swarm"), dict) else {}
            )
            swarm_enabled = bool(swarm_config.get("enabled"))
            swarm_child_runs: list[dict[str, Any]] = []
            swarm_child_events: list[dict[str, Any]] = []
            swarm_child_specs: list[dict[str, Any]] = []
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    preflight_draft = _build_task_draft(
                        config,
                        request,
                        callback_base_url=self._callback_base_url(),
                    )
                    try:
                        with db_mod.transaction(conn):
                            active_claims = agent_activity.active_file_claims(
                                conn,
                                limit=200,
                            )
                            try:
                                blocking_runs = agent_activity.blocking_runs(
                                    conn,
                                    run_ids=request.get("blocked_by_run_ids") or [],
                                )
                            except ValueError as exc:
                                self._send_bytes(
                                    HTTPStatus.BAD_REQUEST,
                                    _json_bytes({"error": str(exc)}),
                                )
                                return
                            current_preflight = _preflight_from_draft(
                                request=request,
                                draft=preflight_draft,
                                active_claims=active_claims,
                                blocking_runs=blocking_runs,
                            )
                            rejection = _consume_preflight(
                                conn,
                                payload=payload,
                                request=request,
                                draft=preflight_draft,
                                preflight=current_preflight,
                                run_id=planned_run_id,
                            )
                            if rejection is not None:
                                status, body = rejection
                                _inc_counter(
                                    perf_state,
                                    "preflight_rejections",
                                    str(body.get("error") or "unknown"),
                                )
                                self._send_bytes(status, _json_bytes(body))
                                return
                            initial_status = (
                                "blocked"
                                if blocking_runs
                                else ("working" if swarm_enabled else "queued")
                            )
                            run_metadata = dict(request["metadata"])
                            if swarm_enabled:
                                run_metadata = agent_swarm.swarm_parent_metadata(
                                    run_metadata,
                                    swarm_config,
                                )
                                request = dict(request)
                                request["metadata"] = run_metadata
                            run = agent_activity.start_run(
                                conn,
                                run_id=planned_run_id,
                                agent_name=request["agent_name"],
                                prompt=request["message"],
                                selected_nodes=request["selected_nodes"],
                                metadata=run_metadata,
                                status=initial_status,
                            )
                            if request.get("blocked_by_run_ids"):
                                agent_activity.add_run_blockers(
                                    conn,
                                    run_id=run["run_id"],
                                    blocked_by_run_ids=request["blocked_by_run_ids"],
                                    reason=(
                                        "Task waits for blocker run(s) before dispatch."
                                    ),
                                    metadata={"source": "graph-task"},
                                )
                                run = agent_activity.get_run(conn, run["run_id"]) or run
                            event = agent_activity.record_event(
                                conn,
                                run_id=run["run_id"],
                                event_type="task",
                                file_path=(
                                    request["selected_paths"][0]
                                    if request["selected_paths"]
                                    else request["node"].get("path")
                                ),
                                message=request["message"],
                                payload={
                                    "status": run.get("status") or "queued",
                                    "provider": request["provider"] or None,
                                    "execution_strategy": request.get(
                                        "execution_strategy"
                                    ),
                                    "swarm": agent_swarm.public_swarm_config(
                                        swarm_config
                                    ),
                                    "selected_nodes": request["selected_nodes"],
                                    "selected_paths": request["selected_paths"],
                                    "node": request["node"],
                                    "preflight_confirmed": request[
                                        "preflight_confirmed"
                                    ],
                                    "blocked_by_run_ids": request.get(
                                        "blocked_by_run_ids"
                                    )
                                    or [],
                                },
                            )
                            if request["selected_paths"]:
                                agent_activity.claim_files(
                                    conn,
                                    run_id=run["run_id"],
                                    file_paths=request["selected_paths"],
                                    mode="review",
                                    reason="Graph task selected file.",
                                    metadata={"source": "graph-task"},
                                )
                            run = agent_activity.get_run(conn, run["run_id"])
                            if swarm_enabled and not blocking_runs and run:
                                swarm_child_specs = agent_swarm.child_run_specs(
                                    request,
                                    parent_run_id=run["run_id"],
                                    swarm=swarm_config,
                                )
                                for spec in swarm_child_specs:
                                    child_run = agent_activity.start_run(
                                        conn,
                                        run_id=secrets.token_hex(16),
                                        agent_name=str(spec["agent_name"]),
                                        prompt=str(spec["prompt"]),
                                        selected_nodes=request["selected_nodes"],
                                        metadata=spec.get("metadata") or {},
                                        status="queued",
                                    )
                                    child_role = spec.get("role") or {}
                                    child_event = agent_activity.record_event(
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
                                            "parent_run_id": run["run_id"],
                                            "swarm": (
                                                spec.get("metadata") or {}
                                            ).get("swarm"),
                                            "swarm_role": child_role,
                                            "selected_nodes": request["selected_nodes"],
                                            "selected_paths": request["selected_paths"],
                                        },
                                    )
                                    if request["selected_paths"]:
                                        agent_activity.claim_files(
                                            conn,
                                            run_id=child_run["run_id"],
                                            file_paths=request["selected_paths"],
                                            mode=str(
                                                spec.get("claim_mode") or "review"
                                            ),
                                            reason=(
                                                "Agent Swarm child selected file."
                                            ),
                                            metadata={
                                                "source": "agent-swarm",
                                                "parent_run_id": run["run_id"],
                                                "role": child_role.get("role"),
                                            },
                                        )
                                    swarm_child_runs.append(
                                        agent_activity.get_run(conn, child_run["run_id"])
                                        or child_run
                                    )
                                    swarm_child_events.append(child_event)
                    except ValueError as exc:
                        http_status = (
                            HTTPStatus.CONFLICT
                            if str(exc).startswith("claim conflict:")
                            else HTTPStatus.BAD_REQUEST
                        )
                        self._send_bytes(
                            http_status,
                            _json_bytes({"error": str(exc)}),
                        )
                        return
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            for child_event in swarm_child_events:
                append_event_jsonl(config.root, child_event)
            task = _build_task_draft(
                config,
                request,
                callback_base_url=self._callback_base_url(),
            )
            task["kind"] = "code_index_agent_task"
            task["run_id"] = run["run_id"] if run else event["run_id"]
            if isinstance(payload.get("preflight"), dict):
                task["preflight"] = payload["preflight"]
            task["context_packet"] = _build_task_context_packet(
                config,
                message=request["message"],
                selected_nodes=request["selected_nodes"],
                selected_paths=request["selected_paths"],
            )
            graph_context = task["graph_context"]
            if isinstance(task.get("context_packet"), dict):
                task["context_packet"]["graph_context"] = graph_context
            collaboration = _build_task_collaboration_packet(
                config,
                run_id=str(task["run_id"]),
                agent_name=request["agent_name"],
                selected_nodes=request["selected_nodes"],
                selected_paths=request["selected_paths"],
                node=request["node"],
            )
            task["collaboration"] = collaboration
            if isinstance(task.get("context_packet"), dict):
                task["context_packet"]["collaboration"] = collaboration
            if blocking_runs:
                dispatch = {
                    "configured": False,
                    "status": "blocked",
                    "reason": "blocked_by_run_ids are not complete",
                    "blocked_by_run_ids": request.get("blocked_by_run_ids") or [],
                }
            elif swarm_enabled:
                swarm_tasks: list[dict[str, Any]] = []
                child_dispatches: list[dict[str, Any]] = []
                for child_run, spec in zip(swarm_child_runs, swarm_child_specs):
                    child_request = dict(request)
                    child_request.update(
                        {
                            "agent_name": spec["agent_name"],
                            "provider": spec["provider"],
                            "message": spec["prompt"],
                            "parent_run_id": task["run_id"],
                            "metadata": spec.get("metadata") or {},
                            "execution_strategy": "single",
                            "swarm": {"enabled": False},
                        }
                    )
                    child_task = _build_task_draft(
                        config,
                        child_request,
                        callback_base_url=self._callback_base_url(),
                    )
                    child_task["kind"] = "code_index_agent_task"
                    child_task["run_id"] = child_run["run_id"]
                    child_task["parent_run_id"] = task["run_id"]
                    child_task["execution_strategy"] = "swarm_child"
                    child_task["swarm"] = {
                        "parent_run_id": task["run_id"],
                        "role": spec.get("role"),
                        "provider": spec.get("provider"),
                    }
                    child_task["context_packet"] = _build_task_context_packet(
                        config,
                        message=str(spec["prompt"]),
                        selected_nodes=request["selected_nodes"],
                        selected_paths=request["selected_paths"],
                    )
                    if isinstance(child_task.get("context_packet"), dict):
                        child_task["context_packet"]["graph_context"] = child_task[
                            "graph_context"
                        ]
                    child_collaboration = _build_task_collaboration_packet(
                        config,
                        run_id=str(child_task["run_id"]),
                        agent_name=str(spec["agent_name"]),
                        selected_nodes=request["selected_nodes"],
                        selected_paths=request["selected_paths"],
                        node=request["node"],
                    )
                    child_task["collaboration"] = child_collaboration
                    if isinstance(child_task.get("context_packet"), dict):
                        child_task["context_packet"][
                            "collaboration"
                        ] = child_collaboration
                    child_dispatch = _dispatch_agent_task(config, child_task)
                    if (
                        child_dispatch.get("configured")
                        and child_dispatch.get("status") == "sent"
                    ):
                        with writer_lock(config):
                            conn = db_mod.connect(config.db_path)
                            try:
                                db_mod.apply_schema(conn)
                                dispatch_event = agent_activity.record_event(
                                    conn,
                                    run_id=str(child_task["run_id"]),
                                    event_type="status",
                                    message="Swarm child dispatched to agent webhook.",
                                    payload={
                                        "status": "working",
                                        "dispatch": child_dispatch,
                                    },
                                )
                                append_event_jsonl(config.root, dispatch_event)
                            finally:
                                db_mod.close(conn)
                    swarm_tasks.append(child_task)
                    child_dispatches.append(
                        {
                            "run_id": child_task["run_id"],
                            "role": (spec.get("role") or {}).get("role"),
                            "dispatch": child_dispatch,
                        }
                    )
                if swarm_child_runs:
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.ensure_schema(conn, config)
                        swarm_child_runs = [
                            agent_activity.get_run(conn, str(child["run_id"])) or child
                            for child in swarm_child_runs
                        ]
                    finally:
                        db_mod.close(conn)
                task["swarm_children"] = [
                    {
                        "run_id": child.get("run_id"),
                        "agent_name": child.get("agent_name"),
                        "status": child.get("status"),
                        "role": (spec.get("role") or {}).get("role"),
                        "provider": spec.get("provider"),
                    }
                    for child, spec in zip(swarm_child_runs, swarm_child_specs)
                ]
                any_configured = any(
                    bool(item["dispatch"].get("configured"))
                    for item in child_dispatches
                )
                dispatch = {
                    "configured": any_configured,
                    "status": "swarm_started" if any_configured else "not_configured",
                    "transport": "swarm",
                    "children": child_dispatches,
                    "task_count": len(swarm_tasks),
                }
            else:
                dispatch = _dispatch_agent_task(config, task)
            if dispatch.get("configured") and dispatch.get("status") == "sent":
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        dispatch_event = agent_activity.record_event(
                            conn,
                            run_id=task["run_id"],
                            event_type="status",
                            message=(
                                "Task dispatched to local command adapter."
                                if dispatch.get("transport") == "local-command"
                                else "Task dispatched to agent webhook."
                            ),
                            payload={"status": "working", "dispatch": dispatch},
                        )
                        run = agent_activity.get_run(conn, task["run_id"])
                    finally:
                        db_mod.close(conn)
                append_event_jsonl(config.root, dispatch_event)
            elif (
                dispatch.get("configured")
                and dispatch.get("transport") == "local-command"
            ):
                with writer_lock(config):
                    conn = db_mod.connect(config.db_path)
                    try:
                        db_mod.apply_schema(conn)
                        run = agent_activity.get_run(conn, task["run_id"])
                    finally:
                        db_mod.close(conn)
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                activity = run_orchestrator.snapshot(conn, limit=80)
                board = activity.get("kanban") or agent_activity.kanban_board(
                    conn, limit=25
                )
                board["orchestrator"] = activity.get("orchestrator")
            finally:
                db_mod.close(conn)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": run,
                        "event": event,
                        "dispatch": dispatch,
                        "task": task,
                        "board": board,
                    }
                ),
            )

        def _record_agent_event(self, payload: dict[str, Any]) -> None:
            event_type = str(payload.get("event_type") or payload.get("type") or "")
            if not event_type:
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    _json_bytes({"error": "event_type is required"}),
                )
                return
            agent_name = str(payload.get("agent_name") or "Agent")
            with writer_lock(config):
                conn = db_mod.connect(config.db_path)
                try:
                    db_mod.apply_schema(conn)
                    event_payload = payload.get("payload") or {}
                    if not isinstance(event_payload, dict):
                        self._send_bytes(
                            HTTPStatus.BAD_REQUEST,
                            _json_bytes({"error": "payload must be a JSON object"}),
                        )
                        return
                    run_id = payload.get("run_id") or event_payload.get("run_id")
                    run = (
                        agent_activity.get_run(conn, str(run_id))
                        if run_id
                        else agent_activity.latest_active_run(conn, agent_name=agent_name)
                    )
                    if run is None:
                        prompt = str(payload.get("prompt") or "").strip()
                        if run_id:
                            self._send_bytes(
                                HTTPStatus.NOT_FOUND,
                                _json_bytes({"error": f"unknown run_id: {run_id}"}),
                            )
                            return
                        if not prompt:
                            self._send_bytes(
                                HTTPStatus.OK,
                                _json_bytes(
                                    {
                                        "ok": True,
                                        "ignored": True,
                                        "reason": (
                                            "run_id is required when no active run can "
                                            "receive this event"
                                        ),
                                        "run": None,
                                        "event": None,
                                    }
                                ),
                            )
                            return
                        run = agent_activity.start_run(
                            conn,
                            agent_name=agent_name,
                            prompt=prompt,
                            metadata={"source": "graph-server"},
                        )
                    if payload.get("status"):
                        event_payload["status"] = str(payload.get("status"))
                    event = agent_activity.record_event(
                        conn,
                        run_id=run["run_id"],
                        event_type=event_type,
                        file_path=payload.get("file_path") or payload.get("file"),
                        symbol_path=payload.get("symbol_path"),
                        message=payload.get("message"),
                        payload=event_payload,
                    )
                    updated_run = agent_activity.get_run(conn, event["run_id"])
                    swarm_reconciliation = None
                    if str((updated_run or {}).get("status") or "").lower() in (
                        agent_activity.STOPPED_STATUSES
                    ):
                        metadata = (
                            updated_run.get("metadata")
                            if isinstance((updated_run or {}).get("metadata"), dict)
                            else {}
                        )
                        swarm_metadata = (
                            metadata.get("swarm")
                            if isinstance(metadata.get("swarm"), dict)
                            else {}
                        )
                        parent_run_id = metadata.get(
                            "parent_run_id"
                        ) or swarm_metadata.get("parent_run_id")
                        if parent_run_id:
                            try:
                                swarm_reconciliation = (
                                    agent_swarm.reconcile_swarm_parent(
                                        conn,
                                        parent_run_id=str(parent_run_id),
                                    )
                                )
                            except ValueError as exc:
                                swarm_reconciliation = {"error": str(exc)}
                            updated_run = agent_activity.get_run(conn, event["run_id"])
                    suggestions_event = None
                    suggestions = None
                    if str((updated_run or {}).get("status") or "").lower() in {
                        "completed",
                        "failed",
                    }:
                        suggestions_event = agent_activity.record_run_suggestions(
                            conn, run_id=event["run_id"]
                        )
                        suggestions = agent_activity.build_run_suggestions(
                            conn, event["run_id"]
                        )
                finally:
                    db_mod.close(conn)
            append_event_jsonl(config.root, event)
            append_event_jsonl(config.root, suggestions_event)
            self._send_bytes(
                HTTPStatus.OK,
                _json_bytes(
                    {
                        "ok": True,
                        "run": updated_run,
                        "event": event,
                        "swarm_reconciliation": swarm_reconciliation,
                        "suggestions_event": suggestions_event,
                        "suggestions": suggestions,
                    }
                ),
            )

        def _stream_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_event_pk: int | None = None
            last_notes_mtime: int | None = None
            interval = max(0.25, float(getattr(args, "event_interval", 1.0) or 1.0))
            while True:
                try:
                    signature = _state_signature(config)
                    sent = False
                    if signature["event_pk"] != last_event_pk:
                        last_event_pk = signature["event_pk"]
                        data = json.dumps(_agent_stream_payload(config))
                        self.wfile.write(f"event: agent\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                        sent = True
                    if signature["notes_mtime"] != last_notes_mtime:
                        last_notes_mtime = signature["notes_mtime"]
                        data = json.dumps({"type": "graph", **signature})
                        self.wfile.write(f"event: graph\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                        sent = True
                    data = json.dumps(_perf_tick_payload(perf_state))
                    self.wfile.write(f"event: perf:tick\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    sent = True
                    if not sent:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    time.sleep(interval)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    _inc_counter(perf_state, "sse_dropped_events")
                    break

    return GraphHandler
