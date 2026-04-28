"""`code_index graph-server`: command entrypoint for the live graph server."""

from __future__ import annotations

import argparse
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from code_index import config as cfg_mod
from code_index.commands.graph_server_http import _make_handler
from code_index.commands.graph_server_state import _agent_stream_payload
from code_index.commands.graph_server_utils import GRAPH_TOKEN_ENV_VAR


__all__ = ["_agent_stream_payload", "_make_handler", "run"]


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2
    host = args.host or "127.0.0.1"
    port = int(args.port or 8767)
    server = ThreadingHTTPServer((host, port), _make_handler(config, args))
    server.quiet = bool(getattr(args, "quiet", False))  # type: ignore[attr-defined]
    token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
    url = f"http://{host}:{port}/repo-graph.html"
    if token:
        url = f"{url}?token={quote(token, safe='')}"
    print(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("graph server stopped")
    finally:
        server.server_close()
    return 0
