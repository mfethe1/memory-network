"""`code_index mcp-serve`: Model Context Protocol server over the local index.

Built on `mcp.server.fastmcp.FastMCP` (shipped inside the official `mcp`
Python SDK, v1.2+). FastMCP gives us a decorator-based tool/resource API,
URI-template parameters, and a choice of transports (stdio by default;
streamable HTTP available via `--transport http`).

Tools (model-controlled):
- `search_text`     → lexical fast path (ripgrep; falls back to python-re).
- `search_query`    → BM25 FTS retrieval over chunks.
- `search_ast`      → tree-sitter structural query over Python.
- `find_symbol`     → symbol lookup; can include call-site references.
- `impact`          → blast-radius (inbound calls/inherits/contains/imports).
- `affected_tests`  → tests that reach a symbol + pytest invocation.
- `doctor`          → health snapshot + FTS drift + rebuild recommendation.

Mutating tools — only registered when `--allow-writes` is passed:
- `update`          → reindex (files | all).
- `rebuild_fts`     → prune tombstone drift.

Default mode is READ-ONLY. Agents cannot reach `update` or `rebuild_fts`
unless the operator explicitly starts the server with `--allow-writes`.

Resources (application-controlled, URI-templated):
- `codeindex://repo-map`
- `codeindex://doctor`
- `codeindex://symbol/{canonical}`
- `codeindex://chunk/{chunk_uid}`

Transports:
- `--transport stdio` (default) — what Claude Code / Codex CLI expect.
- `--transport http`            — streamable HTTP for remote agents.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.commands import mcp_tool_impl as _mcp_tool_impl
from code_index.locking import LockTimeoutError, writer_lock
from code_index.commands.mcp_auth import (
    TOKEN_ENV_VAR,
    TOKEN_FILENAME,
    _generate_token,
    _is_loopback,
    _read_token_file,
    _resolve_bearer_token,
    _validate_bearer,
    _write_token_file,
)
from code_index.commands.mcp_surface import (
    _READ_TOOL_DESCRIPTIONS,
    _RESOURCE_DESCRIPTIONS,
    _TOOL_DESCRIPTIONS,
    _WRITE_TOOL_DESCRIPTIONS,
    _tool_descriptions,
    describe_surface,
)
from code_index.commands.mcp_tool_impl import (
    _resource_chunk,
    _resource_repo_map,
    _resource_symbol,
    _tool_affected_tests,
    _tool_agent_activity,
    _tool_agent_end,
    _tool_agent_event,
    _tool_agent_start,
    _tool_code_graph,
    _tool_doctor,
    _tool_find_symbol,
    _tool_impact,
    _tool_search_ast,
    _tool_search_query,
    _tool_search_text,
)




def _with_local_writer_lock(fn, *args, **kwargs):
    """Run moved MCP tool implementations with this module's writer_lock.

    Some tests and debuggers monkeypatch `mcp_serve_cmd.writer_lock`
    directly. This adapter preserves that seam after moving implementation
    code into `mcp_tool_impl`.
    """
    original = _mcp_tool_impl.writer_lock
    _mcp_tool_impl.writer_lock = writer_lock
    try:
        return fn(*args, **kwargs)
    finally:
        _mcp_tool_impl.writer_lock = original


def _tool_update(
    config: cfg_mod.Config,
    files: list[str] | None = None,
    all: bool = False,
) -> dict:
    return _with_local_writer_lock(
        _mcp_tool_impl._tool_update,
        config,
        files,
        all,
    )


def _tool_rebuild_fts(config: cfg_mod.Config) -> dict:
    return _with_local_writer_lock(_mcp_tool_impl._tool_rebuild_fts, config)


def _mcp_available() -> bool:
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    except Exception:
        return False
    return True


def _resolve_config(root_hint: Path) -> tuple[cfg_mod.Config | None, dict | None]:
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        return None, {"error": "no index", "hint": f"run `code_index init` at {root}"}
    # Idempotent schema-readiness at server boot. Fast path: no-op when
    # the DB is already current. Slow path: acquire writer_lock and run
    # apply_schema. Never emits schema-write SQL outside the lock.
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
    finally:
        db_mod.close(conn)
    return config, None


# ---------- FastMCP wiring ----------


def _build_fastmcp(config: cfg_mod.Config, *, allow_writes: bool = False):
    """Build the FastMCP server.

    Default mode is READ-ONLY — `update` and `rebuild_fts` are NOT registered.
    Pass `allow_writes=True` (wired to the `--allow-writes` CLI flag) to
    expose the mutating tools. `describe_surface(allow_writes=...)` returns
    the same tool list this function registers.
    """
    from mcp.server.fastmcp import FastMCP

    tools = _tool_descriptions(allow_writes=allow_writes)
    mcp = FastMCP("code_index")

    # Read-only tools — always registered.
    @mcp.tool(description=tools["search_text"])
    def search_text(
        pattern: str,
        path: str | None = None,
        max_count: int = 50,
        ignore_case: bool = False,
        fixed_strings: bool = False,
    ) -> dict:
        return _tool_search_text(
            config, pattern, path, max_count, ignore_case, fixed_strings
        )

    @mcp.tool(description=tools["search_query"])
    def search_query(
        query: str,
        limit: int = 20,
        language: str | None = None,
        chunk_type: str | None = None,
    ) -> dict:
        return _tool_search_query(config, query, limit, language, chunk_type)

    @mcp.tool(description=tools["search_ast"])
    def search_ast(pattern: str, limit: int = 100) -> dict:
        return _tool_search_ast(config, pattern, limit)

    @mcp.tool(description=tools["find_symbol"])
    def find_symbol(
        name: str,
        kind: str | None = None,
        language: str | None = None,
        limit: int = 50,
        references: bool = False,
    ) -> dict:
        return _tool_find_symbol(config, name, kind, language, limit, references)

    @mcp.tool(description=tools["impact"])
    def impact(symbol: str, max_depth: int = 2, no_imports: bool = False) -> dict:
        return _tool_impact(config, symbol, max_depth, no_imports)

    @mcp.tool(description=tools["affected_tests"])
    def affected_tests(symbol: str) -> dict:
        return _tool_affected_tests(config, symbol)

    @mcp.tool(description=tools["doctor"])
    def doctor() -> dict:
        return _tool_doctor(config)

    @mcp.tool(description=tools["ask"])
    def ask(question: str) -> dict:
        from code_index.nl import answer as _nl_answer

        conn = db_mod.connect(config.db_path)
        try:
            return _nl_answer(config, conn, question)
        finally:
            db_mod.close(conn)

    @mcp.tool(description=tools["code_graph"])
    def code_graph(
        include_code: bool = False,
        max_code_bytes: int = 200_000,
        focus_paths: list[str] | None = None,
        agent_name: str | None = None,
    ) -> dict:
        return _tool_code_graph(
            config, include_code, max_code_bytes, focus_paths, agent_name
        )

    @mcp.tool(description=tools["agent_activity"])
    def agent_activity(limit: int = 100) -> dict:
        return _tool_agent_activity(config, limit)

    # Mutating tools — only registered when --allow-writes is set.
    if allow_writes:

        @mcp.tool(description=tools["update"])
        def update(files: list[str] | None = None, all: bool = False) -> dict:
            return _tool_update(config, files, all)

        @mcp.tool(description=tools["rebuild_fts"])
        def rebuild_fts() -> dict:
            return _tool_rebuild_fts(config)

        @mcp.tool(description=tools["agent_start"])
        def agent_start(
            agent_name: str = "Agent",
            prompt: str = "",
            selected_nodes: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
            run_id: str | None = None,
        ) -> dict:
            return _tool_agent_start(
                config, agent_name, prompt, selected_nodes, metadata, run_id
            )

        @mcp.tool(description=tools["agent_event"])
        def agent_event(
            event_type: str,
            file_path: str | None = None,
            message: str | None = None,
            run_id: str | None = None,
            agent_name: str = "Agent",
            symbol_path: str | None = None,
            payload: dict[str, Any] | None = None,
        ) -> dict:
            return _tool_agent_event(
                config,
                event_type,
                file_path,
                message,
                run_id,
                agent_name,
                symbol_path,
                payload,
            )

        @mcp.tool(description=tools["agent_end"])
        def agent_end(
            run_id: str | None = None,
            agent_name: str = "Agent",
            status: str = "completed",
        ) -> dict:
            return _tool_agent_end(config, run_id, agent_name, status)

    # Resources (URI templates handled natively by FastMCP).
    @mcp.resource("codeindex://repo-map", description=_RESOURCE_DESCRIPTIONS[0][1])
    def repo_map() -> dict:
        return _resource_repo_map(config)

    @mcp.resource("codeindex://doctor", description=_RESOURCE_DESCRIPTIONS[1][1])
    def doctor_resource() -> dict:
        return _tool_doctor(config)

    @mcp.resource("codeindex://graph", description=_RESOURCE_DESCRIPTIONS[2][1])
    def graph_resource() -> dict:
        return _tool_code_graph(config, include_code=False)

    @mcp.resource(
        "codeindex://symbol/{canonical}", description=_RESOURCE_DESCRIPTIONS[3][1]
    )
    def symbol_resource(canonical: str) -> dict:
        return _resource_symbol(config, canonical)

    @mcp.resource(
        "codeindex://chunk/{chunk_uid}", description=_RESOURCE_DESCRIPTIONS[4][1]
    )
    def chunk_resource(chunk_uid: str) -> dict:
        return _resource_chunk(config, chunk_uid)

    @mcp.resource(
        "codeindex://agent-activity", description=_RESOURCE_DESCRIPTIONS[5][1]
    )
    def agent_activity_resource() -> dict:
        return _tool_agent_activity(config)

    return mcp


# ---------- HTTP server startup (with bearer auth) ----------


def _build_bearer_middleware(expected_token: str):
    """Build a Starlette ASGI middleware class that 401s on missing/bad bearer.

    Returns a (middleware_class, kwargs) tuple suitable for `add_middleware`.
    Kept as a factory so the expected token is a closure, not a global.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class _BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            header = request.headers.get("authorization")
            if not _validate_bearer(header, expected_token):
                return JSONResponse(
                    {
                        "error": "unauthorized",
                        "detail": "missing or invalid bearer token",
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="code_index"'},
                )
            return await call_next(request)

    return _BearerAuthMiddleware


def _run_http(mcp, *, host: str, port: int | None, expected_token: str) -> int:
    """Build the streamable-http Starlette app, attach bearer middleware,
    serve with uvicorn. Returns an exit code.

    Falls back to a warning + unauthenticated run if the SDK layout we
    expect (streamable_http_app + add_middleware) is missing.
    """
    try:
        import uvicorn

        app = mcp.streamable_http_app()
        middleware_cls = _build_bearer_middleware(expected_token)
        app.add_middleware(middleware_cls)
    except (AttributeError, ImportError, RuntimeError) as exc:
        print(
            "code_index mcp-serve: WARNING — could not attach bearer-auth "
            "middleware to FastMCP streamable-http app "
            f"({type(exc).__name__}: {exc}). "
            "Refusing to start an unauthenticated HTTP server. "
            "Use --transport stdio, upgrade the `mcp` SDK, or file a bug.",
            file=sys.stderr,
        )
        return 2

    effective_port = port if port is not None else mcp.settings.port
    config = uvicorn.Config(
        app,
        host=host,
        port=effective_port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    server.run()
    return 0


# ---------- CLI entrypoint ----------


def run(args: argparse.Namespace) -> int:
    if not _mcp_available():
        if args.json:
            print(json.dumps(_UNAVAILABLE, indent=2))
        else:
            print(f"error: {_UNAVAILABLE['error']}")
            print(f"hint:  {_UNAVAILABLE['hint']}")
        return 2

    allow_writes = bool(getattr(args, "allow_writes", False))

    if getattr(args, "describe", False):
        # Describe path doesn't need a live index — it's a static surface.
        print(json.dumps(describe_surface(allow_writes=allow_writes), indent=2))
        return 0

    if allow_writes:
        print(
            "mcp-serve: mutating tools enabled via --allow-writes",
            file=sys.stderr,
        )

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    config, err = _resolve_config(root_hint)
    if err is not None:
        if args.json:
            print(json.dumps(err, indent=2))
        else:
            print(f"error: {err['error']} — {err.get('hint', '')}")
        return 2
    assert config is not None

    transport = getattr(args, "transport", "stdio")

    # --- HTTP transport: validate bind, resolve token, attach middleware. ---
    if transport in ("http", "streamable-http"):
        bind = getattr(args, "bind", None) or "127.0.0.1"
        allow_remote = bool(getattr(args, "allow_remote", False))
        if not _is_loopback(bind) and not allow_remote:
            err_obj = {
                "error": "remote bind refused",
                "bind": bind,
                "hint": "pass --allow-remote to bind a non-loopback address (requires a bearer token)",
            }
            if args.json:
                print(json.dumps(err_obj, indent=2))
            else:
                print(f"error: {err_obj['error']} (bind={bind}) — {err_obj['hint']}")
            return 2

        try:
            token, _source = _resolve_bearer_token(
                flag_token=getattr(args, "bearer_token", None),
                flag_token_file=getattr(args, "bearer_token_file", None),
                env_token=os.environ.get(TOKEN_ENV_VAR),
                config=config,
                generate_if_missing=True,
                stderr=sys.stderr,
            )
        except (OSError, ValueError) as exc:
            err_obj = {"error": "bearer token resolution failed", "detail": str(exc)}
            if args.json:
                print(json.dumps(err_obj, indent=2))
            else:
                print(f"error: {err_obj['error']} — {err_obj['detail']}")
            return 2
        assert token is not None  # generate_if_missing=True

        mcp = _build_fastmcp(config, allow_writes=allow_writes)
        port = getattr(args, "port", None)
        try:
            return _run_http(mcp, host=bind, port=port, expected_token=token)
        except KeyboardInterrupt:
            return 0

    # --- stdio transport: unchanged, no auth. ---
    mcp = _build_fastmcp(config, allow_writes=allow_writes)
    try:
        if transport == "stdio":
            mcp.run()
        else:
            print(f"error: unsupported transport '{transport}' (choose: stdio | http)")
            return 2
    except KeyboardInterrupt:
        pass
    return 0
