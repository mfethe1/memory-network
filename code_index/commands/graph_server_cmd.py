"""`code_index graph-server`: command entrypoint for the live graph server."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from code_index import config as cfg_mod
from code_index import scopes
from code_index.commands.graph_server_http import _make_handler
from code_index.commands.graph_server_state import _agent_stream_payload


__all__ = ["_agent_stream_payload", "_make_handler", "run"]


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    try:
        scope_selection = scopes.resolve_scope(config.root, getattr(args, "scope", None))
        setattr(args, "_resolved_scope", scope_selection)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    host = args.host or "127.0.0.1"
    port = int(args.port or 8767)
    server = ThreadingHTTPServer((host, port), _make_handler(config, args))
    server.quiet = bool(getattr(args, "quiet", False))  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/repo-graph.html"
    print(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("graph server stopped")
    finally:
        server.server_close()
    return 0
