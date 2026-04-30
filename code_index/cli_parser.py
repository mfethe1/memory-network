"""code_index CLI entrypoint.

Subcommand dispatch via argparse. Every subcommand accepts --json and --root.
"""

from __future__ import annotations

import argparse
from code_index import __version__
from code_index import agent_activity
from code_index import agent_providers
from code_index.commands import (
    agent_adapter_cmd,
    agent_cmd,
    ask_cmd,
    context_cmd,
    doctor_cmd,
    embed_cmd,
    grep_cmd,
    graph_cmd,
    graph_server_cmd,
    impact_cmd,
    import_scip_cmd,
    init_cmd,
    install_hooks_cmd,
    mcp_serve_cmd,
    query_cmd,
    rebuild_fts_cmd,
    rebuild_tests_cmd,
    repo_map_cmd,
    run_orchestrator_cmd,
    similar_cmd,
    scip_python_cmd,
    symbol_cmd,
    tests_cmd,
    update_cmd,
    watch_cmd,
)


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--root", help="repo root (default: cwd / nearest .code_index/)")
    sub.add_argument("--json", action="store_true", help="emit JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_index",
        description="Local-first hybrid code-memory index. "
        "Symbols are the primary identity (deterministic declaration key); "
        "chunks are projections.",
    )
    parser.add_argument(
        "--version", action="version", version=f"code_index {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser(
        "init", help="scaffold .code_index/ and run a full scan"
    )
    _add_common(p_init)
    p_init.add_argument(
        "--force", action="store_true", help="reparse files even if unchanged"
    )
    p_init.set_defaults(func=init_cmd.run)

    p_update = subparsers.add_parser("update", help="reindex changed files")
    _add_common(p_update)
    p_update.add_argument("--files", nargs="*", help="explicit file list")
    p_update.add_argument("--all", action="store_true", help="scan the whole repo")
    p_update.add_argument(
        "--force", action="store_true", help="reparse files even if unchanged"
    )
    p_update.add_argument(
        "--rename-map",
        dest="rename_map",
        help=(
            'path to a JSON file of [{"old": ..., "new": ...}] canonical-name '
            "pairs to migrate in place (preserves symbol_pk, recomputes symbol_uid) "
            "before reindexing"
        ),
    )
    p_update.set_defaults(func=update_cmd.run)

    p_grep = subparsers.add_parser("grep", help="lexical fast path (ripgrep preferred)")
    _add_common(p_grep)
    p_grep.add_argument("pattern", help="regex or fixed string to search for")
    p_grep.add_argument("--path", help="glob restricting which files are searched")
    p_grep.add_argument(
        "--max-count", type=int, default=50, help="max matches per file"
    )
    p_grep.add_argument("-i", "--ignore-case", action="store_true")
    p_grep.add_argument("-F", "--fixed-strings", action="store_true")
    p_grep.set_defaults(func=grep_cmd.run)

    p_symbol = subparsers.add_parser("symbol", help="look up symbols by name")
    _add_common(p_symbol)
    p_symbol.add_argument("name", help="canonical name, display name, or substring")
    p_symbol.add_argument(
        "--kind", help="filter by kind (class, function, method, module)"
    )
    p_symbol.add_argument("--lang", help="filter by language")
    p_symbol.add_argument("--limit", type=int, default=50)
    p_symbol.add_argument(
        "--references",
        action="store_true",
        help="include up to 50 call-site references in JSON output",
    )
    p_symbol.set_defaults(func=symbol_cmd.run)

    p_query = subparsers.add_parser(
        "query", help="FTS-backed ranked retrieval over chunks"
    )
    _add_common(p_query)
    p_query.add_argument(
        "pattern", nargs="?", help="search terms (or tree-sitter query with --ast)"
    )
    p_query.add_argument("--lang", help="filter by language")
    p_query.add_argument("--type", help="filter by chunk_type")
    p_query.add_argument("--limit", type=int, default=20)
    p_query.add_argument(
        "--ast",
        action="store_true",
        help="structural (tree-sitter) query; pattern is a bundled name or raw S-expression",
    )
    p_query.add_argument(
        "--list-ast-queries",
        action="store_true",
        help="list bundled structural query names and exit",
    )
    p_query.set_defaults(func=query_cmd.run)

    p_doctor = subparsers.add_parser(
        "doctor", help="coverage, drift, and optional-dep report"
    )
    _add_common(p_doctor)
    p_doctor.add_argument(
        "--eval-retrieval",
        action="store_true",
        help="run the local retrieval golden-set eval and include metrics",
    )
    p_doctor.add_argument(
        "--eval-file",
        help="path to a retrieval eval JSON file (default: bundled fixture)",
    )
    p_doctor.add_argument(
        "--eval-limit",
        type=int,
        default=10,
        help="max retrieval results per eval case (default 10)",
    )
    p_doctor.add_argument(
        "--eval-budget-bytes",
        type=int,
        default=20_000,
        help="byte budget per eval case (default 20000)",
    )
    p_doctor.set_defaults(func=doctor_cmd.run)

    p_watch = subparsers.add_parser(
        "watch", help="debounced filesystem reindex (requires watchdog extra)"
    )
    _add_common(p_watch)
    p_watch.add_argument(
        "--debounce-ms",
        type=int,
        default=250,
        help="coalesce events for this many ms of quiet time (default 250)",
    )
    p_watch.set_defaults(func=watch_cmd.run)

    p_impact = subparsers.add_parser(
        "impact",
        help="symbol impact analysis (callers, subclasses, importers)",
    )
    _add_common(p_impact)
    p_impact.add_argument(
        "symbol",
        nargs="?",
        help="target symbol (canonical name, display name, or substring)",
    )
    p_impact.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="transitive walk depth over inbound edges (default 2)",
    )
    p_impact.add_argument(
        "--no-imports",
        action="store_true",
        help="exclude medium-confidence 'imports' edges",
    )
    p_impact.set_defaults(func=impact_cmd.run)

    p_tests = subparsers.add_parser(
        "tests",
        help="affected-tests lookup (direct + transitive via materialized test_edges)",
    )
    _add_common(p_tests)
    p_tests.add_argument(
        "symbol",
        nargs="?",
        help="target: symbol_uid, canonical name, display name, or substring",
    )
    p_tests.add_argument(
        "--direct-only",
        action="store_true",
        help="restrict results to direct edges (depth == 1)",
    )
    p_tests.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="cap reported edges to this depth (does not rematerialize)",
    )
    p_tests.add_argument(
        "--runner",
        help="emit runner-specific invocation data (supported: pytest)",
    )
    p_tests.add_argument(
        "--runner-json",
        action="store_true",
        help="with --runner pytest, emit invocation JSON instead of node ids",
    )
    p_tests.set_defaults(func=tests_cmd.run)

    p_rebuild = subparsers.add_parser(
        "rebuild-fts",
        help="prune tombstone drift from the FTS index",
    )
    _add_common(p_rebuild)
    p_rebuild.set_defaults(func=rebuild_fts_cmd.run)

    p_rebuild_tests = subparsers.add_parser(
        "rebuild-tests",
        help="force a full rebuild of the test_edges table",
    )
    _add_common(p_rebuild_tests)
    p_rebuild_tests.set_defaults(func=rebuild_tests_cmd.run)

    p_embed = subparsers.add_parser(
        "embed",
        help="populate or refresh semantic embeddings for chunks",
    )
    _add_common(p_embed)
    p_embed.add_argument("--model", help="override the embedding model name")
    p_embed.add_argument("--batch", type=int, default=32)
    p_embed.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap how many chunks to embed in this run",
    )
    p_embed.add_argument(
        "--refresh",
        action="store_true",
        help="drop existing rows for (provider, model) first",
    )
    p_embed.set_defaults(func=embed_cmd.run)

    p_similar = subparsers.add_parser(
        "similar",
        help="semantic (embedding) retrieval over embedded chunks",
    )
    _add_common(p_similar)
    p_similar.add_argument("query", nargs="?", help="query string")
    p_similar.add_argument("--model", help="override the embedding model name")
    p_similar.add_argument("--limit", type=int, default=10)
    p_similar.add_argument("--lang", help="filter by language")
    p_similar.add_argument("--type", help="filter by chunk_type")
    p_similar.set_defaults(func=similar_cmd.run)

    p_ask = subparsers.add_parser(
        "ask",
        help="natural-language query: maps 'who calls X' / 'tests for X' / 'find code like Y' to the right primitive",
    )
    _add_common(p_ask)
    p_ask.add_argument("question", nargs="?", help="question in quotes")
    p_ask.add_argument(
        "--no-fallback",
        action="store_true",
        help="disable retrieval-broker fallback for unknown questions",
    )
    p_ask.set_defaults(func=ask_cmd.run)

    p_context = subparsers.add_parser(
        "context",
        help="build a task-aware context and handoff packet for coding agents",
    )
    _add_common(p_context)
    p_context.add_argument("task", nargs="?", help="task or question to package")
    p_context.add_argument(
        "--budget-tokens",
        type=int,
        default=1200,
        help="approximate token budget for the packet (default 1200)",
    )
    p_context.add_argument(
        "--selected-node",
        action="append",
        default=[],
        help="graph node id to include; repeatable",
    )
    p_context.add_argument(
        "--path",
        action="append",
        default=[],
        help="repo-relative file path to include; repeatable",
    )
    p_context.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="output format (default: json)",
    )
    p_context.add_argument(
        "--limit",
        type=int,
        default=8,
        help="max items per section before budget trimming (default 8)",
    )
    p_context.set_defaults(func=context_cmd.run)

    p_repo_map = subparsers.add_parser(
        "repo-map",
        help="Aider-style compact symbol overview ranked by centrality",
    )
    _add_common(p_repo_map)
    p_repo_map.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="output format (default: json)",
    )
    p_repo_map.add_argument(
        "--limit",
        type=int,
        default=100,
        help="max number of symbols to return (default 100)",
    )
    p_repo_map.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="trim lowest-scored entries until ~tokens (chars/4) fits",
    )
    p_repo_map.set_defaults(func=repo_map_cmd.run)

    p_graph = subparsers.add_parser(
        "graph",
        help="interactive file/directory graph with importance and care guidance",
    )
    _add_common(p_graph)
    p_graph.add_argument(
        "--format",
        choices=["html", "json"],
        default="html",
        help="output format (default: html)",
    )
    p_graph.add_argument(
        "--output",
        help=(
            "write to path (default for html: .code_index/repo-graph.html; "
            "json defaults to stdout)"
        ),
    )
    p_graph.add_argument(
        "--no-code",
        action="store_true",
        help="omit embedded source code from the graph payload",
    )
    p_graph.add_argument(
        "--max-code-bytes",
        type=int,
        default=200_000,
        help="largest file to embed in the HTML code view (default 200000)",
    )
    p_graph.add_argument(
        "--focus",
        nargs="*",
        default=[],
        help="repo-relative files to highlight as active agent work",
    )
    p_graph.add_argument(
        "--agent-name",
        default="Codex",
        help="agent label shown in the graph status panel (default: Codex)",
    )
    p_graph.add_argument(
        "--no-sidecar",
        action="store_true",
        help="do not write a .json sidecar next to HTML output",
    )
    p_graph.add_argument(
        "--watch",
        action="store_true",
        help="keep regenerating the graph output for live review",
    )
    p_graph.add_argument(
        "--watch-interval",
        type=float,
        default=2.0,
        help="seconds between graph regenerations with --watch (default 2.0)",
    )
    p_graph.set_defaults(func=graph_cmd.run)

    p_graph_server = subparsers.add_parser(
        "graph-server",
        help="serve the interactive graph with live SSE updates and note capture",
    )
    _add_common(p_graph_server)
    p_graph_server.add_argument("--host", default="127.0.0.1")
    p_graph_server.add_argument("--port", type=int, default=8767)
    p_graph_server.add_argument(
        "--no-code",
        action="store_true",
        help="omit embedded source code from the graph payload",
    )
    p_graph_server.add_argument(
        "--max-code-bytes",
        type=int,
        default=200_000,
        help="largest file to embed in the HTML code view (default 200000)",
    )
    p_graph_server.add_argument(
        "--focus",
        nargs="*",
        default=[],
        help="repo-relative files to highlight as active agent work",
    )
    p_graph_server.add_argument(
        "--agent-name",
        default="Codex",
        help="agent label shown in the graph status panel (default: Codex)",
    )
    p_graph_server.add_argument(
        "--event-interval",
        type=float,
        default=1.0,
        help="seconds between live event checks (default 1.0)",
    )
    p_graph_server.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-request HTTP logs",
    )
    p_graph_server.set_defaults(func=graph_server_cmd.run)

    p_agent = subparsers.add_parser(
        "agent",
        help="record agent runs/events for the live code graph",
    )
    _add_common(p_agent)
    p_agent.add_argument(
        "agent_action",
        choices=[
            "start",
            "event",
            "end",
            "recent",
            "transcript",
            "decision",
            "claims",
            "claim",
            "release",
            "block",
            "board",
            "verify-claim",
        ],
        help="activity action to record or inspect",
    )
    p_agent.add_argument(
        "--run-id",
        help="stable run id; start creates one when omitted",
    )
    p_agent.add_argument(
        "--agent-name",
        default="Codex",
        help="agent label stored with the run/event (default: Codex)",
    )
    p_agent.add_argument(
        "--prompt",
        default="",
        help="run prompt or task text for start/implicit event runs",
    )
    p_agent.add_argument(
        "--selected-node",
        action="append",
        default=[],
        help="graph node id selected for the run; repeatable",
    )
    p_agent.add_argument(
        "--metadata",
        help="JSON object or @path stored on a started run",
    )
    p_agent.add_argument(
        "--type",
        dest="event_type",
        help=(
            "event type for `event` "
            "(read, edit, test, note, tool, navigate, status, decision)"
        ),
    )
    p_agent.add_argument(
        "--file",
        dest="file_path",
        action="append",
        help="repo-relative file path touched by the event or claim; repeatable",
    )
    p_agent.add_argument(
        "--symbol",
        dest="symbol_path",
        help="optional symbol path associated with the event",
    )
    p_agent.add_argument(
        "--message",
        help="human-readable event message",
    )
    p_agent.add_argument(
        "--payload",
        help="JSON object or @path with structured event details",
    )
    p_agent.add_argument(
        "--status",
        help="run status for start/end/status events or decision ledger status",
    )
    p_agent.add_argument(
        "--limit",
        type=int,
        default=100,
        help="event limit for `recent` or `transcript` (default 100)",
    )
    p_agent.add_argument(
        "--file-limit",
        type=int,
        default=8,
        help="recent file activity limit for `recent` (default 8)",
    )
    p_agent.add_argument(
        "--mode",
        default="edit",
        help="claim mode for `claim` or `release` (read, edit, review, test)",
    )
    p_agent.add_argument(
        "--ttl-seconds",
        type=float,
        default=agent_activity.DEFAULT_CLAIM_TTL_SECONDS,
        help="claim lease duration for `claim` (default 1800)",
    )
    p_agent.add_argument(
        "--fence",
        type=int,
        help="fence token for `verify-claim` write lease checks",
    )
    p_agent.add_argument(
        "--blocked-by",
        dest="blocked_by_run_id",
        action="append",
        default=[],
        help="run id that must complete before this run can start; repeatable",
    )
    p_agent.set_defaults(func=agent_cmd.run)

    p_agent_adapter = subparsers.add_parser(
        "agent-adapter",
        help="adapter for graph-submitted agent tasks",
    )
    _add_common(p_agent_adapter)
    p_agent_adapter.add_argument(
        "--mode",
        choices=["auto", "dry-run", "command"],
        default="auto",
        help=(
            "adapter mode; auto uses command when --command, --provider, "
            "CODE_INDEX_AGENT_COMMAND, or CODE_INDEX_AGENT_PROVIDER is set, otherwise dry-run"
        ),
    )
    p_agent_adapter.add_argument(
        "--task-json",
        help="task JSON file path or @path; reads stdin when omitted",
    )
    p_agent_adapter.add_argument(
        "--callback-url",
        help="override task.callback.agent_events_url",
    )
    p_agent_adapter.add_argument(
        "--event-delay",
        type=float,
        default=0.0,
        help="seconds to wait between posted dry-run events",
    )
    p_agent_adapter.add_argument(
        "--command",
        help=(
            "command template for command mode; placeholders include "
            "{message}, {provider_prompt}, {run_id}, {root}, {task_json}, "
            "{last_message}, {provider_prompt_file}, {mcp_config_file}, and "
            "{selected_paths}"
        ),
    )
    p_agent_adapter.add_argument(
        "--provider",
        choices=agent_providers.provider_choices(),
        default="custom",
        help=(
            "provider preset used when --command is omitted; also accepts "
            "CODE_INDEX_AGENT_PROVIDER"
        ),
    )
    p_agent_adapter.add_argument(
        "--cwd",
        help="working directory for command mode (default: task.root)",
    )
    p_agent_adapter.add_argument(
        "--command-timeout",
        type=float,
        default=None,
        help="seconds before command mode marks the run failed",
    )
    p_agent_adapter.add_argument(
        "--max-output-events",
        type=int,
        default=400,
        help="max stdout/stderr lines to post as tool events in command mode",
    )
    p_agent_adapter.add_argument(
        "--fail",
        action="store_true",
        help="finish dry-run mode with failed status",
    )
    p_agent_adapter.set_defaults(func=agent_adapter_cmd.run)

    p_run_orchestrator = subparsers.add_parser(
        "run-orchestrator",
        help="inspect or apply Agent Run lifecycle orchestration",
    )
    _add_common(p_run_orchestrator)
    p_run_orchestrator.add_argument(
        "--apply",
        action="store_true",
        help="apply deterministic lifecycle actions instead of reporting only",
    )
    p_run_orchestrator.add_argument(
        "--known-dead-run-id",
        action="append",
        default=[],
        help="run id whose process liveness is known dead; repeatable",
    )
    p_run_orchestrator.add_argument(
        "--quiet-after-seconds",
        type=float,
        default=600.0,
        help="seconds without activity before health becomes quiet",
    )
    p_run_orchestrator.add_argument(
        "--stale-after-seconds",
        type=float,
        default=agent_activity.DEFAULT_ACTIVE_RUN_MAX_AGE_SECONDS,
        help="seconds without activity before health becomes stale",
    )
    p_run_orchestrator.set_defaults(func=run_orchestrator_cmd.run)

    p_import_scip = subparsers.add_parser(
        "import-scip",
        help="ingest SCIP semantic index data into symbols/occurrences/relations",
    )
    _add_common(p_import_scip)
    p_import_scip.add_argument(
        "--json-index",
        help="path to JSON from `scip print --json`",
    )
    p_import_scip.add_argument(
        "--from",
        dest="index",
        help="path to index.scip; requires the `scip` CLI on PATH",
    )
    p_import_scip.set_defaults(func=import_scip_cmd.run)

    p_scip_python = subparsers.add_parser(
        "scip-python-index",
        help="run scip-python into .code_index/external/scip-python/index.scip",
    )
    _add_common(p_scip_python)
    p_scip_python.add_argument(
        "--project-name",
        help="project name passed to scip-python (default: repo directory name)",
    )
    p_scip_python.add_argument(
        "--project-version",
        help="optional project version passed to scip-python",
    )
    p_scip_python.add_argument(
        "--project-namespace",
        help="optional project namespace passed to scip-python",
    )
    p_scip_python.add_argument(
        "--environment",
        help="optional scip-python environment JSON path",
    )
    p_scip_python.add_argument(
        "--target-only",
        help="optional repo-relative subdirectory passed to scip-python",
    )
    p_scip_python.add_argument(
        "--output-dir",
        help="directory where index.scip should be written (default: .code_index/external/scip-python)",
    )
    p_scip_python.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="additional argument to append to `scip-python index` (repeatable)",
    )
    p_scip_python.add_argument(
        "--import-index",
        action="store_true",
        help="after generation, import index.scip via the `scip` CLI",
    )
    p_scip_python.set_defaults(func=scip_python_cmd.run)

    p_hooks = subparsers.add_parser(
        "install-hooks",
        help="write git hook scripts under .code_index/hooks and set core.hooksPath",
    )
    _add_common(p_hooks)
    p_hooks.add_argument(
        "--uninstall",
        action="store_true",
        help="remove installed code_index hooks and unset core.hooksPath if it points here",
    )
    p_hooks.set_defaults(func=install_hooks_cmd.run)

    p_mcp = subparsers.add_parser(
        "mcp-serve",
        help="MCP (Model Context Protocol) server over the index; requires the `mcp` extra.",
    )
    _add_common(p_mcp)
    p_mcp.add_argument(
        "--describe",
        action="store_true",
        help="print the tool/resource surface as JSON and exit (does not start the loop)",
    )
    p_mcp.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http", "streamable-http"],
        help="transport for the MCP server (default: stdio)",
    )
    p_mcp.add_argument(
        "--bind",
        default=None,
        help="HTTP bind address (default: 127.0.0.1; requires --allow-remote for anything else)",
    )
    p_mcp.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: FastMCP default, typically 8000)",
    )
    p_mcp.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit binding to a non-loopback address (still requires a bearer token)",
    )
    p_mcp.add_argument(
        "--bearer-token",
        default=None,
        help="bearer token required for every HTTP request (overrides env and file)",
    )
    p_mcp.add_argument(
        "--bearer-token-file",
        default=None,
        help="path to a file whose contents are the bearer token",
    )
    p_mcp.add_argument(
        "--allow-writes",
        action="store_true",
        help=(
            "expose mutating tools (update, rebuild_fts) to MCP clients. "
            "Default is READ-ONLY: agents cannot trigger a reindex or FTS "
            "rebuild unless this flag is set."
        ),
    )
    p_mcp.set_defaults(func=mcp_serve_cmd.run)

    return parser
