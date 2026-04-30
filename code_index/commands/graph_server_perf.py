"""Performance counters and latency observation for the live graph server."""

from __future__ import annotations

import json
import threading
from typing import Any


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
    from code_index.commands.graph_server_utils import _now_iso

    return {
        "kind": "code_index_graph_debug_perf",
        "generated_at": _now_iso(),
        "counters": counters,
    }


def _perf_tick_payload(perf: dict[str, Any]) -> dict[str, Any]:
    payload = _perf_snapshot(perf)
    payload["type"] = "perf:tick"
    return payload
