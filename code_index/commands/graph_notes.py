"""Durable user notes for the code graph."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def notes_path(root: Path) -> Path:
    return root / ".code_index" / "graph-notes.json"


def _empty_payload(root: Path) -> dict[str, Any]:
    return {
        "kind": "code_index_graph_notes",
        "root": str(root),
        "updated_at": None,
        "notes": [],
    }


def read_notes(root: Path) -> dict[str, Any]:
    path = notes_path(root)
    if not path.exists():
        return _empty_payload(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_payload(root)
    if not isinstance(payload, dict):
        return _empty_payload(root)
    notes = payload.get("notes")
    if not isinstance(notes, list):
        notes = []
    clean_notes = [note for note in notes if isinstance(note, dict)]
    payload["kind"] = "code_index_graph_notes"
    payload["root"] = str(root)
    payload["notes"] = clean_notes
    return payload


def notes_by_node(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for note in read_notes(root).get("notes", []):
        node_id = note.get("node_id")
        if isinstance(node_id, str) and node_id:
            out[node_id] = note
    return out


def graph_notes_block(root: Path) -> dict[str, Any]:
    payload = read_notes(root)
    by_node = notes_by_node(root)
    return {
        "path": str(notes_path(root)),
        "updated_at": payload.get("updated_at"),
        "items": payload.get("notes", []),
        "by_node": by_node,
        "count": len(by_node),
    }


def upsert_note(root: Path, note: dict[str, Any]) -> dict[str, Any]:
    node_id = str(note.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")
    now = _now_iso()
    payload = read_notes(root)
    existing = {
        str(item.get("node_id")): item
        for item in payload.get("notes", [])
        if item.get("node_id")
    }
    value = str(note.get("note") or "").strip()
    if value:
        existing[node_id] = {
            "node_id": node_id,
            "path": note.get("path"),
            "node_kind": note.get("node_kind") or note.get("kind"),
            "care_level": note.get("care_level"),
            "note": value,
            "summary": note.get("summary"),
            "updated_at": now,
        }
    else:
        existing.pop(node_id, None)
    next_payload = {
        "kind": "code_index_graph_notes",
        "root": str(root),
        "updated_at": now,
        "notes": sorted(existing.values(), key=lambda item: item["node_id"]),
    }
    path = notes_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(next_payload, indent=2), encoding="utf-8")
    return existing.get(node_id, {"node_id": node_id, "note": "", "updated_at": now})
