"""Static MCP tool/resource surface for `code_index mcp-serve`."""

from __future__ import annotations

from code_index import __version__ as _code_index_version




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
    "code_graph": "Read-only file/directory graph projection with cross-file relation edges, importance scores, care guidance, summaries, and optional embedded source code.",
    "agent_activity": "Read recent agent runs/events that the graph uses for live active-file highlighting and activity trails.",
}

# Mutating tool descriptions — only exposed when --allow-writes is passed.
# The "MUTATING —" prefix makes the warning visible to agents in the tool list.
_WRITE_TOOL_DESCRIPTIONS: dict[str, str] = {
    "update": "MUTATING — reindexes the repo (takes the writer lock). `files=[...]` for targeted update; `all=True` for a full sweep. Empty call pulses the pipeline.",
    "rebuild_fts": "MUTATING — drops + recreates chunks_fts to prune tombstone drift. Holds the writer lock while running.",
    "agent_start": "MUTATING — records the start of an agent run for graph/live activity tracking.",
    "agent_event": "MUTATING — records an agent event (read/edit/test/note/tool/navigate/status) against an optional file and symbol.",
    "agent_end": "MUTATING — marks an agent run completed, failed, or cancelled.",
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
        "codeindex://graph",
        "Read-only file graph JSON without embedded source code.",
    ),
    (
        "codeindex://symbol/{canonical}",
        "Symbol lookup by canonical name; includes references.",
    ),
    ("codeindex://chunk/{chunk_uid}", "Canonical chunk content + context_json."),
    ("codeindex://agent-activity", "Recent agent runs/events for graph overlays."),
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
