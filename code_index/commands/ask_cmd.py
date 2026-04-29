"""`code_index ask "QUESTION"`: natural-language query synthesis.

Deterministic pattern classifier — no LLM — that maps everyday questions
to the right primitives and returns a structured bundle plus a one-paragraph
narrative the consuming agent can quote directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.nl import answer


def run(args: argparse.Namespace) -> int:
    if not args.question:
        print(
            'error: provide a question in quotes, e.g. `code_index ask "who calls reindex"`'
        )
        return 2
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    with db_mod.open_config(config, schema="ensure") as conn:
        bundle = answer(
            config,
            conn,
            args.question,
            fallback_unknown=not getattr(args, "no_fallback", False),
        )

    if args.json:
        print(json.dumps(bundle, indent=2, default=str))
        return 0
    # Terse text mode for humans.
    print(f"Q: {bundle['question']}")
    intent = bundle["intent"]
    print(
        f"intent: {intent['kind']}  confidence={intent['confidence']:.2f}"
        f"  target={intent.get('target')!r}"
    )
    print(f"  matched: {intent.get('rationale')}")
    print(f"primary_tool: {bundle['primary_tool']}")
    print(f"\n{bundle['narrative']}")
    if bundle.get("suggestions"):
        print("\nfollow-ups:")
        for s in bundle["suggestions"]:
            print(f"  - {s}")
    if bundle.get("limitations"):
        print("\nlimitations:")
        for limitation in bundle["limitations"]:
            print(f"  - {limitation}")
    return 0
