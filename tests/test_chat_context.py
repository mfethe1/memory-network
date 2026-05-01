from __future__ import annotations

import pytest

from code_index.commands.chat_context import InvalidEditPolicy, normalise_chat_task


def test_normalise_minimal_payload():
    result = normalise_chat_task(
        {
            "message": "review this",
            "selected_paths": ["code_index/commands/graph_server_routes.py"],
            "edit_policy": "review_before_edit",
            "provider": "codex",
        }
    )

    assert result["message"] == "review this"
    assert result["selected_paths"] == ["code_index/commands/graph_server_routes.py"]
    assert result["edit_policy"] == "review_before_edit"
    assert result["selected_symbols"] == []
    assert result["selected_nodes"] == []


def test_normalise_defaults_edit_policy_to_review_before_edit():
    result = normalise_chat_task({"message": "go"})

    assert result["edit_policy"] == "review_before_edit"


def test_normalise_rejects_unknown_edit_policy():
    with pytest.raises(InvalidEditPolicy):
        normalise_chat_task({"message": "go", "edit_policy": "nuke_it"})


def test_normalise_deduplicates_selected_paths():
    result = normalise_chat_task(
        {
            "message": "x",
            "selected_paths": ["a.py", "a.py", "b.py"],
        }
    )

    assert result["selected_paths"] == ["a.py", "b.py"]


def test_normalise_selected_symbols_shape():
    sym = {
        "symbol_uid": "abc",
        "canonical_name": "mod.func",
        "kind": "function",
        "def_file": "mod.py",
        "def_line": 10,
    }

    result = normalise_chat_task({"message": "x", "selected_symbols": [sym]})

    assert result["selected_symbols"][0]["canonical_name"] == "mod.func"
