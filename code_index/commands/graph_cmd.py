"""`code_index graph`: repo-level file graph for humans and agents.

The graph is a read-only projection over the existing index. It surfaces:

- file and directory nodes
- cross-file relations derived from symbol relations
- file importance and agent care guidance
- deterministic file summaries
- optional embedded source code for click-to-code HTML inspection
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import scopes
from code_index.commands.graph_html import render_html
from code_index.commands.graph_model import build_graph


def _write_or_print(text: str, output: str | None, *, root: Path) -> None:
    if not output:
        print(text)
        return
    out_path = Path(output)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(str(out_path.resolve()))


def _write_json_sidecar(payload: dict[str, Any], output: str | None, *, root: Path) -> None:
    if not output:
        return
    out_path = Path(output)
    if not out_path.is_absolute():
        out_path = root / out_path
    sidecar_path = out_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    try:
        scope_selection = scopes.resolve_scope(config.root, getattr(args, "scope", None))
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    fmt = "json" if args.json else args.format
    output = args.output
    if getattr(args, "watch", False) and output is None:
        suffix = "json" if fmt == "json" else "html"
        output = str(config.index_dir / f"repo-graph.{suffix}")

    def _build_payload() -> dict[str, Any]:
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.ensure_schema(conn, config)
            focus_paths = list(args.focus or [])
            if scope_selection.explicit:
                focus_paths.extend(
                    path
                    for path in scopes.indexed_file_paths_for_scope(
                        conn, scope_selection
                    )
                    if path not in focus_paths
                )
            return build_graph(
                conn,
                config.root,
                include_code=not args.no_code,
                max_code_bytes=max(0, int(args.max_code_bytes)),
                focus_paths=focus_paths,
                agent_name=args.agent_name,
            )
        finally:
            db_mod.close(conn)

    def _emit_once() -> None:
        payload = _build_payload()
        payload["scope"] = scope_selection.to_dict()
        if fmt == "json":
            _write_or_print(json.dumps(payload, indent=2), output, root=config.root)
            return
        html = render_html(payload)
        html_output = output or str(config.index_dir / "repo-graph.html")
        if not getattr(args, "no_sidecar", False):
            _write_json_sidecar(payload, html_output, root=config.root)
        _write_or_print(html, html_output, root=config.root)

    if not getattr(args, "watch", False):
        _emit_once()
        return 0

    import time

    interval = max(0.5, float(getattr(args, "watch_interval", 2.0) or 2.0))
    print(f"watching graph output every {interval:.1f}s; press Ctrl+C to stop")
    try:
        while True:
            _emit_once()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("graph watch stopped")
    return 0
