"""Durable graph note storage."""

from __future__ import annotations

from pathlib import Path

from code_index.commands.graph_notes import graph_notes_block, notes_path, upsert_note


def test_graph_notes_upsert_and_clear(tmp_path: Path):
    saved = upsert_note(
        tmp_path,
        {
            "node_id": "file:pkg/a.py",
            "path": "pkg/a.py",
            "node_kind": "file",
            "care_level": "high",
            "summary": "A file",
            "note": "Please review this file.",
        },
    )
    assert saved["node_id"] == "file:pkg/a.py"
    assert notes_path(tmp_path).exists()

    block = graph_notes_block(tmp_path)
    assert block["count"] == 1
    assert block["by_node"]["file:pkg/a.py"]["note"] == "Please review this file."

    cleared = upsert_note(
        tmp_path,
        {
            "node_id": "file:pkg/a.py",
            "path": "pkg/a.py",
            "note": "",
        },
    )
    assert cleared["note"] == ""
    assert graph_notes_block(tmp_path)["count"] == 0
