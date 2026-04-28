"""Small retrieval evaluation harness for local context changes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import retrieval

DEFAULT_EVAL_FILE = Path(__file__).parent / "evals" / "retrieval_eval.json"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("retrieval eval file must contain a JSON array")
    cases: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("retrieval eval case must be an object")
        query = str(item.get("query") or "").strip()
        expected = item.get("expected")
        if not query or not isinstance(expected, list) or not expected:
            raise ValueError("retrieval eval case requires query and expected[]")
        cases.append(item)
    return cases


def _expected_keys(case: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for expected in case.get("expected") or []:
        if not isinstance(expected, dict):
            continue
        kind = str(expected.get("kind") or "").strip()
        ident = str(expected.get("id") or "").strip()
        if kind and ident:
            keys.add((kind, ident))
    return keys


def run_eval(
    config: cfg_mod.Config,
    *,
    eval_file: Path | None = None,
    limit: int = 10,
    budget_bytes: int = 20_000,
) -> dict[str, Any]:
    """Evaluate current local retrieval against a tiny checked-in golden set."""

    path = eval_file or DEFAULT_EVAL_FILE
    cases = _load_cases(path)
    started = time.perf_counter()
    conn = db_mod.connect(config.db_path)
    case_results: list[dict[str, Any]] = []
    try:
        db_mod.ensure_schema(conn, config)
        for case in cases:
            case_started = time.perf_counter()
            query = str(case["query"])
            expected = _expected_keys(case)
            response = retrieval.retrieve(
                conn,
                retrieval.RetrievalRequest(
                    query=query,
                    limit=max(0, int(limit)),
                    budget_bytes=max(0, int(budget_bytes)),
                    sources=(
                        retrieval.SourceKind.FILE_PATH,
                        retrieval.SourceKind.CODE_CHUNK,
                    ),
                    per_source_limit=max(0, int(limit)),
                ),
            )
            hits = [result.to_dict() for result in response.results]
            hit_keys = {(str(hit["kind"]), str(hit["id"])) for hit in hits}
            found = sorted(expected & hit_keys)
            case_results.append(
                {
                    "id": case.get("id") or query,
                    "query": query,
                    "expected_count": len(expected),
                    "found_count": len(found),
                    "recall": (len(found) / len(expected)) if expected else 0.0,
                    "precision": (len(found) / len(hits)) if hits else 0.0,
                    "bytes_used": response.bytes_used,
                    "latency_ms": round((time.perf_counter() - case_started) * 1000, 2),
                    "found": [{"kind": kind, "id": ident} for kind, ident in found],
                    "top_results": hits[: min(5, len(hits))],
                }
            )
    finally:
        db_mod.close(conn)

    total_expected = sum(int(case["expected_count"]) for case in case_results)
    total_found = sum(int(case["found_count"]) for case in case_results)
    total_returned = sum(len(case["top_results"]) for case in case_results)
    bytes_values = sorted(int(case["bytes_used"]) for case in case_results)
    p95_index = max(0, min(len(bytes_values) - 1, int(len(bytes_values) * 0.95)))
    return {
        "kind": "code_index_retrieval_eval",
        "eval_file": str(path),
        "case_count": len(case_results),
        "limit": int(limit),
        "budget_bytes": int(budget_bytes),
        "recall_at_limit": (total_found / total_expected) if total_expected else 0.0,
        "precision_at_limit": (total_found / total_returned) if total_returned else 0.0,
        "bytes_p95": bytes_values[p95_index] if bytes_values else 0,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "cases": case_results,
    }
