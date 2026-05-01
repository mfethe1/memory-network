"""Integration coverage for the unified Agent Task chat context."""

from __future__ import annotations

from pathlib import Path


def test_full_agent_task_context_contract(tmp_path: Path, capsys, monkeypatch):
    from tests.test_graph_server_cmd import _make_server

    (tmp_path / "mymod.py").write_text(
        "def do_the_thing(x: int) -> str:\n    return str(x)\n",
        encoding="utf-8",
    )

    with _make_server(tmp_path, capsys, monkeypatch) as server:
        server.reindex()
        symbol_resp = server.get_json(
            "/api/symbols?q=do_the_thing&kind=function&limit=5"
        )
        hit = next(
            item
            for item in symbol_resp["results"]
            if item["canonical_name"].endswith("do_the_thing")
        )
        payload = {
            "message": "Review this function and suggest an edit",
            "selected_paths": ["mymod.py"],
            "selected_nodes": ["file:mymod.py"],
            "selected_symbols": [
                {
                    "symbol_uid": hit["symbol_uid"],
                    "canonical_name": hit["canonical_name"],
                    "kind": hit["symbol_kind"],
                    "def_file": hit["def_file"],
                    "def_line": hit["def_line"],
                }
            ],
            "edit_policy": "review_before_edit",
            "provider": "codex",
        }
        resp = server.post_json("/api/agent-task-preflight", payload)

    draft = resp["draft"]
    assert draft["edit_policy"] == "review_before_edit"
    assert any(
        symbol["canonical_name"].endswith("do_the_thing")
        for symbol in draft.get("selected_symbols", [])
    )
    assert draft.get("context_preview", {}).get("selected_file") == "mymod.py"
