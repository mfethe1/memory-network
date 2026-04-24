"""`code_index doctor`: coverage, drift, and optional-dep report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.parsers import ctags as ctags_mod
from code_index.parsers import tree_sitter as ts_mod
from code_index.search import lexical
from code_index.structural import ts_python


def _parse_status_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT parse_status, COUNT(*) AS n FROM files WHERE deleted_at IS NULL GROUP BY parse_status"
    ).fetchall()
    return {row["parse_status"]: row["n"] for row in rows}


def _semantic_source_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT semantic_source, COUNT(*) AS n FROM files WHERE deleted_at IS NULL GROUP BY semantic_source"
    ).fetchall()
    return {row["semantic_source"] or "unknown": row["n"] for row in rows}


def _language_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT language, COUNT(*) AS n FROM files WHERE deleted_at IS NULL GROUP BY language"
    ).fetchall()
    return {row["language"] or "unknown": row["n"] for row in rows}


def _relation_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT relation_kind, COUNT(*) AS n FROM relations GROUP BY relation_kind"
    ).fetchall()
    return {row["relation_kind"]: row["n"] for row in rows}


def _fts_consistency(conn: sqlite3.Connection) -> dict:
    # Caveat: on external-content FTS5, COUNT(*) on the virtual table routes
    # to the content table (chunks). The reliable "indexed document" count
    # lives in `chunks_fts_docsize` — one row per indexed document. We join it
    # back against live chunks to compute true drift (tombstoned-but-indexed).
    try:
        live = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        try:
            indexed = conn.execute(
                "SELECT COUNT(*) FROM chunks_fts_docsize"
            ).fetchone()[0]
            drift = conn.execute(
                """
                SELECT COUNT(*) FROM chunks_fts_docsize d
                  LEFT JOIN chunks c
                    ON c.chunk_pk = d.id AND c.deleted_at IS NULL
                 WHERE c.chunk_pk IS NULL
                """
            ).fetchone()[0]
        except sqlite3.OperationalError:
            indexed, drift = None, 0
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": str(exc)}
    rebuild_recommended = drift > 50 or (live > 0 and drift > max(10, int(live * 0.10)))
    # `ok` tracks "no action recommended" — not "zero drift". A handful of
    # tombstoned-but-still-indexed rows is normal between reindexes and is
    # harmless because `query` filters via `chunks.deleted_at IS NULL`.
    # Flip to False only when a rebuild is actually recommended.
    return {
        "ok": not rebuild_recommended,
        "live_chunks": live,
        "total_chunks": total,
        "tombstoned_chunks": total - live,
        "fts_indexed_documents": indexed,
        "drift": drift,
        "rebuild_recommended": rebuild_recommended,
        "rebuild_command": "code_index rebuild-fts",
    }


def _git_summary(conn: sqlite3.Connection) -> dict:
    """Surface git-tracking state from the `files` table. Safe on databases
    that predate schema v3 — older `files` rows just have NULL columns, and
    non-git repos have them all NULL."""
    import time

    try:
        tracked = conn.execute(
            "SELECT COUNT(*) FROM files WHERE deleted_at IS NULL AND git_blob_oid IS NOT NULL"
        ).fetchone()[0]
        untracked = conn.execute(
            "SELECT COUNT(*) FROM files WHERE deleted_at IS NULL AND git_blob_oid IS NULL"
        ).fetchone()[0]
        cutoff = int(time.time()) - 90 * 24 * 3600
        stale = conn.execute(
            """
            SELECT COUNT(*) FROM files
             WHERE deleted_at IS NULL
               AND git_committed_at IS NOT NULL
               AND git_committed_at < ?
            """,
            (cutoff,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # Pre-v3 schema: columns don't exist yet.
        return {"available": False, "reason": "pre-v3 schema (run init to populate)"}
    return {
        "available": tracked > 0,
        "tracked_files": tracked,
        "untracked_files": untracked,
        "stale_90d": stale,
    }


def _embeddings_summary(conn: sqlite3.Connection) -> dict:
    """Coverage + backend availability for the embeddings table."""
    try:
        from code_index.embeddings import availability_report, coverage
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    try:
        cov = coverage(conn)
    except sqlite3.OperationalError as exc:
        return {"available": False, "error": str(exc)}
    avail = availability_report()
    return {
        "available": avail["available"],
        "provider": avail["provider"],
        "model_default": avail["model_default"],
        "backends": avail["backends"],
        **cov,
    }


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _external_tool(name: str, *, commands: tuple[str, ...], role: str, hint: str) -> dict:
    """Report availability for optional external code-intelligence systems."""
    for command in commands:
        path = shutil.which(command)
        if path:
            return {
                "available": True,
                "command": command,
                "path": path,
                "role": role,
                "hint": hint,
            }
    return {
        "available": False,
        "command": commands[0],
        "path": None,
        "role": role,
        "hint": hint,
    }


def _external_tools_report() -> dict:
    """Optional systems we can learn from or incorporate without changing
    the core local-first contract. Kept in `doctor` so agents can make
    evidence-based decisions before suggesting a dependency-heavy workflow.
    """
    return {
        "scip": _external_tool(
            "scip",
            commands=("scip",),
            role="Read/inspect SCIP indexes for high-confidence symbols, occurrences, references, and implementation relationships.",
            hint="Install the SCIP CLI from https://github.com/scip-code/scip/releases; use `code_index import-scip --from index.scip` or pass `--json-index`.",
        ),
        "scip_python": _external_tool(
            "scip-python",
            commands=("scip-python",),
            role="Produce Python SCIP indexes using Pyright-backed semantic analysis.",
            hint="Install with `npm install -g @sourcegraph/scip-python`; keep Python AST as the zero-dependency fallback.",
        ),
        "ast_grep": _external_tool(
            "ast-grep",
            commands=("ast-grep", "sg"),
            role="Optional syntax-aware structural search and rewrite engine.",
            hint="Install ast-grep if `query --ast` needs broader polyglot structural search or codemod previews.",
        ),
        "zoekt": _external_tool(
            "zoekt",
            commands=("zoekt", "zoekt-index", "zoekt-git-index"),
            role="Optional large-repo trigram code-search backend with symbol-aware ranking.",
            hint="Keep ripgrep/SQLite FTS until benchmarks show local search is the bottleneck.",
        ),
        "codeql": _external_tool(
            "codeql",
            commands=("codeql",),
            role="Optional security and data-flow analysis sidecar; useful for diagnostics ingestion, not primary code memory.",
            hint="Use CodeQL for SARIF/diagnostic import after SCIP semantic ingestion is stable.",
        ),
    }


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)

    rg_resolved = lexical.resolve_ripgrep(config)
    structural_report = ts_python.availability_report()

    report: dict = {
        "root": str(config.root),
        "index_dir": str(config.index_dir),
        "index_exists": config.db_path.exists(),
        "python": sys.version.split()[0],
        "ripgrep": rg_resolved.to_dict(),
        "structural_engine": structural_report,
        "identity_model": {
            "fields": [
                "language",
                "kind",
                "canonical_name",
                "signature_norm",
                "container_uid",
            ],
            "refactor_durable": False,
            "migrate_via": "code_index update --rename-map PATH",
            "notes": (
                "symbol_uid is a deterministic declaration key; it is stable "
                "across re-parses of the same declaration but changes when "
                "canonical_name, signature, container, kind, or language "
                "change. Use --rename-map to migrate identity in place "
                "(preserves symbol_pk and downstream FK references)."
            ),
        },
        "optional_deps": {
            "ripgrep": rg_resolved.path is not None,
            "tree_sitter": ts_mod.available(),
            "tree_sitter_python": structural_report["available"],
            "ctags": ctags_mod.available(),
            "watchdog": shutil.which("watchmedo") is not None
            or _module_available("watchdog"),
            "mcp": _module_available("mcp"),
            "jedi": {
                "available": _module_available("jedi"),
                "enabled": bool(getattr(config, "enable_jedi", False)),
            },
        },
        "external_tools": _external_tools_report(),
    }

    if config.db_path.exists():
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.ensure_schema(conn, config)
            stored_version = db_mod.get_schema_version(conn)
            columns_ok, missing = db_mod.expected_column_health(conn)
            report["schema_version"] = stored_version
            report["schema_health"] = {
                "version": stored_version,
                "expected": db_mod.SCHEMA_VERSION,
                "columns_ok": columns_ok,
                "missing": missing,
            }
            report["parse_status"] = _parse_status_counts(conn)
            report["semantic_sources"] = _semantic_source_counts(conn)
            report["languages"] = _language_counts(conn)
            report["chunks_total"] = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
            ).fetchone()[0]
            report["chunks_tombstoned"] = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
            report["symbols_total"] = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE deleted_at IS NULL"
            ).fetchone()[0]
            report["symbols_tombstoned"] = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
            report["edits_recorded"] = conn.execute(
                "SELECT COUNT(*) FROM chunk_edits"
            ).fetchone()[0]
            report["relations"] = _relation_counts(conn)
            report["fts_consistency"] = _fts_consistency(conn)
            report["test_edges"] = conn.execute(
                "SELECT COUNT(*) FROM test_edges"
            ).fetchone()[0]
            report["unresolved_calls_open"] = conn.execute(
                "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NULL"
            ).fetchone()[0]
            report["unresolved_calls_backfilled"] = conn.execute(
                "SELECT COUNT(*) FROM unresolved_calls WHERE resolved_at IS NOT NULL"
            ).fetchone()[0]
            report["git"] = _git_summary(conn)
            report["embeddings"] = _embeddings_summary(conn)
        finally:
            db_mod.close(conn)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"code_index doctor @ {config.root}")
    print(f"  index exists:   {report['index_exists']}")
    if report["index_exists"]:
        print(f"  schema:         {report.get('schema_version')}")
        print(
            f"  chunks:         {report.get('chunks_total')} (tombstoned {report.get('chunks_tombstoned')})"
        )
        print(f"  symbols:        {report.get('symbols_total')}")
        print(f"  relations:      {report.get('relations', {})}")
        print(f"  edits recorded: {report.get('edits_recorded')}")
        ps = report.get("parse_status", {})
        print(f"  parse_status:   {ps}")
        print(f"  languages:      {report.get('languages')}")
        fts = report.get("fts_consistency", {})
        drift = fts.get("drift")
        marker = ""
        if fts.get("rebuild_recommended"):
            marker = "  [drift — run `code_index rebuild-fts`]"
        print(
            f"  fts sync:       live={fts.get('live_chunks')} "
            f"indexed={fts.get('fts_indexed_documents')} drift={drift}{marker}"
        )
        print(f"  test_edges:     {report.get('test_edges')}")
        print(
            f"  unresolved:     {report.get('unresolved_calls_open')} open, "
            f"{report.get('unresolved_calls_backfilled')} backfilled"
        )
    rg = report["ripgrep"]
    if rg["path"]:
        print(
            f"  ripgrep:        {rg['path']} (via {rg['source']}, {rg.get('version') or '?'})"
        )
    else:
        print(f"  ripgrep:        NOT FOUND ({len(rg['tried'])} candidates tried)")
    se = report["structural_engine"]
    print(
        f"  tree-sitter:    {'available' if se['available'] else 'NOT AVAILABLE'}"
        f"{'' if se['available'] else ' — ' + (se.get('reason') or '?')}"
    )
    print("  optional deps:")
    for name, avail in report["optional_deps"].items():
        print(f"    - {name}: {'yes' if avail else 'no'}")
    return 0
