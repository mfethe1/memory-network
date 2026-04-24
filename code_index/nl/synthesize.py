"""Dispatch a classified Intent to the right primitives and synthesize a
narrative answer.

The output bundle shape is stable — consumers (MCP clients, agents) can
rely on it:

    {
      "question": str,
      "intent": { kind, confidence, target, rationale, ... },
      "primary_tool": str,           # which primitive did the real work
      "supporting_tools": [str],     # any other primitives consulted
      "results": dict,               # tool output (shape depends on tool)
      "narrative": str,              # one paragraph answer for the LLM
      "suggestions": [str],          # follow-up questions the agent could ask
      "limitations": [str],          # what this answer could NOT cover
    }
"""

from __future__ import annotations

import sqlite3
from typing import Any

from code_index.nl.classify import Intent, IntentKind, classify


def _find_primary_symbol(conn: sqlite3.Connection, target: str) -> dict | None:
    """Resolve `target` to the most-likely symbol row. Same ordering as
    `symbol_search.lookup` but returns only the top hit."""
    from code_index.search.symbol_search import lookup

    rows = lookup(conn, target, limit=1)
    return rows[0] if rows else None


def _with_limits(msg: str) -> list[str]:
    return [msg]


def _dispatch(config, conn: sqlite3.Connection, intent: Intent) -> dict[str, Any]:
    """Return {primary_tool, supporting_tools, results, limitations, suggestions}
    for this intent. Never raises — every branch returns a usable bundle."""
    out: dict[str, Any] = {
        "primary_tool": None,
        "supporting_tools": [],
        "results": {},
        "limitations": [],
        "suggestions": [],
    }

    if intent.kind == IntentKind.UNKNOWN or not intent.target:
        # No target → we can still do useful work for `overview` / `health`.
        if intent.kind == IntentKind.HEALTH:
            from code_index.commands.doctor_cmd import (
                _fts_consistency,
                _language_counts,
                _parse_status_counts,
                _relation_counts,
                _semantic_source_counts,
            )

            out["primary_tool"] = "doctor"
            out["results"] = {
                "parse_status": _parse_status_counts(conn),
                "semantic_sources": _semantic_source_counts(conn),
                "languages": _language_counts(conn),
                "relations": _relation_counts(conn),
                "fts_consistency": _fts_consistency(conn),
            }
            return out
        if intent.kind == IntentKind.OVERVIEW:
            from code_index.commands.repo_map_cmd import build_repo_map

            out["primary_tool"] = "repo-map"
            out["results"] = build_repo_map(conn, limit=intent.limit or 20)
            return out
        # Truly unknown: run both `query` (BM25) and `similar` (embedding)
        # as a general "best-effort retrieval" so the agent still gets data.
        out["primary_tool"] = "query"
        out["supporting_tools"] = ["similar"]
        out["limitations"].append(
            "question shape wasn't recognized; falling back to ranked + semantic search"
        )
        # We don't pass a query — the agent gave us none. Just return empty.
        out["results"] = {"query": None, "hits": []}
        out["suggestions"] = [
            "try `where is <name>`",
            "try `who calls <name>`",
            "try `find code like <phrase>`",
        ]
        return out

    target = intent.target

    # Resolve a concrete symbol for kinds that need one.
    need_symbol = intent.kind in {
        IntentKind.WHERE,
        IntentKind.CALLERS,
        IntentKind.IMPACT,
        IntentKind.TESTS,
        IntentKind.REFERENCES,
    }
    primary_symbol = _find_primary_symbol(conn, target) if need_symbol else None

    if intent.kind == IntentKind.WHERE:
        from code_index.search.symbol_search import lookup

        out["primary_tool"] = "symbol"
        out["results"] = {"query": target, "results": lookup(conn, target, limit=5)}
        return out

    if intent.kind == IntentKind.REFERENCES:
        from code_index.search.symbol_search import lookup

        out["primary_tool"] = "symbol"
        out["results"] = {
            "query": target,
            "results": lookup(conn, target, limit=5, include_references=True),
        }
        return out

    if intent.kind == IntentKind.CALLERS:
        # Direct callers only: impact --max-depth 1 --no-imports, filtered
        # to the 1-hop high-confidence set.
        from code_index.commands.impact_cmd import _resolve_target, compute_impact

        candidates = _resolve_target(conn, target)
        if not candidates:
            out["primary_tool"] = "impact"
            out["results"] = {"error": "no matching symbol", "query": target}
            return out
        impact = compute_impact(
            conn, int(candidates[0]["symbol_pk"]), max_depth=1, include_imports=False
        )
        out["primary_tool"] = "impact"
        out["results"] = impact
        out["limitations"] = impact.get("limitations", [])
        return out

    if intent.kind == IntentKind.IMPACT:
        from code_index.commands.impact_cmd import _resolve_target, compute_impact

        candidates = _resolve_target(conn, target)
        if not candidates:
            out["primary_tool"] = "impact"
            out["results"] = {"error": "no matching symbol", "query": target}
            return out
        impact = compute_impact(
            conn, int(candidates[0]["symbol_pk"]), max_depth=2, include_imports=True
        )
        out["primary_tool"] = "impact"
        out["results"] = impact
        out["limitations"] = impact.get("limitations", [])
        return out

    if intent.kind == IntentKind.TESTS:
        from code_index.commands.tests_cmd import _affected, _resolve_input
        from code_index.runners.pytest import build_pytest_invocation

        candidates = _resolve_input(conn, target)
        if not candidates:
            out["primary_tool"] = "tests"
            out["results"] = {"error": "no matching symbol", "query": target}
            return out
        tsym = candidates[0]
        affected = _affected(conn, int(tsym["symbol_pk"]))
        out["primary_tool"] = "tests"
        out["results"] = {
            "target": {
                "canonical_name": tsym["canonical_name"],
                "symbol_uid": tsym["symbol_uid"],
            },
            "affected_tests": affected,
            "runner": build_pytest_invocation(affected),
        }
        return out

    if intent.kind == IntentKind.SIMILAR:
        from code_index.embeddings import (
            DEFAULT_MODEL,
            availability_report,
            get_backend,
            semantic_search,
        )

        report = availability_report()
        if not report["available"]:
            out["primary_tool"] = "similar"
            out["results"] = {"error": "no embedding backend installed"}
            out["limitations"].append(
                "install fastembed or sentence-transformers to enable `similar`"
            )
            return out
        try:
            backend = get_backend(DEFAULT_MODEL)
            hits = semantic_search(
                conn, backend, target, limit=intent.limit, language=intent.language
            )
            out["primary_tool"] = "similar"
            out["results"] = {"query": target, "hits": hits}
        except Exception as exc:
            out["primary_tool"] = "similar"
            out["results"] = {"error": f"embedding backend failed: {exc!r}"}
        return out

    if intent.kind == IntentKind.STRUCTURAL:
        from code_index.structural import ts_python

        if not ts_python.available():
            out["primary_tool"] = "query-ast"
            out["results"] = {"error": "tree-sitter not installed"}
            return out
        # Map the keyword to a bundled query alias.
        bundled = {
            "classes": "class",
            "class": "class",
            "functions": "function",
            "function": "function",
            "methods": "method",
            "method": "method",
            "calls": "call",
            "call": "call",
            "imports": "import",
            "import": "import",
            "decorators": "decorator",
            "decorator": "decorator",
        }
        alias = bundled.get(target.lower())
        if alias is None:
            out["primary_tool"] = "query-ast"
            out["results"] = {"error": f"no bundled query for '{target}'"}
            return out
        # Walk repo python files.
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
            result = ts_python.query_files(files, alias)
        except Exception as exc:
            out["primary_tool"] = "query-ast"
            out["results"] = {"error": repr(exc)}
            return out
        out["primary_tool"] = "query-ast"
        out["results"] = {
            "pattern": alias,
            "total": len(result.captures),
            "captures": [
                {
                    "file": c.file_path,
                    "start_line": c.start_line,
                    "capture_name": c.capture_name,
                    "preview": c.text[:100],
                }
                for c in result.captures[: intent.limit]
            ],
        }
        return out

    if intent.kind == IntentKind.LITERAL:
        from code_index.search import lexical

        result = lexical.grep(config, pattern=target, max_count=intent.limit)
        out["primary_tool"] = "grep"
        out["results"] = result
        return out

    if intent.kind == IntentKind.RANKED:
        from code_index.search import fts

        out["primary_tool"] = "query"
        out["results"] = {
            "query": target,
            "results": fts.search(
                conn, target, limit=intent.limit, language=intent.language
            ),
        }
        return out

    if intent.kind == IntentKind.HEALTH:
        from code_index.commands.doctor_cmd import (
            _fts_consistency,
            _language_counts,
            _parse_status_counts,
            _relation_counts,
            _semantic_source_counts,
        )

        out["primary_tool"] = "doctor"
        out["results"] = {
            "parse_status": _parse_status_counts(conn),
            "semantic_sources": _semantic_source_counts(conn),
            "languages": _language_counts(conn),
            "relations": _relation_counts(conn),
            "fts_consistency": _fts_consistency(conn),
        }
        return out

    if intent.kind == IntentKind.OVERVIEW:
        from code_index.commands.repo_map_cmd import build_repo_map

        out["primary_tool"] = "repo-map"
        out["results"] = build_repo_map(conn, limit=intent.limit or 20)
        return out

    out["primary_tool"] = None
    out["results"] = {"error": f"no dispatcher for {intent.kind}"}
    return out


def _narrate(intent: Intent, results: dict) -> str:
    """One-paragraph human-readable summary of what we found."""
    if intent.kind == IntentKind.WHERE:
        rows = results.get("results") or []
        if not rows:
            return f"I couldn't find any symbol matching `{intent.target}`."
        top = rows[0]
        base = (
            f"`{top['canonical_name']}` is defined at "
            f"{top.get('def_file', '?')}:{top.get('def_line', '?')} "
            f"({top.get('kind', '?')})."
        )
        if len(rows) > 1:
            return f"{base} {len(rows) - 1} other matches."
        return base
    if intent.kind == IntentKind.REFERENCES:
        rows = results.get("results") or []
        if not rows:
            return f"No symbol named `{intent.target}` in the index."
        top = rows[0]
        refs = top.get("references") or []
        return (
            f"`{top['canonical_name']}` has {len(refs)} recorded call sites"
            + (f", first at {refs[0]['file']}:{refs[0]['start_line']}" if refs else "")
            + "."
        )
    if intent.kind == IntentKind.CALLERS:
        if "error" in results:
            return f"No symbol named `{intent.target}`."
        summary = results.get("summary") or {}
        return (
            f"`{results['target']['canonical_name']}` has "
            f"{summary.get('direct_callers', 0)} direct callers, "
            f"{summary.get('impacted_symbol_count', 0)} symbols impacted at depth 1."
        )
    if intent.kind == IntentKind.IMPACT:
        if "error" in results:
            return f"No symbol named `{intent.target}`."
        summary = results.get("summary") or {}
        return (
            f"Changing `{results['target']['canonical_name']}` impacts "
            f"{summary.get('impacted_symbol_count', 0)} symbols across "
            f"{summary.get('impacted_file_count', 0)} files "
            f"(depth ≤ {results['parameters']['max_depth']}, direct callers: "
            f"{summary.get('direct_callers', 0)})."
        )
    if intent.kind == IntentKind.TESTS:
        if "error" in results:
            return f"No symbol named `{intent.target}`."
        tests = results.get("affected_tests") or []
        runner = (results.get("runner") or {}).get("node_ids") or []
        return (
            f"`{results['target']['canonical_name']}` is exercised by "
            f"{len(tests)} tests; pytest invocation has {len(runner)} node ids."
        )
    if intent.kind == IntentKind.SIMILAR:
        if "error" in results:
            return f"Semantic search unavailable: {results['error']}."
        hits = results.get("hits") or []
        if not hits:
            return (
                f"No embedded chunks matched `{intent.target}`. Did you run "
                f"`code_index embed`?"
            )
        top = hits[0]
        return (
            f"Top semantic match for `{intent.target}`: "
            f"{top.get('symbol_path') or top.get('symbol_name')} at "
            f"{top['file_path']}:{top['start_line']} (score {top['score']:.3f}); "
            f"{len(hits)} hits total."
        )
    if intent.kind == IntentKind.STRUCTURAL:
        if "error" in results:
            return f"Structural search failed: {results['error']}."
        return (
            f"Found {results.get('total', 0)} `{results.get('pattern')}` nodes; "
            f"returning top {len(results.get('captures') or [])}."
        )
    if intent.kind == IntentKind.LITERAL:
        return (
            f"{results.get('count', 0)} hits for `{intent.target}` via "
            f"{results.get('engine', '?')}."
        )
    if intent.kind == IntentKind.RANKED:
        rows = results.get("results") or []
        return (
            f"{len(rows)} ranked BM25 hits for `{intent.target}`"
            + (f"; top: {rows[0].get('symbol_path')}" if rows else "")
            + "."
        )
    if intent.kind == IntentKind.HEALTH:
        fts = results.get("fts_consistency") or {}
        rel = results.get("relations") or {}
        return (
            f"Index shows {rel.get('calls', 0)} calls, {rel.get('contains', 0)} contains, "
            f"{rel.get('imports', 0)} imports; FTS drift {fts.get('drift', 0)} "
            f"({'ok' if fts.get('ok') else 'rebuild recommended'})."
        )
    if intent.kind == IntentKind.OVERVIEW:
        syms = results.get("symbols") or []
        return (
            f"Repo map returned top {len(syms)} symbols by centrality"
            + (f"; leader: {syms[0]['canonical_name']}" if syms else "")
            + "."
        )
    if intent.kind == IntentKind.UNKNOWN:
        return (
            "I couldn't match this question to a known pattern. Try "
            "`where is <name>`, `who calls <name>`, `tests for <name>`, "
            "or `find code like <phrase>`."
        )
    return "Completed."


def _suggestions(intent: Intent) -> list[str]:
    if intent.target is None:
        return []
    t = intent.target
    if intent.kind == IntentKind.WHERE:
        return [f"who calls {t}", f"tests for {t}", f"call sites of {t}"]
    if intent.kind == IntentKind.CALLERS:
        return [f"what breaks if I change {t}", f"tests for {t}"]
    if intent.kind == IntentKind.IMPACT:
        return [f"tests for {t}", f"call sites of {t}"]
    if intent.kind == IntentKind.TESTS:
        return [f"who calls {t}", f"impact of {t}"]
    if intent.kind == IntentKind.REFERENCES:
        return [f"who calls {t}", f"tests for {t}"]
    if intent.kind == IntentKind.SIMILAR:
        return [f"where is {t}", f"find all functions"]
    return []


def answer(config, conn: sqlite3.Connection, question: str) -> dict[str, Any]:
    """Main entry point. Classify the question, dispatch, narrate, return
    the full bundle. Safe to call concurrently — reads only."""
    intent = classify(question)
    dispatch = _dispatch(config, conn, intent)
    return {
        "question": question,
        "intent": {
            "kind": intent.kind.value,
            "confidence": intent.confidence,
            "target": intent.target,
            "language": intent.language,
            "limit": intent.limit,
            "rationale": intent.rationale,
            "matched_pattern": intent.matched_pattern,
            "unknown_reason": intent.unknown_reason,
        },
        "primary_tool": dispatch["primary_tool"],
        "supporting_tools": dispatch["supporting_tools"],
        "results": dispatch["results"],
        "narrative": _narrate(intent, dispatch["results"]),
        "suggestions": _suggestions(intent) + dispatch["suggestions"],
        "limitations": dispatch["limitations"],
    }
