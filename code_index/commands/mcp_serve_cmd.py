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
import ipaddress
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from code_index import __version__ as _code_index_version
from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands.impact_cmd import _resolve_target, compute_impact
from code_index.locking import LockTimeoutError, writer_lock
from code_index.pipeline import reindex
from code_index.runners.pytest import build_pytest_invocation
from code_index.search import fts, lexical, symbol_search
from code_index.structural import ts_python


# ---------- HTTP auth helpers (pure functions; tested directly) ----------


TOKEN_FILENAME = "mcp-token"
TOKEN_ENV_VAR = "CODE_INDEX_MCP_TOKEN"


def _generate_token() -> str:
    """Return a 32-byte hex-encoded random token (64 hex chars)."""
    return secrets.token_hex(32)


def _write_token_file(path: Path, token: str) -> None:
    """Write token to path with mode 0600 on POSIX. Creates parent dirs.

    Note: On Windows the chmod to 0600 is a best-effort no-op — NTFS ACLs
    provide the real protection and we don't try to set them here. Callers
    should rely on the file living under `.code_index/` (which inherits the
    repo's existing ACLs).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except (OSError, NotImplementedError):
        # Windows / odd filesystems: best-effort only.
        pass


def _read_token_file(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"bearer token file is empty: {path}")
    return token


def _is_loopback(bind: str) -> bool:
    """True iff bind is a literal loopback address ('127.0.0.1', '::1', 'localhost')."""
    if bind in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _resolve_bearer_token(
    *,
    flag_token: str | None,
    flag_token_file: str | None,
    env_token: str | None,
    config: cfg_mod.Config,
    generate_if_missing: bool,
    stderr,
) -> tuple[str | None, str]:
    """Return (token, source). Source is one of: 'flag', 'file', 'env', 'generated'.

    If `generate_if_missing` is True and no other source is set, generate a
    new token, persist it to `.code_index/mcp-token` with 0600 perms, and
    print it ONCE to `stderr` so the user can copy it.

    If `generate_if_missing` is False, returns (None, 'none') when nothing is
    set — caller decides what to do.
    """
    if flag_token:
        return flag_token.strip(), "flag"
    if flag_token_file:
        return _read_token_file(Path(flag_token_file)), "file"
    if env_token:
        return env_token.strip(), "env"
    if not generate_if_missing:
        return None, "none"
    token = _generate_token()
    token_path = config.index_dir / TOKEN_FILENAME
    _write_token_file(token_path, token)
    print(
        f"code_index mcp-serve: generated bearer token (copy this):\n"
        f"  token: {token}\n"
        f"  file:  {token_path} (mode 0600 on POSIX)\n"
        f"  env:   export {TOKEN_ENV_VAR}={token}\n",
        file=stderr,
    )
    return token, "generated"


def _validate_bearer(auth_header: str | None, expected: str) -> bool:
    """Return True iff `auth_header` is a well-formed `Bearer <expected>`.

    Uses `secrets.compare_digest` to avoid timing leaks.
    """
    if not auth_header or not expected:
        return False
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1].strip(), expected)


_UNAVAILABLE = {
    "error": "MCP SDK not installed",
    "hint": "install with: pip install 'code-index[mcp]'  (or: pip install mcp)",
}


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


# ---------- Pure tool implementations (plain functions; no MCP imports) ----------


def _tool_search_text(
    config: cfg_mod.Config,
    pattern: str,
    path: str | None = None,
    max_count: int = 50,
    ignore_case: bool = False,
    fixed_strings: bool = False,
) -> dict:
    return lexical.grep(
        config,
        pattern=pattern,
        path_glob=path,
        max_count=max_count,
        case_insensitive=ignore_case,
        fixed_strings=fixed_strings,
    )


def _tool_search_query(
    config: cfg_mod.Config,
    query: str,
    limit: int = 20,
    language: str | None = None,
    chunk_type: str | None = None,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        results = fts.search(
            conn, query, limit=limit, language=language, chunk_type=chunk_type
        )
    finally:
        db_mod.close(conn)
    return {"engine": "fts5", "query": query, "results": results}


def _tool_search_ast(config: cfg_mod.Config, pattern: str, limit: int = 100) -> dict:
    if not ts_python.available():
        return {
            "error": "tree-sitter not available",
            "reason": ts_python._unavailable_reason(),
        }
    from code_index.ignore import build as build_matcher
    from code_index.scanner import iter_files

    matcher = build_matcher(
        config.root, extra=config.extra_ignore, include_hidden=config.include_hidden
    )
    files = [
        (s.path, s.rel_path)
        for s in iter_files(config.root, matcher, max_bytes=config.max_file_bytes)
        if s.rel_path.lower().endswith((".py", ".pyi"))
    ]
    try:
        result = ts_python.query_files(files, pattern)
    except Exception as exc:
        return {"error": "invalid tree-sitter query", "detail": repr(exc)}
    caps = [
        {
            "file": c.file_path,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "capture_name": c.capture_name,
            "node_kind": c.node_kind,
            "preview": c.text[:120].replace("\n", " "),
        }
        for c in result.captures[:limit]
    ]
    return {
        "engine": "tree-sitter",
        "query": result.query,
        "expanded_query": result.expanded_query,
        "total_captures": len(result.captures),
        "returned": len(caps),
        "results": caps,
    }


def _tool_find_symbol(
    config: cfg_mod.Config,
    name: str,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 50,
    references: bool = False,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        results = symbol_search.lookup(
            conn,
            name,
            kind=kind,
            language=language,
            limit=limit,
            include_references=references,
        )
    finally:
        db_mod.close(conn)
    return {"query": name, "results": results}


def _tool_impact(
    config: cfg_mod.Config,
    symbol: str,
    max_depth: int = 2,
    no_imports: bool = False,
) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        candidates = _resolve_target(conn, symbol)
        if not candidates:
            return {"error": "no matching symbol", "query": symbol}
        target_pk = int(candidates[0]["symbol_pk"])
        result = compute_impact(
            conn,
            target_pk,
            max_depth=max_depth,
            include_imports=not no_imports,
        )
        result["query"] = symbol
        return result
    finally:
        db_mod.close(conn)


def _tool_affected_tests(config: cfg_mod.Config, symbol: str) -> dict:
    from code_index.commands.tests_cmd import _affected, _resolve_input

    conn = db_mod.connect(config.db_path)
    try:
        candidates = _resolve_input(conn, symbol)
        if not candidates:
            return {"error": "no matching symbol", "query": symbol}
        target = candidates[0]
        affected = _affected(conn, int(target["symbol_pk"]))
    finally:
        db_mod.close(conn)
    return {
        "query": symbol,
        "target": {
            "symbol_uid": target["symbol_uid"],
            "canonical_name": target["canonical_name"],
            "kind": target["kind"],
        },
        "affected_tests": affected,
        "runner": build_pytest_invocation(affected),
    }


def _tool_doctor(config: cfg_mod.Config) -> dict:
    from code_index.commands.doctor_cmd import (
        _fts_consistency,
        _language_counts,
        _parse_status_counts,
        _relation_counts,
        _semantic_source_counts,
    )

    conn = db_mod.connect(config.db_path)
    try:
        return {
            "root": str(config.root),
            "schema_version": db_mod.get_schema_version(conn),
            "parse_status": _parse_status_counts(conn),
            "semantic_sources": _semantic_source_counts(conn),
            "languages": _language_counts(conn),
            "relations": _relation_counts(conn),
            "fts_consistency": _fts_consistency(conn),
            "test_edges": conn.execute("SELECT COUNT(*) FROM test_edges").fetchone()[0],
            "unresolved_calls_open": conn.execute(
                "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
            ).fetchone()[0],
        }
    finally:
        db_mod.close(conn)


def _tool_update(
    config: cfg_mod.Config,
    files: list[str] | None = None,
    all: bool = False,
) -> dict:
    paths = [Path(p) for p in files] if files else (None if all else [])
    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                stats = reindex(conn, config, paths=paths, event_source="mcp")
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        return {
            "error": "another writer holds the lock",
            "lock_path": str(exc.lock_path),
            "timeout_s": exc.timeout_s,
        }
    return {"stats": stats.to_dict()}


def _tool_rebuild_fts(config: cfg_mod.Config) -> dict:
    from code_index.commands.rebuild_fts_cmd import _rebuild

    try:
        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                return _rebuild(conn)
            finally:
                db_mod.close(conn)
    except LockTimeoutError as exc:
        return {
            "error": "another writer holds the lock",
            "lock_path": str(exc.lock_path),
            "timeout_s": exc.timeout_s,
        }


# ---------- Resource implementations ----------


def _resource_repo_map(config: cfg_mod.Config) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT canonical_name, kind, display_name
              FROM symbols
             WHERE deleted_at IS NULL
               AND kind IN ('module', 'class', 'function', 'method')
             ORDER BY canonical_name ASC
            """
        ).fetchall()
    finally:
        db_mod.close(conn)
    return {
        "symbols": [
            {
                "canonical_name": r["canonical_name"],
                "kind": r["kind"],
                "display_name": r["display_name"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


def _resource_symbol(config: cfg_mod.Config, name: str) -> dict:
    return _tool_find_symbol(config, name, references=True, limit=10)


def _resource_chunk(config: cfg_mod.Config, chunk_uid: str) -> dict:
    conn = db_mod.connect(config.db_path)
    try:
        row = conn.execute(
            """
            SELECT chunk_uid, file_path, chunk_type, symbol_name, symbol_path,
                   signature, start_line, end_line, context_json, content
              FROM chunks
             WHERE chunk_uid = ? AND deleted_at IS NULL
            """,
            (chunk_uid,),
        ).fetchone()
    finally:
        db_mod.close(conn)
    if row is None:
        return {"error": "chunk not found", "chunk_uid": chunk_uid}
    return dict(row)


# ---------- Static surface (used by `--describe` and by FastMCP registration) ----------


# Read-only tool descriptions — always exposed.
_READ_TOOL_DESCRIPTIONS: dict[str, str] = {
    "search_text": "Lexical search (ripgrep fast path, python-re fallback). Returns hits and an rg_resolution trail.",
    "search_query": "Ranked FTS5 retrieval (BM25) over chunks; symbol-name hits rank above body.",
    "search_ast": "Tree-sitter structural query for Python. Pattern is a bundled name (class|function|method|call|import|...) or raw S-expression.",
    "find_symbol": "Look up a symbol by canonical name, display name, or substring. Set references=True to include up to 50 call-site occurrences.",
    "impact": "Blast-radius analysis: walks inbound calls/inherits/contains (+imports) from a symbol. Returns impacted_symbols + impacted_files + rationale.",
    "affected_tests": "Tests that reach a symbol (direct + transitive). Returns affected_tests plus a ready-to-run pytest invocation.",
    "doctor": "Index health snapshot: parse_status, semantic_sources, relation counts, FTS consistency + rebuild recommendation.",
    "ask": "Natural-language query synthesis. Pass a question string (e.g. 'who calls reindex', 'tests for apply_schema', 'find code like jwt expiry'); returns intent classification + the right primitive's results + a one-paragraph narrative.",
}

# Mutating tool descriptions — only exposed when --allow-writes is passed.
# The "MUTATING —" prefix makes the warning visible to agents in the tool list.
_WRITE_TOOL_DESCRIPTIONS: dict[str, str] = {
    "update": "MUTATING — reindexes the repo (takes the writer lock). `files=[...]` for targeted update; `all=True` for a full sweep. Empty call pulses the pipeline.",
    "rebuild_fts": "MUTATING — drops + recreates chunks_fts to prune tombstone drift. Holds the writer lock while running.",
}


def _tool_descriptions(*, allow_writes: bool) -> dict[str, str]:
    """Single source of truth for the exposed tool surface.

    `describe_surface()` and `_build_fastmcp()` both consume this so the
    advertised surface and the actually-registered surface can never drift.
    """
    if allow_writes:
        return {**_READ_TOOL_DESCRIPTIONS, **_WRITE_TOOL_DESCRIPTIONS}
    return dict(_READ_TOOL_DESCRIPTIONS)


# Back-compat alias. External callers (tests, docs tooling) may still import
# the old name; it now reflects the full-superset surface.
_TOOL_DESCRIPTIONS: dict[str, str] = _tool_descriptions(allow_writes=True)

_RESOURCE_DESCRIPTIONS: list[tuple[str, str]] = [
    (
        "codeindex://repo-map",
        "Compact list of every live symbol (module/class/function/method).",
    ),
    (
        "codeindex://doctor",
        "Snapshot of index health: parse status, relations, FTS drift.",
    ),
    (
        "codeindex://symbol/{canonical}",
        "Symbol lookup by canonical name; includes references.",
    ),
    ("codeindex://chunk/{chunk_uid}", "Canonical chunk content + context_json."),
]


def describe_surface(*, allow_writes: bool = False) -> dict:
    """Return the static surface description. Pure function; used by
    `--describe` and by `tests/test_cli.py`.

    When `allow_writes=False` (the default), mutating tools (`update`,
    `rebuild_fts`) are omitted. The FastMCP registration in
    `_build_fastmcp` uses the same selector so the advertised and
    actually-exposed surfaces stay in sync.
    """
    tools = _tool_descriptions(allow_writes=allow_writes)
    return {
        "server": "code_index",
        "version": _code_index_version,
        "transport": "stdio",
        "read_only": not allow_writes,
        "tools": [{"name": name, "description": desc} for name, desc in tools.items()],
        "resources": [
            {"uri": uri, "description": desc} for uri, desc in _RESOURCE_DESCRIPTIONS
        ],
    }


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

    # Mutating tools — only registered when --allow-writes is set.
    if allow_writes:

        @mcp.tool(description=tools["update"])
        def update(files: list[str] | None = None, all: bool = False) -> dict:
            return _tool_update(config, files, all)

        @mcp.tool(description=tools["rebuild_fts"])
        def rebuild_fts() -> dict:
            return _tool_rebuild_fts(config)

    # Resources (URI templates handled natively by FastMCP).
    @mcp.resource("codeindex://repo-map", description=_RESOURCE_DESCRIPTIONS[0][1])
    def repo_map() -> dict:
        return _resource_repo_map(config)

    @mcp.resource("codeindex://doctor", description=_RESOURCE_DESCRIPTIONS[1][1])
    def doctor_resource() -> dict:
        return _tool_doctor(config)

    @mcp.resource(
        "codeindex://symbol/{canonical}", description=_RESOURCE_DESCRIPTIONS[2][1]
    )
    def symbol_resource(canonical: str) -> dict:
        return _resource_symbol(config, canonical)

    @mcp.resource(
        "codeindex://chunk/{chunk_uid}", description=_RESOURCE_DESCRIPTIONS[3][1]
    )
    def chunk_resource(chunk_uid: str) -> dict:
        return _resource_chunk(config, chunk_uid)

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
