"""Markdown report rendering for the retrieval benchmark."""

from __future__ import annotations

from typing import Any


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Retrieval Broker vs Ripgrep Benchmark",
        "",
        f"- Cases: {report['case_count']}",
        f"- k: {report['k']}",
        f"- Corpus: `{report['corpus_root']}`",
        "",
        "| Mode | Recall@k | Precision@k | MRR | p50 ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode_name, label in (("broker", "Broker"), ("ripgrep", "Ripgrep")):
        mode = report["aggregate"][mode_name]
        macro = mode["macro"]
        latency = mode["latency_ms"]
        lines.append(
            "| {label} | {recall:.4f} | {precision:.4f} | {mrr:.4f} | "
            "{p50:.3f} | {p95:.3f} |".format(
                label=label,
                recall=macro["recall_at_k"],
                precision=macro["precision_at_k"],
                mrr=macro["mrr"],
                p50=latency["p50"],
                p95=latency["p95"],
            )
        )
    lines.extend(
        [
            "",
            "## Per Case",
            "",
            "| Case | Group | Broker R@k | rg R@k | Broker MRR | rg MRR | Delta R |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for case in report["cases"]:
        broker = case["broker"]
        rg = case["ripgrep"]
        diff = case["diff"]
        lines.append(
            "| `{id}` | {group} | {br:.4f} | {rr:.4f} | {bm:.4f} | "
            "{rm:.4f} | {delta:.4f} |".format(
                id=case["id"],
                group=case["group"],
                br=broker["recall_at_k"],
                rr=rg["recall_at_k"],
                bm=broker["mrr"],
                rm=rg["mrr"],
                delta=diff["broker_recall_minus_ripgrep"],
            )
        )
    return "\n".join(lines) + "\n"
