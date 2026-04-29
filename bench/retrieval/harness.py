"""Broker-vs-ripgrep retrieval benchmark core."""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_index import retrieval


HERE = Path(__file__).resolve().parent
DEFAULT_CASES_PATH = HERE / "cases.jsonl"
DEFAULT_K = 20
DEFAULT_BUDGET_BYTES = 50_000
DEFAULT_SOURCES = (
    retrieval.SourceKind.FILE_PATH,
    retrieval.SourceKind.CODE_CHUNK,
    retrieval.SourceKind.TRANSCRIPT_EVENT,
)

STOPWORDS = {
    "about",
    "adding",
    "after",
    "against",
    "and",
    "are",
    "budget",
    "byte",
    "check",
    "code",
    "context",
    "file",
    "files",
    "find",
    "for",
    "from",
    "into",
    "remaining",
    "selected",
    "shared",
    "the",
    "tool",
    "uses",
    "with",
}
SHORT_KEEPERS = {"db", "fts", "mcp", "rg", "nl"}


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    query: str
    expected: tuple[tuple[str, str], ...]
    group: str = "default"
    limit: int | None = None
    budget_bytes: int | None = None
    sources: tuple[str, ...] = ()


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[RetrievalCase]:
    cases: list[RetrievalCase] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSONL case: {exc}") from exc
        cases.append(_parse_case(item, path=path, lineno=lineno))
    if not cases:
        raise ValueError(f"{path}: no retrieval benchmark cases found")
    return cases


def run_benchmark(
    conn: sqlite3.Connection,
    corpus_root: Path,
    cases: list[RetrievalCase],
    *,
    k: int = DEFAULT_K,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    rg_path: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    resolved_rg = _resolve_rg_path(rg_path)
    case_rows = []
    for case in cases:
        broker = run_broker_case(
            conn,
            case,
            k=k,
            budget_bytes=budget_bytes,
        )
        rg = run_rg_case(
            corpus_root,
            case,
            k=k,
            rg_path=resolved_rg,
        )
        case_rows.append(
            {
                "id": case.id,
                "group": case.group,
                "query": case.query,
                "expected": [
                    {"kind": kind, "id": ident} for kind, ident in case.expected
                ],
                "broker": broker,
                "ripgrep": rg,
                "diff": _case_diff(case.expected, broker, rg),
            }
        )

    aggregate = {
        "broker": _aggregate_mode(case_rows, "broker"),
        "ripgrep": _aggregate_mode(case_rows, "ripgrep"),
    }
    return {
        "kind": "retrieval_broker_vs_ripgrep_benchmark",
        "case_count": len(case_rows),
        "k": int(k),
        "budget_bytes": int(budget_bytes),
        "corpus_root": str(corpus_root),
        "rg_path": resolved_rg,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "aggregate": aggregate,
        "cases": case_rows,
    }


def run_broker_case(
    conn: sqlite3.Connection,
    case: RetrievalCase,
    *,
    k: int = DEFAULT_K,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
) -> dict[str, Any]:
    limit = int(case.limit or k)
    request_budget = int(case.budget_bytes or budget_bytes)
    sources = _case_sources(case)
    started = time.perf_counter()
    response = retrieval.retrieve(
        conn,
        retrieval.RetrievalRequest(
            query=case.query,
            limit=limit,
            budget_bytes=request_budget,
            sources=sources,
            per_source_limit=max(limit * 2, limit, 1),
        ),
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    raw_results = [result.to_dict() for result in response.results]
    ranked = _dedupe_ranked_hits(_broker_ranked_hits(raw_results, case.expected))
    return _pack_mode_result(
        expected=case.expected,
        ranked=ranked,
        raw_results=raw_results,
        elapsed_ms=elapsed_ms,
        extra={
            "bytes_used": response.bytes_used,
            "candidate_count": response.candidate_count,
            "truncation_reason": response.truncation_reason.value,
        },
    )


def run_rg_case(
    corpus_root: Path,
    case: RetrievalCase,
    *,
    k: int = DEFAULT_K,
    rg_path: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    terms = _query_terms(case.query)
    resolved_rg = _resolve_rg_path(rg_path)
    if not resolved_rg:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return _pack_mode_result(
            expected=case.expected,
            ranked=[],
            raw_results=[],
            elapsed_ms=elapsed_ms,
            extra={"engine": "ripgrep", "status": "unavailable", "terms": terms},
        )

    path_scores = _rg_path_scores(corpus_root, resolved_rg, terms, case.query)
    content_scores = _rg_content_scores(corpus_root, resolved_rg, terms)
    merged = _merge_rg_scores(path_scores, content_scores)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    ranked = [
        (("file", path), {"kind": "file", "id": path, **data})
        for path, data in merged[:k]
    ]
    return _pack_mode_result(
        expected=case.expected,
        ranked=ranked,
        raw_results=[payload for _key, payload in ranked],
        elapsed_ms=elapsed_ms,
        extra={
            "engine": "ripgrep",
            "status": "ok",
            "terms": terms,
            "candidate_count": len(merged),
        },
    )


def _parse_case(item: dict[str, Any], *, path: Path, lineno: int) -> RetrievalCase:
    if not isinstance(item, dict):
        raise ValueError(f"{path}:{lineno}: case must be a JSON object")
    case_id = str(item.get("id") or "").strip()
    query = str(item.get("query") or "").strip()
    expected_items = item.get("expected")
    if not case_id or not query or not isinstance(expected_items, list):
        raise ValueError(f"{path}:{lineno}: case requires id, query, and expected[]")
    expected: list[tuple[str, str]] = []
    for expected_item in expected_items:
        if not isinstance(expected_item, dict):
            raise ValueError(f"{path}:{lineno}: expected item must be an object")
        kind = str(expected_item.get("kind") or "").strip()
        ident = _normalize_path(str(expected_item.get("id") or "").strip())
        if not kind or not ident:
            raise ValueError(f"{path}:{lineno}: expected item requires kind and id")
        expected.append((kind, ident))
    sources = tuple(str(source) for source in item.get("sources") or ())
    return RetrievalCase(
        id=case_id,
        group=str(item.get("group") or "default"),
        query=query,
        expected=tuple(dict.fromkeys(expected)),
        limit=_optional_int(item.get("limit")),
        budget_bytes=_optional_int(item.get("budget_bytes")),
        sources=sources,
    )


def _case_sources(
    case: RetrievalCase,
) -> tuple[retrieval.SourceKind | str, ...]:
    return case.sources or DEFAULT_SOURCES


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _resolve_rg_path(candidate: str | None) -> str | None:
    if candidate:
        found = shutil.which(candidate)
        if found:
            return found
        if Path(candidate).exists():
            return candidate
        return None
    return shutil.which("rg")


def _broker_ranked_hits(
    raw_results: list[dict[str, Any]],
    expected: tuple[tuple[str, str], ...],
) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    ranked: list[tuple[tuple[str, str], dict[str, Any]]] = []
    expected_set = set(expected)
    for rank, result in enumerate(raw_results, 1):
        result_kind = str(result.get("kind") or "")
        result_id = _normalize_path(str(result.get("id") or ""))
        file_path = _normalize_path(
            str(result.get("file_path") or result.get("payload", {}).get("file_path") or "")
        )
        payload = {
            "rank": rank,
            "kind": result_kind,
            "id": result_id,
            "handle": result.get("handle"),
            "source_kind": result.get("source_kind"),
            "file_path": file_path or None,
            "score": result.get("score"),
        }
        exact_key = (result_kind, result_id)
        file_key = ("file", file_path)
        if exact_key in expected_set and result_kind and result_id:
            ranked.append((exact_key, payload))
        elif file_path:
            ranked.append((file_key, {**payload, "kind": "file", "id": file_path}))
        elif result_kind and result_id:
            ranked.append((exact_key, payload))
    return ranked


def _pack_mode_result(
    *,
    expected: tuple[tuple[str, str], ...],
    ranked: list[tuple[tuple[str, str], dict[str, Any]]],
    raw_results: list[dict[str, Any]],
    elapsed_ms: float,
    extra: dict[str, Any],
) -> dict[str, Any]:
    deduped = [
        (
            key,
            {
                **payload,
                "rank": int(payload.get("rank") or rank),
            },
        )
        for rank, (key, payload) in enumerate(_dedupe_ranked_hits(ranked), 1)
    ]
    expected_set = set(expected)
    returned_keys = [key for key, _payload in deduped]
    hit_ranks = [
        rank
        for rank, key in enumerate(returned_keys, 1)
        if key in expected_set
    ]
    found = [key for key in expected if key in set(returned_keys)]
    recall = len(found) / len(expected) if expected else 0.0
    precision = len(hit_ranks) / len(returned_keys) if returned_keys else 0.0
    mrr = (1.0 / hit_ranks[0]) if hit_ranks else 0.0
    return {
        "recall_at_k": round(recall, 4),
        "precision_at_k": round(precision, 4),
        "mrr": round(mrr, 4),
        "latency_ms": elapsed_ms,
        "found_count": len(found),
        "returned_count": len(returned_keys),
        "found": [{"kind": kind, "id": ident} for kind, ident in found],
        "missed": [
            {"kind": kind, "id": ident}
            for kind, ident in expected
            if (kind, ident) not in set(returned_keys)
        ],
        "top_results": [payload for _key, payload in deduped[:10]],
        "raw_results": raw_results[:10],
        **extra,
    }


def _dedupe_ranked_hits(
    ranked: list[tuple[tuple[str, str], dict[str, Any]]],
) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    out: list[tuple[tuple[str, str], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for key, payload in ranked:
        normalized_key = (key[0], _normalize_path(key[1]))
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        out.append((normalized_key, payload))
    return out


def _case_diff(
    expected: tuple[tuple[str, str], ...],
    broker: dict[str, Any],
    rg: dict[str, Any],
) -> dict[str, Any]:
    broker_ranks = _ranks_by_key(broker["top_results"])
    rg_ranks = _ranks_by_key(rg["top_results"])
    rows = []
    for key in expected:
        broker_rank = broker_ranks.get(key)
        rg_rank = rg_ranks.get(key)
        if broker_rank is not None and rg_rank is not None:
            status = "both"
        elif broker_rank is not None:
            status = "broker_only"
        elif rg_rank is not None:
            status = "ripgrep_only"
        else:
            status = "missed"
        rows.append(
            {
                "kind": key[0],
                "id": key[1],
                "broker_rank": broker_rank,
                "ripgrep_rank": rg_rank,
                "rank_delta": (
                    broker_rank - rg_rank
                    if broker_rank is not None and rg_rank is not None
                    else None
                ),
                "status": status,
            }
        )
    return {
        "expected": rows,
        "broker_recall_minus_ripgrep": round(
            float(broker["recall_at_k"]) - float(rg["recall_at_k"]),
            4,
        ),
        "broker_mrr_minus_ripgrep": round(
            float(broker["mrr"]) - float(rg["mrr"]),
            4,
        ),
    }


def _ranks_by_key(results: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for fallback_rank, result in enumerate(results, 1):
        key = (str(result.get("kind") or ""), _normalize_path(str(result.get("id") or "")))
        if key[0] and key[1] and key not in out:
            out[key] = int(result.get("rank") or fallback_rank)
    return out


def _aggregate_mode(case_rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    rows = [case[mode] for case in case_rows]
    expected_total = sum(
        len(case.get("expected", ())) for case in case_rows
    )
    found_total = sum(int(row["found_count"]) for row in rows)
    returned_total = sum(int(row["returned_count"]) for row in rows)
    latencies = [float(row["latency_ms"]) for row in rows]
    return {
        "macro": {
            "recall_at_k": _mean(row["recall_at_k"] for row in rows),
            "precision_at_k": _mean(row["precision_at_k"] for row in rows),
            "mrr": _mean(row["mrr"] for row in rows),
        },
        "micro": {
            "recall_at_k": round(found_total / expected_total, 4)
            if expected_total
            else 0.0,
            "precision_at_k": round(found_total / returned_total, 4)
            if returned_total
            else 0.0,
            "expected_total": expected_total,
            "found_total": found_total,
            "returned_total": returned_total,
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
    }


def _mean(values: Any) -> float:
    nums = [float(value) for value in values]
    return round(sum(nums) / len(nums), 4) if nums else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * pct) - 1))
    return round(ordered[index], 3)


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_./:-]+", query.lower()):
        normalized = token.strip("._:-/")
        if not normalized:
            continue
        candidates = [normalized]
        if any(sep in normalized for sep in "/.:-"):
            candidates.extend(
                part for part in re.split(r"[/.:\\-]+", normalized) if part
            )
        for candidate in candidates:
            if candidate in STOPWORDS:
                continue
            if len(candidate) < 3 and candidate not in SHORT_KEEPERS:
                continue
            if candidate not in terms:
                terms.append(candidate)
    return terms[:16]


def _rg_path_scores(
    corpus_root: Path,
    rg_path: str,
    terms: list[str],
    query: str,
) -> dict[str, dict[str, Any]]:
    proc = subprocess.run(
        [rg_path, "--files"],
        cwd=corpus_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    scores: dict[str, dict[str, Any]] = {}
    if proc.returncode not in (0, 1):
        return scores
    query_norm = _normalize_path(query.lower())
    for raw_path in proc.stdout.splitlines():
        path = _normalize_path(raw_path)
        path_lower = path.lower()
        score = 0.0
        matched_terms: list[str] = []
        if query_norm and path_lower == query_norm:
            score += 100.0
        elif query_norm and (query_norm in path_lower or path_lower in query_norm):
            score += 35.0
        for term in terms:
            if term in path_lower:
                score += 5.0
                matched_terms.append(term)
        if score:
            scores[path] = {
                "path_score": score,
                "content_score": 0.0,
                "matched_terms": sorted(set(matched_terms)),
                "match_count": 0,
                "matched_lines": 0,
            }
    return scores


def _rg_content_scores(
    corpus_root: Path,
    rg_path: str,
    terms: list[str],
) -> dict[str, dict[str, Any]]:
    if not terms:
        return {}
    args = [
        rg_path,
        "--json",
        "--line-number",
        "--ignore-case",
        "--fixed-strings",
    ]
    for term in terms:
        args.extend(("-e", term))
    args.append(".")
    proc = subprocess.run(
        args,
        cwd=corpus_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    scores: dict[str, dict[str, Any]] = {}
    if proc.returncode not in (0, 1):
        return scores
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path = _normalize_path(str((data.get("path") or {}).get("text") or ""))
        if not path:
            continue
        entry = scores.setdefault(
            path,
            {
                "path_score": 0.0,
                "content_score": 0.0,
                "matched_terms": set(),
                "match_count": 0,
                "matched_lines": 0,
            },
        )
        entry["matched_lines"] += 1
        submatches = data.get("submatches") or []
        if not submatches:
            entry["content_score"] += 0.5
            continue
        for submatch in submatches:
            matched = str((submatch.get("match") or {}).get("text") or "").lower()
            if matched:
                entry["matched_terms"].add(matched)
                entry["match_count"] += 1
                entry["content_score"] += 1.0
    for entry in scores.values():
        matched_terms = sorted(entry["matched_terms"])
        entry["matched_terms"] = matched_terms
        entry["content_score"] += len(matched_terms) * 4.0
    return scores


def _merge_rg_scores(
    path_scores: dict[str, dict[str, Any]],
    content_scores: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in (path_scores, content_scores):
        for path, data in source.items():
            entry = merged.setdefault(
                path,
                {
                    "score": 0.0,
                    "path_score": 0.0,
                    "content_score": 0.0,
                    "matched_terms": set(),
                    "match_count": 0,
                    "matched_lines": 0,
                },
            )
            entry["path_score"] += float(data.get("path_score") or 0.0)
            entry["content_score"] += float(data.get("content_score") or 0.0)
            entry["matched_terms"].update(data.get("matched_terms") or [])
            entry["match_count"] += int(data.get("match_count") or 0)
            entry["matched_lines"] += int(data.get("matched_lines") or 0)
    for entry in merged.values():
        entry["matched_terms"] = sorted(entry["matched_terms"])
        entry["score"] = round(
            float(entry["path_score"])
            + float(entry["content_score"])
            + min(int(entry["matched_lines"]), 20) * 0.1,
            4,
        )
    return sorted(
        merged.items(),
        key=lambda item: (-float(item[1]["score"]), item[0]),
    )


def _normalize_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").removeprefix("./")
