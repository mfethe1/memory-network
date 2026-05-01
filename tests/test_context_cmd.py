"""Focused tests for task-aware context packets."""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.cli import main
from code_index.commands import context_cmd
from code_index.commands.graph_notes import upsert_note
from code_index.pipeline import reindex


def _write_context_fixture(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "memory.py").write_text(
        textwrap.dedent(
            '''
            class MemoryStore:
                """Store task-aware memory handoff details."""

                def remember(self, task: str) -> str:
                    return f"memory handoff for {task}"

                def packet_title(self, task: str) -> str:
                    return f"handoff packet: {task}"


            def build_memory_handoff(task: str) -> str:
                store = MemoryStore()
                return store.packet_title(task)
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    (pkg / "handoff.py").write_text(
        textwrap.dedent(
            """
            from pkg.memory import build_memory_handoff


            def prepare_context(task: str) -> str:
                return build_memory_handoff(task)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    other = root / "other"
    other.mkdir()
    (other / "outside.py").write_text(
        textwrap.dedent(
            """
            def unrelated_memory_handoff() -> str:
                return "memory handoff from outside the scoped package"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "README.md").write_text("# context fixture\n", encoding="utf-8")


def _init_index(root: Path) -> cfg_mod.Config:
    config = cfg_mod.load(root)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        reindex(conn, config, paths=None, event_source="init")
    finally:
        db_mod.close(conn)
    return config


def _seed_notes_and_activity(root: Path, config: cfg_mod.Config) -> None:
    upsert_note(
        root,
        {
            "node_id": "file:pkg/memory.py",
            "path": "pkg/memory.py",
            "node_kind": "file",
            "care_level": "high",
            "summary": "Memory handoff implementation",
            "note": "Keep context packet behavior deterministic.",
        },
    )
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        run = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Implement context packet",
            selected_nodes=["file:pkg/memory.py"],
        )
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="pkg/memory.py",
            symbol_path="pkg.memory.MemoryStore",
            message="Working on memory handoff packet shape.",
            timestamp="2099-01-01T00:00:00+00:00",
        )
    finally:
        db_mod.close(conn)


def _ready_repo(tmp_path: Path) -> cfg_mod.Config:
    _write_context_fixture(tmp_path)
    config = _init_index(tmp_path)
    _seed_notes_and_activity(tmp_path, config)
    return config


def test_build_context_packet_includes_context_sources(tmp_path: Path):
    config = _ready_repo(tmp_path)

    packet = context_cmd.build_context_packet(
        config,
        "memory handoff",
        budget_tokens=5000,
        selected_paths=["pkg/memory.py"],
        selected_nodes=["file:pkg/memory.py", "pkg.memory.MemoryStore"],
        limit=8,
    )

    assert packet["kind"] == "code_index_context_packet"
    assert packet["task"] == "memory handoff"
    json.dumps(packet)

    repo_symbols = {
        item["canonical_name"] for item in packet["repo_map"]["symbols"]
    }
    assert "pkg.memory.MemoryStore" in repo_symbols
    assert packet["selected_paths"][0]["path"] == "pkg/memory.py"
    assert packet["selected_paths"][0]["indexed"] is True
    assert packet["selected_paths"][0]["note"]["note"] == (
        "Keep context packet behavior deterministic."
    )

    nodes = {item["node_id"]: item for item in packet["selected_nodes"]}
    assert nodes["file:pkg/memory.py"]["found"] is True
    assert nodes["pkg.memory.MemoryStore"]["symbol"]["canonical_name"] == (
        "pkg.memory.MemoryStore"
    )

    assert any(
        item["file_path"] == "pkg/memory.py"
        for item in packet["matching_chunks"]
    )
    assert packet["agent_activity"]["recent_events"][0]["message"] == (
        "Working on memory handoff packet shape."
    )
    assert packet["graph_notes"]["count"] == 1
    assert "Task: memory handoff" in packet["handoff_markdown"]


def test_context_packet_budget_trimming_is_stable(tmp_path: Path):
    config = _ready_repo(tmp_path)

    full_packet = context_cmd.build_context_packet(
        config,
        "memory handoff",
        budget_tokens=0,
        selected_paths=["pkg/memory.py", "pkg/handoff.py"],
        selected_nodes=["file:pkg/memory.py", "pkg.memory.MemoryStore"],
        limit=20,
    )
    trimmed = context_cmd.build_context_packet(
        config,
        "memory handoff",
        budget_tokens=500,
        selected_paths=["pkg/memory.py", "pkg/handoff.py"],
        selected_nodes=["file:pkg/memory.py", "pkg.memory.MemoryStore"],
        limit=20,
    )
    trimmed_again = context_cmd.build_context_packet(
        config,
        "memory handoff",
        budget_tokens=500,
        selected_paths=["pkg/memory.py", "pkg/handoff.py"],
        selected_nodes=["file:pkg/memory.py", "pkg.memory.MemoryStore"],
        limit=20,
    )

    assert trimmed == trimmed_again
    assert trimmed["budget"]["truncated"] is True
    assert trimmed["budget"]["estimated_tokens"] <= full_packet["budget"][
        "estimated_tokens"
    ]
    assert len(json.dumps(trimmed, sort_keys=True)) < len(
        json.dumps(full_packet, sort_keys=True)
    )


def test_context_run_prints_json_packet(
    tmp_path: Path, capsys
):
    _ready_repo(tmp_path)
    args = argparse.Namespace(
        root=str(tmp_path),
        task="memory handoff",
        budget_tokens=2000,
        selected_node=["file:pkg/memory.py"],
        path=["pkg/handoff.py"],
        format="json",
        json=True,
        limit=6,
        scope=None,
    )

    assert context_cmd.run(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["task"] == "memory handoff"
    assert payload["selected_paths"][0]["path"] == "pkg/handoff.py"
    assert payload["selected_nodes"][0]["node_id"] == "file:pkg/memory.py"
    assert "Task: memory handoff" in payload["handoff_markdown"]


def test_context_cli_parser_routes_to_packet(
    tmp_path: Path, capsys
):
    _ready_repo(tmp_path)

    assert (
        main(
            [
                "context",
                "--root",
                str(tmp_path),
                "memory handoff",
                "--path",
                "pkg/memory.py",
                "--selected-node",
                "file:pkg/memory.py",
                "--budget-tokens",
                "2000",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"] == "memory handoff"
    assert payload["selected_paths"][0]["path"] == "pkg/memory.py"


def test_context_cli_scope_defaults_selection_and_retrieval_to_directory(
    tmp_path: Path, capsys
):
    _ready_repo(tmp_path)

    assert (
        main(
            [
                "context",
                "--root",
                str(tmp_path),
                "--scope",
                "pkg",
                "memory handoff",
                "--budget-tokens",
                "3000",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["root"] == str(tmp_path.resolve())
    assert payload["scope"]["path"] == "pkg"
    assert payload["scope"]["explicit"] is True
    assert payload["selected_paths"]
    assert {item["path"] for item in payload["selected_paths"]} == {
        "pkg/__init__.py",
        "pkg/handoff.py",
        "pkg/memory.py",
    }
    assert payload["matching_chunks"]
    assert {
        item["file_path"] for item in payload["matching_chunks"]
    } <= {"pkg/__init__.py", "pkg/handoff.py", "pkg/memory.py"}


def test_context_scope_omitted_preserves_unscoped_defaults(
    tmp_path: Path, capsys
):
    _ready_repo(tmp_path)

    assert (
        main(
            [
                "context",
                "--root",
                str(tmp_path),
                "memory handoff",
                "--budget-tokens",
                "5000",
                "--limit",
                "20",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["scope"]["path"] == "."
    assert payload["scope"]["explicit"] is False
    assert payload["selected_paths"] == []
    assert {
        item["file_path"] for item in payload["matching_chunks"]
    } & {"other/outside.py"}


def test_context_scope_must_stay_inside_root(tmp_path: Path, capsys):
    _ready_repo(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()

    assert (
        main(
            [
                "context",
                "--root",
                str(tmp_path),
                "--scope",
                str(outside),
                "memory handoff",
                "--json",
            ]
        )
        == 2
    )

    assert "error: scope must be inside root" in capsys.readouterr().out


def test_context_selected_path_must_stay_inside_scope(tmp_path: Path, capsys):
    _ready_repo(tmp_path)

    assert (
        main(
            [
                "context",
                "--root",
                str(tmp_path),
                "--scope",
                "pkg",
                "--path",
                "other/outside.py",
                "memory handoff",
                "--json",
            ]
        )
        == 2
    )

    assert "error: selected path is outside scope: other/outside.py" in (
        capsys.readouterr().out
    )


def test_context_run_without_index_returns_clear_error(
    tmp_path: Path, capsys
):
    args = argparse.Namespace(
        root=str(tmp_path),
        task="memory handoff",
        budget_tokens=1200,
        selected_node=[],
        path=[],
        format="json",
        json=True,
        limit=6,
        scope=None,
    )

    assert context_cmd.run(args) == 2
    assert "error: no index" in capsys.readouterr().out
