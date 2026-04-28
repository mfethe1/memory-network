"""Task-aware context packets for agent handoff.

The CLI parser is wired elsewhere; this module intentionally exposes a
`run(args)` entrypoint and a reusable `build_context_packet(...)` helper.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.commands.graph_notes import graph_notes_block
from code_index.commands.repo_map_cmd import build_repo_map
from code_index.search import fts


DEFAULT_BUDGET_TOKENS = 1200
DEFAULT_LIMIT = 8
PREVIEW_CHARS = 420


def build_context_packet(
    config: cfg_mod.Config,
    task: str,
    *,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    selected_nodes: list[str] | None = None,
    selected_paths: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Build a compact, JSON-serializable context packet for `task`.

    Raises FileNotFoundError when the configured index does not exist; `run`
    turns that into the command-style "exit 2" behavior.
    """

    task_text = str(task or "").strip()
    if not config.db_path.exists():
        raise FileNotFoundError(
            f"no index at {config.index_dir}. run `code_index init` first."
        )

    selected_nodes = _as_list(selected_nodes)
    selected_paths = _as_list(selected_paths)
    limit = max(0, int(limit))
    budget_tokens = int(budget_tokens or 0)

    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        notes = _compact_notes(graph_notes_block(config.root), limit=limit)
        event_limit = max(limit * 4, 12) if limit > 0 else 0
        file_limit = min(max(limit, 1), 8) if limit > 0 else 0
        packet: dict[str, Any] = {
            "kind": "code_index_context_packet",
            "task": task_text,
            "root": str(config.root),
            "repo_map": _repo_map(conn, limit=limit),
            "selected_paths": [
                _path_summary(conn, config, path, notes)
                for path in _normalize_paths(config, selected_paths)
            ],
            "selected_nodes": [
                _node_summary(conn, config, node, notes)
                for node in selected_nodes
                if str(node).strip()
            ],
            "matching_chunks": _matching_chunks(conn, task_text, limit=limit),
            "agent_activity": _compact_activity(
                agent_activity.activity_snapshot(
                    conn,
                    event_limit=event_limit,
                    file_limit=file_limit,
                ),
                event_limit=max(limit * 2, 6) if limit > 0 else 0,
                file_limit=file_limit,
            ),
            "graph_notes": notes,
            "handoff_markdown": "",
            "budget": {
                "budget_tokens": budget_tokens,
                "estimated_tokens": 0,
                "truncated": False,
            },
        }
    finally:
        db_mod.close(conn)

    packet["handoff_markdown"] = _build_handoff_markdown(packet)
    packet = _trim_to_budget(packet, budget_tokens)
    packet["handoff_markdown"] = _build_handoff_markdown(packet)
    packet = _trim_to_budget(packet, budget_tokens)
    _refresh_budget(packet, budget_tokens)
    return packet


def run(args: argparse.Namespace) -> int:
    task = str(getattr(args, "task", "") or "").strip()
    if not task:
        print("error: task is required")
        return 2

    root_arg = getattr(args, "root", None)
    root_hint = Path(root_arg).resolve() if root_arg else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)

    try:
        packet = build_context_packet(
            config,
            task,
            budget_tokens=int(
                getattr(args, "budget_tokens", DEFAULT_BUDGET_TOKENS)
                or DEFAULT_BUDGET_TOKENS
            ),
            selected_nodes=_as_list(getattr(args, "selected_node", None)),
            selected_paths=_as_list(getattr(args, "path", None)),
            limit=int(getattr(args, "limit", DEFAULT_LIMIT) or DEFAULT_LIMIT),
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2

    fmt = str(getattr(args, "format", "") or "").lower()
    json_requested = bool(getattr(args, "json", False)) or fmt in {"", "json"}
    if json_requested:
        print(json.dumps(packet, indent=2, sort_keys=True))
    else:
        print(packet["handoff_markdown"])
    return 0


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    return [str(item) for item in value if str(item).strip()]


def _normalize_paths(config: cfg_mod.Config, paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        normalized = _normalize_path(config, raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _normalize_path(config: cfg_mod.Config, raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute():
        try:
            text = path.resolve().relative_to(config.root.resolve()).as_posix()
        except ValueError:
            text = path.as_posix()
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _path_summary(
    conn: sqlite3.Connection,
    config: cfg_mod.Config,
    path: str,
    notes: dict[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT file_path, language, parse_status, semantic_source,
               parser_confidence, indexed_at, deleted_at
          FROM files
         WHERE file_path = ?
         LIMIT 1
        """,
        (path,),
    ).fetchone()
    note = notes.get("by_node", {}).get(f"file:{path}")
    if row is None or row["deleted_at"] is not None:
        return {
            "path": path,
            "indexed": False,
            "language": None,
            "parse_status": "missing",
            "semantic_source": None,
            "parser_confidence": None,
            "symbols": [],
            "chunks": [],
            "note": note,
        }
    symbols = _symbols_for_path(conn, path, limit=8)
    return {
        "path": path,
        "indexed": True,
        "language": row["language"],
        "parse_status": row["parse_status"],
        "semantic_source": row["semantic_source"],
        "parser_confidence": row["parser_confidence"],
        "symbols": symbols,
        "chunks": _chunks_for_path(conn, path, limit=4),
        "note": note,
        "absolute_path": str(config.root / path),
    }


def _repo_map(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    if limit <= 0:
        return {"symbols": []}
    return build_repo_map(conn, limit=limit)


def _symbols_for_path(
    conn: sqlite3.Connection, path: str, *, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.symbol_uid, s.canonical_name, s.kind, s.signature_norm,
               o.start_line, o.end_line
          FROM occurrences o
          JOIN symbols s ON s.symbol_pk = o.symbol_pk
          JOIN files f ON f.file_pk = o.file_pk
         WHERE f.file_path = ?
           AND o.role = 'definition'
           AND s.deleted_at IS NULL
           AND f.deleted_at IS NULL
         ORDER BY o.start_line ASC, s.canonical_name ASC
         LIMIT ?
        """,
        (path, max(0, int(limit))),
    ).fetchall()
    return [
        {
            "symbol_uid": row["symbol_uid"],
            "canonical_name": row["canonical_name"],
            "kind": row["kind"],
            "signature": row["signature_norm"] or "",
            "start_line": row["start_line"],
            "end_line": row["end_line"],
        }
        for row in rows
    ]


def _chunks_for_path(
    conn: sqlite3.Connection, path: str, *, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT chunk_uid, file_path, language, chunk_type, symbol_name,
               symbol_path, signature, start_line, end_line, content
          FROM chunks
         WHERE file_path = ?
           AND deleted_at IS NULL
         ORDER BY start_line ASC, chunk_uid ASC
         LIMIT ?
        """,
        (path, max(0, int(limit))),
    ).fetchall()
    return [_compact_chunk(row, include_preview=True) for row in rows]


def _node_summary(
    conn: sqlite3.Connection,
    config: cfg_mod.Config,
    node_id: str,
    notes: dict[str, Any],
) -> dict[str, Any]:
    node_id = str(node_id).strip()
    note = notes.get("by_node", {}).get(node_id)
    if node_id.startswith("file:"):
        path = _normalize_path(config, node_id.removeprefix("file:"))
        summary = _path_summary(conn, config, path, notes)
        return {
            "node_id": node_id,
            "kind": "file",
            "found": bool(summary["indexed"]),
            "path": path,
            "file": summary,
            "note": note or summary.get("note"),
        }
    if node_id.startswith("chunk:"):
        chunk_uid = node_id.removeprefix("chunk:")
        chunk = _chunk_for_uid(conn, chunk_uid)
        return {
            "node_id": node_id,
            "kind": "chunk",
            "found": chunk is not None,
            "chunk": chunk,
            "note": note,
        }

    symbol_query = (
        node_id.removeprefix("symbol:")
        if node_id.startswith("symbol:")
        else node_id
    )
    symbol = _symbol_for_query(conn, symbol_query)
    if symbol is not None:
        chunks = _chunks_for_symbol(conn, int(symbol.pop("_symbol_pk")), limit=4)
        return {
            "node_id": node_id,
            "kind": "symbol",
            "found": True,
            "symbol": symbol,
            "chunks": chunks,
            "note": note,
        }

    path = _normalize_path(config, node_id)
    path_summary = _path_summary(conn, config, path, notes)
    if path_summary["indexed"]:
        return {
            "node_id": node_id,
            "kind": "file",
            "found": True,
            "path": path,
            "file": path_summary,
            "note": note or path_summary.get("note"),
        }
    chunk = _chunk_for_uid(conn, node_id)
    if chunk is not None:
        return {
            "node_id": node_id,
            "kind": "chunk",
            "found": True,
            "chunk": chunk,
            "note": note,
        }
    return {
        "node_id": node_id,
        "kind": "unknown",
        "found": False,
        "note": note,
    }


def _symbol_for_query(
    conn: sqlite3.Connection, query: str
) -> dict[str, Any] | None:
    query = str(query or "").strip()
    if not query:
        return None
    row = conn.execute(
        """
        SELECT s.symbol_pk, s.symbol_uid, s.kind, s.language,
               s.canonical_name, s.display_name, s.signature_norm,
               s.semantic_source, s.confidence,
               (SELECT f.file_path FROM occurrences o
                  JOIN files f ON f.file_pk = o.file_pk
                 WHERE o.symbol_pk = s.symbol_pk
                   AND o.role = 'definition'
                   AND f.deleted_at IS NULL
                 ORDER BY o.start_line ASC LIMIT 1) AS def_file,
               (SELECT o.start_line FROM occurrences o
                 WHERE o.symbol_pk = s.symbol_pk
                   AND o.role = 'definition'
                 ORDER BY o.start_line ASC LIMIT 1) AS def_line
          FROM symbols s
         WHERE s.deleted_at IS NULL
           AND (s.canonical_name = ? OR s.symbol_uid = ? OR s.display_name = ?)
         ORDER BY s.canonical_name ASC
         LIMIT 1
        """,
        (query, query, query),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT s.symbol_pk, s.symbol_uid, s.kind, s.language,
                   s.canonical_name, s.display_name, s.signature_norm,
                   s.semantic_source, s.confidence,
                   (SELECT f.file_path FROM occurrences o
                      JOIN files f ON f.file_pk = o.file_pk
                     WHERE o.symbol_pk = s.symbol_pk
                       AND o.role = 'definition'
                       AND f.deleted_at IS NULL
                     ORDER BY o.start_line ASC LIMIT 1) AS def_file,
                   (SELECT o.start_line FROM occurrences o
                     WHERE o.symbol_pk = s.symbol_pk
                       AND o.role = 'definition'
                     ORDER BY o.start_line ASC LIMIT 1) AS def_line
              FROM symbols s
             WHERE s.deleted_at IS NULL
               AND (s.canonical_name LIKE ? OR s.display_name LIKE ?)
             ORDER BY LENGTH(s.canonical_name) ASC, s.canonical_name ASC
             LIMIT 1
            """,
            (f"%{query}%", f"%{query}%"),
        ).fetchone()
    if row is None:
        return None
    return {
        "_symbol_pk": int(row["symbol_pk"]),
        "symbol_uid": row["symbol_uid"],
        "canonical_name": row["canonical_name"],
        "display_name": row["display_name"],
        "kind": row["kind"],
        "language": row["language"],
        "signature": row["signature_norm"] or "",
        "semantic_source": row["semantic_source"],
        "confidence": row["confidence"],
        "def_file": row["def_file"],
        "def_line": row["def_line"],
    }


def _chunks_for_symbol(
    conn: sqlite3.Connection, symbol_pk: int, *, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT chunk_uid, file_path, language, chunk_type, symbol_name,
               symbol_path, signature, start_line, end_line, content
          FROM chunks
         WHERE primary_symbol_pk = ?
           AND deleted_at IS NULL
         ORDER BY start_line ASC, chunk_uid ASC
         LIMIT ?
        """,
        (symbol_pk, max(0, int(limit))),
    ).fetchall()
    return [_compact_chunk(row, include_preview=True) for row in rows]


def _chunk_for_uid(
    conn: sqlite3.Connection, chunk_uid: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT chunk_uid, file_path, language, chunk_type, symbol_name,
               symbol_path, signature, start_line, end_line, content
          FROM chunks
         WHERE chunk_uid = ?
           AND deleted_at IS NULL
         LIMIT 1
        """,
        (chunk_uid,),
    ).fetchone()
    if row is None:
        return None
    return _compact_chunk(row, include_preview=True)


def _compact_chunk(
    row: sqlite3.Row, *, include_preview: bool = False
) -> dict[str, Any]:
    item = {
        "chunk_uid": row["chunk_uid"],
        "file_path": row["file_path"],
        "language": row["language"],
        "chunk_type": row["chunk_type"],
        "symbol_name": row["symbol_name"],
        "symbol_path": row["symbol_path"],
        "signature": row["signature"] or "",
        "start_line": row["start_line"],
        "end_line": row["end_line"],
    }
    if include_preview and "content" in row.keys():
        item["content_preview"] = _preview(row["content"] or "")
    return item


def _matching_chunks(
    conn: sqlite3.Connection, task: str, *, limit: int
) -> list[dict[str, Any]]:
    if not task.strip() or limit <= 0:
        return []
    try:
        rows = fts.search(conn, task, limit=limit)
    except sqlite3.OperationalError:
        fallback = " ".join(re.findall(r"[A-Za-z0-9_.]+", task.replace("-", " ")))
        if not fallback:
            return []
        try:
            rows = fts.search(conn, fallback, limit=limit)
        except sqlite3.OperationalError:
            return []
    rows = sorted(
        rows,
        key=lambda row: (
            float(row.get("score", 0.0)),
            row.get("file_path") or "",
            int(row.get("start_line") or 0),
            row.get("chunk_uid") or "",
        ),
    )
    return [
        {
            "chunk_uid": row.get("chunk_uid"),
            "file_path": row.get("file_path"),
            "language": row.get("language"),
            "chunk_type": row.get("chunk_type"),
            "symbol_name": row.get("symbol_name"),
            "symbol_path": row.get("symbol_path"),
            "signature": row.get("signature") or "",
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "score": row.get("score"),
            "snippet": _preview(str(row.get("snippet") or ""), limit=260),
        }
        for row in rows
    ]


def _compact_activity(
    snapshot: dict[str, Any], *, event_limit: int, file_limit: int
) -> dict[str, Any]:
    return {
        "active_runs": [
            {
                "run_id": run.get("run_id"),
                "agent_name": run.get("agent_name"),
                "status": run.get("status"),
                "prompt": _preview(str(run.get("prompt") or ""), limit=220),
                "selected_nodes": list(run.get("selected_nodes") or [])[:8],
                "active_files": list(run.get("active_files") or [])[:8],
                "started_at": run.get("started_at"),
                "updated_at": run.get("updated_at"),
            }
            for run in list(snapshot.get("active_runs") or [])[:5]
        ],
        "recent_events": [
            {
                "run_id": event.get("run_id"),
                "agent_name": event.get("agent_name"),
                "run_status": event.get("run_status"),
                "timestamp": event.get("timestamp"),
                "event_type": event.get("event_type"),
                "file_path": event.get("file_path"),
                "symbol_path": event.get("symbol_path"),
                "message": _preview(str(event.get("message") or ""), limit=220),
            }
            for event in list(snapshot.get("recent_events") or [])[
                : max(0, int(event_limit))
            ]
        ],
        "recent_files": [
            {
                "file_path": item.get("file_path"),
                "last_edited_at": item.get("last_edited_at"),
                "event_source": item.get("event_source"),
                "edit_count": item.get("edit_count"),
                "activity_count": item.get("activity_count"),
                "change_types": item.get("change_types") or {},
                "symbols": list(item.get("symbols") or [])[:6],
                "agents": list(item.get("agents") or [])[:5],
                "last_event_type": item.get("last_event_type"),
                "last_message": _preview(
                    str(item.get("last_message") or ""), limit=180
                ),
            }
            for item in list(snapshot.get("recent_files") or [])[
                : max(0, int(file_limit))
            ]
        ],
        "active_file_claims": [
            {
                "run_id": claim.get("run_id"),
                "agent_name": claim.get("agent_name"),
                "file_path": claim.get("file_path"),
                "mode": claim.get("mode"),
                "reason": _preview(str(claim.get("reason") or ""), limit=160),
                "updated_at": claim.get("updated_at"),
                "expires_at": claim.get("expires_at"),
            }
            for claim in list(snapshot.get("active_claims") or [])[:12]
        ],
    }


def _compact_notes(block: dict[str, Any], *, limit: int) -> dict[str, Any]:
    items = [_compact_note(item) for item in list(block.get("items") or [])]
    items = sorted(items, key=lambda item: item.get("node_id") or "")
    if limit <= 0:
        items = []
    else:
        items = items[: max(limit, 4)]
    by_node = {
        str(item["node_id"]): item
        for item in items
        if item.get("node_id")
    }
    return {
        "path": block.get("path"),
        "updated_at": block.get("updated_at"),
        "count": int(block.get("count") or 0),
        "items": items,
        "by_node": by_node,
    }


def _compact_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": note.get("node_id"),
        "path": note.get("path"),
        "node_kind": note.get("node_kind") or note.get("kind"),
        "care_level": note.get("care_level"),
        "summary": _preview(str(note.get("summary") or ""), limit=160),
        "note": _preview(str(note.get("note") or ""), limit=260),
        "updated_at": note.get("updated_at"),
    }


def _preview(text: str, *, limit: int = PREVIEW_CHARS) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _build_handoff_markdown(packet: dict[str, Any]) -> str:
    lines = ["# Context Packet", "", f"Task: {packet.get('task') or ''}"]

    symbols = (packet.get("repo_map") or {}).get("symbols") or []
    if symbols:
        lines.extend(["", "## Top Symbols"])
        for item in symbols[:5]:
            location = _location(item.get("def_file"), item.get("def_line"))
            lines.append(
                "- "
                f"[{item.get('kind')}] {item.get('canonical_name')}"
                f"{location}"
            )

    paths = packet.get("selected_paths") or []
    if paths:
        lines.extend(["", "## Selected Paths"])
        for item in paths[:5]:
            symbol_names = [
                symbol.get("canonical_name")
                for symbol in item.get("symbols", [])[:3]
                if symbol.get("canonical_name")
            ]
            suffix = f" symbols: {', '.join(symbol_names)}" if symbol_names else ""
            status = "indexed" if item.get("indexed") else "not indexed"
            lines.append(f"- {item.get('path')} ({status}){suffix}")

    nodes = packet.get("selected_nodes") or []
    if nodes:
        lines.extend(["", "## Selected Nodes"])
        for item in nodes[:5]:
            detail = item.get("kind") or "unknown"
            if item.get("symbol"):
                detail = item["symbol"].get("canonical_name") or detail
            elif item.get("path"):
                detail = item.get("path") or detail
            lines.append(f"- {item.get('node_id')} -> {detail}")

    matches = packet.get("matching_chunks") or []
    if matches:
        lines.extend(["", "## Matching Chunks"])
        for item in matches[:5]:
            name = item.get("symbol_path") or item.get("symbol_name") or "chunk"
            location = _location(item.get("file_path"), item.get("start_line"))
            snippet = item.get("snippet") or ""
            suffix = f" - {snippet}" if snippet else ""
            lines.append(f"- {name}{location}{suffix}")

    activity = packet.get("agent_activity") or {}
    active_claims = activity.get("active_file_claims") or []
    if active_claims:
        lines.extend(["", "## Active File Claims"])
        for claim in active_claims[:5]:
            lines.append(
                "- "
                f"{claim.get('agent_name') or 'Agent'} "
                f"{claim.get('mode') or 'claim'} {claim.get('file_path')}: "
                f"{claim.get('reason') or 'active claim'}"
            )
    recent_events = activity.get("recent_events") or []
    if recent_events:
        lines.extend(["", "## Recent Activity"])
        for event in recent_events[:3]:
            path = event.get("file_path") or "(no file)"
            message = event.get("message") or event.get("event_type") or "activity"
            lines.append(
                "- "
                f"{event.get('agent_name') or 'Agent'} "
                f"{event.get('event_type') or 'event'} {path}: {message}"
            )

    notes = (packet.get("graph_notes") or {}).get("items") or []
    if notes:
        lines.extend(["", "## Notes"])
        for note in notes[:4]:
            text = note.get("note") or note.get("summary") or ""
            if text:
                lines.append(f"- {note.get('node_id')}: {text}")

    return "\n".join(lines).strip() + "\n"


def _location(path: Any, line: Any) -> str:
    if not path:
        return ""
    if line is None:
        return f" ({path})"
    return f" ({path}:{line})"


def _trim_to_budget(packet: dict[str, Any], budget_tokens: int) -> dict[str, Any]:
    packet = deepcopy(packet)
    was_truncated = False
    if budget_tokens <= 0:
        _refresh_budget(packet, budget_tokens)
        return packet

    while _estimated_tokens(packet) > budget_tokens:
        if _pop_last(packet, ["matching_chunks"]):
            was_truncated = True
            continue
        if _pop_last(packet, ["repo_map", "symbols"]):
            was_truncated = True
            continue
        if _pop_nested_last(packet, ["selected_paths"], "chunks"):
            was_truncated = True
            continue
        if _pop_nested_last(packet, ["selected_nodes"], "chunks"):
            was_truncated = True
            continue
        if _pop_node_file_chunks(packet):
            was_truncated = True
            continue
        if _pop_nested_last(packet, ["selected_paths"], "symbols"):
            was_truncated = True
            continue
        if _pop_last(packet, ["agent_activity", "recent_events"]):
            was_truncated = True
            continue
        if _pop_last(packet, ["agent_activity", "recent_files"]):
            was_truncated = True
            continue
        if _pop_last(packet, ["agent_activity", "active_runs"]):
            was_truncated = True
            continue
        if _pop_last(packet, ["graph_notes", "items"]):
            was_truncated = True
            packet["graph_notes"]["by_node"] = {
                str(item["node_id"]): item
                for item in packet["graph_notes"].get("items", [])
                if item.get("node_id")
            }
            continue
        markdown = packet.get("handoff_markdown") or ""
        if len(markdown) > 220:
            was_truncated = True
            packet["handoff_markdown"] = markdown[:216].rstrip() + "...\n"
            continue
        break

    _refresh_budget(packet, budget_tokens)
    if was_truncated:
        packet["budget"]["truncated"] = True
    return packet


def _pop_last(packet: dict[str, Any], path: list[str]) -> bool:
    target: Any = packet
    for key in path[:-1]:
        target = target.get(key, {})
        if not isinstance(target, dict):
            return False
    values = target.get(path[-1])
    if isinstance(values, list) and values:
        values.pop()
        return True
    return False


def _pop_nested_last(
    packet: dict[str, Any], list_path: list[str], nested_key: str
) -> bool:
    target: Any = packet
    for key in list_path:
        target = target.get(key, {})
    if not isinstance(target, list):
        return False
    for item in reversed(target):
        values = item.get(nested_key)
        if isinstance(values, list) and values:
            values.pop()
            return True
    return False


def _pop_node_file_chunks(packet: dict[str, Any]) -> bool:
    nodes = packet.get("selected_nodes")
    if not isinstance(nodes, list):
        return False
    for node in reversed(nodes):
        file_summary = node.get("file")
        if not isinstance(file_summary, dict):
            continue
        chunks = file_summary.get("chunks")
        if isinstance(chunks, list) and chunks:
            chunks.pop()
            return True
    return False


def _estimated_tokens(value: dict[str, Any]) -> int:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return (len(rendered) + 3) // 4


def _refresh_budget(packet: dict[str, Any], budget_tokens: int) -> None:
    budget = packet.setdefault("budget", {})
    already_truncated = bool(budget.get("truncated"))
    estimated = _estimated_tokens({k: v for k, v in packet.items() if k != "budget"})
    budget["budget_tokens"] = int(budget_tokens)
    budget["estimated_tokens"] = estimated
    budget["truncated"] = already_truncated or bool(
        budget_tokens > 0 and estimated > budget_tokens
    )
