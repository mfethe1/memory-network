"""Run the broker-vs-ripgrep retrieval benchmark.

Examples:
    python -m bench.retrieval.run --json
    python -m bench.retrieval.run --markdown-output bench/retrieval/results.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.retrieval import corpus as corpus_mod  # noqa: E402
from bench.retrieval import harness, report as report_mod  # noqa: E402
from code_index import db_router as db_mod  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        default=str(harness.DEFAULT_CASES_PATH),
        help="JSONL benchmark cases path.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to mirror into the benchmark corpus.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=harness.DEFAULT_K,
        help=f"Result depth for recall/precision/MRR (default: {harness.DEFAULT_K}).",
    )
    parser.add_argument(
        "--budget-bytes",
        type=int,
        default=harness.DEFAULT_BUDGET_BYTES,
        help=(
            "Broker byte budget per case "
            f"(default: {harness.DEFAULT_BUDGET_BYTES})."
        ),
    )
    parser.add_argument(
        "--rg-path",
        default=None,
        help="Optional ripgrep executable path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the JSON report to stdout.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print the markdown report to stdout.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON output path.",
    )
    parser.add_argument(
        "--markdown-output",
        default=None,
        help="Optional markdown output path.",
    )
    parser.add_argument(
        "--keep-corpus",
        action="store_true",
        help="Keep the mirrored corpus at bench/retrieval/.corpus/self.",
    )
    args = parser.parse_args(argv)

    cases = harness.load_cases(Path(args.cases))
    with corpus_mod.prepare_self_corpus(
        Path(args.repo_root),
        keep=bool(args.keep_corpus),
    ) as prepared:
        conn = sqlite3.connect(prepared.db_path)
        conn.row_factory = sqlite3.Row
        try:
            db_mod.ensure_schema(conn)
            payload = harness.run_benchmark(
                conn,
                prepared.root,
                cases,
                k=max(1, int(args.k)),
                budget_bytes=max(1, int(args.budget_bytes)),
                rg_path=args.rg_path,
            )
        finally:
            db_mod.close(conn)

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    markdown = None
    if args.markdown or args.markdown_output or not args.json:
        markdown = report_mod.render_markdown(payload)
    if args.markdown_output:
        md_output = Path(args.markdown_output)
        md_output.parent.mkdir(parents=True, exist_ok=True)
        md_output.write_text(markdown or "", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.markdown or markdown:
        print(markdown, end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
