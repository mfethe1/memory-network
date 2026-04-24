"""Embedding relevance benchmark.

Drives BM25 (`code_index.search.fts.search`), embeddings
(`code_index.embeddings.semantic_search`), and a union+rerank hybrid
against a fixed query set and scores recall@1, recall@5, and MRR.

Run:
    python -m bench.embeddings.run --corpus self

Writes `bench/embeddings/results.json`. No production code is modified.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_index import config as cfg_mod  # noqa: E402
from code_index import db as db_mod  # noqa: E402
from code_index import pipeline as pipeline_mod  # noqa: E402
from bench.embeddings import corpus as corpus_mod  # noqa: E402
from code_index.embeddings import (  # noqa: E402
    DEFAULT_MODEL,
    availability_report,
    coverage,
    get_backend,
    populate,
    semantic_search,
)
from code_index.search import fts as fts_mod  # noqa: E402


QUERIES_PATH = HERE / "queries.json"
RESULTS_PATH = HERE / "results.json"


# ------------------------------ scoring ------------------------------ #


def _hit_rank(results: list[dict], target: str) -> int | None:
    """1-indexed rank of the first result whose symbol_path equals target,
    or None if not found."""
    for i, r in enumerate(results, start=1):
        if r.get("symbol_path") == target:
            return i
    return None


def _score_block(per_query: list[dict]) -> dict[str, float]:
    n = len(per_query)
    if n == 0:
        return {"recall_at_1": 0.0, "recall_at_5": 0.0, "mrr": 0.0, "n": 0}
    r1 = sum(1 for q in per_query if q["rank"] is not None and q["rank"] <= 1)
    r5 = sum(1 for q in per_query if q["rank"] is not None and q["rank"] <= 5)
    mrr = sum((1.0 / q["rank"]) if q["rank"] else 0.0 for q in per_query)
    return {
        "recall_at_1": round(r1 / n, 4),
        "recall_at_5": round(r5 / n, 4),
        "mrr": round(mrr / n, 4),
        "n": n,
    }


def _score_block_by_category(per_query: list[dict]) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = {}
    for q in per_query:
        buckets.setdefault(q["category"], []).append(q)
    return {cat: _score_block(rows) for cat, rows in sorted(buckets.items())}


# ------------------------------ hybrid ------------------------------- #


def _minmax(values: list[float], *, higher_is_better: bool) -> list[float]:
    """Normalize to [0, 1]. If all values are equal, return 0.5 for all.
    If higher_is_better is False, invert so larger raw value -> smaller score."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    if higher_is_better:
        return [(v - lo) / (hi - lo) for v in values]
    # lower-is-better: map lo->1, hi->0
    return [1.0 - (v - lo) / (hi - lo) for v in values]


def _hybrid(bm25: list[dict], emb: list[dict], *, limit: int) -> list[dict]:
    """Union the two result sets, normalize scores within each list, then
    average the normalized scores. A result present in only one list uses
    its own normalized score as its combined score (the other side gets 0).

    BM25 scores are negative (lower is better in the fts.py convention,
    since `ORDER BY score ASC`), so we flip them for normalization.
    Embeddings scores are cosine (higher is better)."""
    # Normalize BM25 (lower raw = better) within its list.
    bm_scores = [r.get("score", 0.0) for r in bm25]
    bm_norm = _minmax(bm_scores, higher_is_better=False)
    bm_index = {r.get("chunk_uid"): (r, n) for r, n in zip(bm25, bm_norm)}

    em_scores = [r.get("score", 0.0) for r in emb]
    em_norm = _minmax(em_scores, higher_is_better=True)
    em_index = {r.get("chunk_uid"): (r, n) for r, n in zip(emb, em_norm)}

    uids = set(bm_index) | set(em_index)
    merged: list[tuple[float, dict]] = []
    for uid in uids:
        b = bm_index.get(uid)
        e = em_index.get(uid)
        b_row = b[0] if b else None
        e_row = e[0] if e else None
        b_n = b[1] if b else 0.0
        e_n = e[1] if e else 0.0
        combined = (b_n + e_n) / 2.0
        row = dict(e_row or b_row or {})
        row["bm25_norm"] = b_n
        row["embed_norm"] = e_n
        row["score"] = combined
        merged.append((combined, row))
    merged.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in merged[:limit]]


# -------------------------- corpus prep ------------------------------ #


def _ensure_corpus(
    repo_root: Path, *, refresh: bool = False
) -> tuple[sqlite3.Connection, Path]:
    """Mirror the benchmark source trees into a sandboxed corpus and
    open its index db. Keeps the benchmark deterministic and keeps the
    ground-truth `bench/queries.json` out of the FTS corpus.

    Returns (connection, corpus_root)."""
    corpus_root = corpus_mod.prepare_self_corpus(repo_root, refresh=refresh)
    db_path = corpus_mod.corpus_db_path(corpus_root)
    conn = db_mod.connect(db_path)
    db_mod.apply_schema(conn)
    chunk_count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
    ).fetchone()[0]
    print(f"[corpus] {chunk_count} live chunks at {db_path}")
    return conn, corpus_root


def _ensure_embeddings(conn: sqlite3.Connection, model: str) -> dict[str, Any]:
    """Populate embeddings for the current backend/model if coverage < 100%.
    Returns a status dict describing what happened."""
    report = availability_report()
    if not report["available"]:
        return {
            "status": "backend_unavailable",
            "availability": report,
        }
    cov_before = coverage(conn)
    if (
        cov_before["embedded_chunks"] >= cov_before["total_chunks"]
        and cov_before["total_chunks"] > 0
    ):
        backend = get_backend(model)
        return {
            "status": "already_populated",
            "coverage": cov_before,
            "provider": backend.provider,
            "model": backend.model_name,
            "dimension": backend.dimension,
        }
    print(
        f"[embed] populating: {cov_before['embedded_chunks']}/{cov_before['total_chunks']} "
        f"covered, model={model}…"
    )
    t0 = time.monotonic()
    backend = get_backend(model)
    stats = populate(conn, backend)
    dt = time.monotonic() - t0
    cov_after = coverage(conn)
    print(f"[embed] populated in {dt:.1f}s: {stats}")
    return {
        "status": "populated",
        "populate_stats": stats,
        "coverage": cov_after,
        "seconds": round(dt, 2),
        "provider": backend.provider,
        "model": backend.model_name,
        "dimension": backend.dimension,
    }


# -------------------------- main harness ----------------------------- #


def _load_queries() -> list[dict]:
    payload = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return list(payload["queries"])


def _run_one_query(
    conn: sqlite3.Connection,
    backend,
    q: dict,
    *,
    limit: int,
) -> dict[str, Any]:
    text = q["text"]
    target = q["target"]

    # FTS5 treats `-` as a NOT operator and `:` as a column-filter, so the
    # repo's `_sanitize` (which keeps both) can produce queries that raise
    # `no such column: file` on phrases like "cross-file" or
    # "natural-language". For the BM25 path in the benchmark we further
    # replace those two chars with spaces. The word tokens themselves
    # survive, so BM25 still gets a fair shake.
    bm_text = text.replace("-", " ").replace(":", " ")
    t0 = time.monotonic()
    bm = fts_mod.search(conn, bm_text, limit=limit)
    bm_dt = time.monotonic() - t0

    t0 = time.monotonic()
    em = semantic_search(conn, backend, text, limit=limit)
    em_dt = time.monotonic() - t0

    t0 = time.monotonic()
    hy = _hybrid(bm, em, limit=limit)
    hy_dt = time.monotonic() - t0

    def pack(results: list[dict], mode: str, dt: float) -> dict:
        rank = _hit_rank(results, target)
        return {
            "mode": mode,
            "rank": rank,
            "seconds": round(dt, 4),
            "top5": [
                {
                    "symbol_path": r.get("symbol_path"),
                    "chunk_uid": r.get("chunk_uid"),
                    "score": r.get("score"),
                }
                for r in results[:5]
            ],
        }

    return {
        "id": q["id"],
        "text": text,
        "target": target,
        "category": q.get("category", "unspecified"),
        "bm25": pack(bm, "bm25", bm_dt),
        "embeddings": pack(em, "embeddings", em_dt),
        "hybrid": pack(hy, "hybrid", hy_dt),
    }


def _aggregate(per_query: list[dict]) -> dict[str, Any]:
    def flat(mode: str) -> list[dict]:
        return [
            {"rank": row[mode]["rank"], "category": row["category"]}
            for row in per_query
        ]

    overall = {
        "bm25": _score_block(flat("bm25")),
        "embeddings": _score_block(flat("embeddings")),
        "hybrid": _score_block(flat("hybrid")),
    }
    by_category = {
        "bm25": _score_block_by_category(flat("bm25")),
        "embeddings": _score_block_by_category(flat("embeddings")),
        "hybrid": _score_block_by_category(flat("hybrid")),
    }
    return {"overall": overall, "by_category": by_category}


def _write_stub_results(reason: str, extra: dict | None = None) -> dict:
    payload = {
        "status": "backend_unavailable",
        "reason": reason,
        "note": (
            "No embedding backend installed. Install one of: "
            "`pip install fastembed` or `pip install sentence-transformers`. "
            "This stub exists so CI can still check the harness wiring."
        ),
        "availability": extra or {},
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--corpus",
        default="self",
        choices=["self"],
        help="Benchmark corpus. Only 'self' (the code_index repo itself) is supported.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Per-query result limit for each retrieval mode (default: 20).",
    )
    ap.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Embedding model name (default: {DEFAULT_MODEL}).",
    )
    ap.add_argument(
        "--results",
        default=str(RESULTS_PATH),
        help="Output JSON path.",
    )
    ap.add_argument(
        "--refresh-corpus",
        action="store_true",
        help="Re-mirror the source trees and force a full reindex.",
    )
    args = ap.parse_args(argv)

    results_path = Path(args.results)

    report = availability_report()
    if not report["available"]:
        print("[abort] no embedding backend installed; writing stub results.")
        _write_stub_results(
            reason="availability_report().available is False", extra=report
        )
        return 2

    if args.corpus == "self":
        root = REPO_ROOT
    else:  # pragma: no cover
        raise SystemExit(f"unsupported corpus: {args.corpus}")

    conn, corpus_root = _ensure_corpus(root, refresh=args.refresh_corpus)
    embed_info = _ensure_embeddings(conn, args.model)
    if embed_info["status"] == "backend_unavailable":
        print("[abort] backend became unavailable mid-run; writing stub.")
        _write_stub_results(
            reason="backend unavailable during populate", extra=embed_info
        )
        return 2

    backend = get_backend(args.model)
    queries = _load_queries()
    print(f"[bench] running {len(queries)} queries…")

    per_query: list[dict] = []
    for q in queries:
        row = _run_one_query(conn, backend, q, limit=args.limit)
        per_query.append(row)
        # Quick per-query visibility.
        bm_r = row["bm25"]["rank"]
        em_r = row["embeddings"]["rank"]
        hy_r = row["hybrid"]["rank"]
        print(
            f"  {q['id']:<4} [{q['category']:<8}] bm25={bm_r!s:<4} "
            f"emb={em_r!s:<4} hybrid={hy_r!s:<4}  {q['text'][:60]!r}"
        )

    aggregate = _aggregate(per_query)

    payload = {
        "status": "ok",
        "corpus": args.corpus,
        "limit": args.limit,
        "repo_root": str(root),
        "corpus_root": str(corpus_root),
        "embedding": {
            "provider": backend.provider,
            "model": backend.model_name,
            "dimension": backend.dimension,
            "prep": embed_info,
        },
        "aggregate": aggregate,
        "per_query": per_query,
    }
    results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\n[results] wrote {results_path}")

    # Print overall table.
    o = aggregate["overall"]
    print("\nOverall (n={n}):".format(n=o["bm25"]["n"]))
    print(f"  {'mode':<12} {'R@1':>6} {'R@5':>6} {'MRR':>6}")
    for mode in ("bm25", "embeddings", "hybrid"):
        m = o[mode]
        print(
            f"  {mode:<12} {m['recall_at_1']:>6.3f} "
            f"{m['recall_at_5']:>6.3f} {m['mrr']:>6.3f}"
        )

    db_mod.close(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
