"""Acceptance tests for `code_index graph`."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.cli import main
from code_index.commands.graph_notes import upsert_note


def _write_graph_fixture(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "low.py").write_text(
        textwrap.dedent(
            """
            def base(value: int) -> int:
                return value + 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (pkg / "mid.py").write_text(
        textwrap.dedent(
            """
            from pkg.low import base


            def wrapper(value: int) -> int:
                return base(value) * 2
            """
        ).lstrip(),
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_mid.py").write_text(
        textwrap.dedent(
            """
            from pkg.mid import wrapper


            def test_wrapper():
                assert wrapper(2) == 6
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "README.md").write_text("# graph fixture\n", encoding="utf-8")


def test_graph_json_exposes_files_relations_care_and_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _write_graph_fixture(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    upsert_note(
        tmp_path,
        {
            "node_id": "file:pkg/mid.py",
            "path": "pkg/mid.py",
            "node_kind": "file",
            "care_level": "high",
            "summary": "mid",
            "note": "Review wrapper behavior.",
        },
    )
    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        run = agent_activity.start_run(conn, agent_name="Codex")
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="pkg/mid.py",
            symbol_path="pkg.mid.wrapper",
            message="Working on wrapper graph behavior.",
            timestamp="2099-01-01T00:00:00+00:00",
        )
    finally:
        db_mod.close(conn)

    rc = main(
        [
            "graph",
            "--root",
            str(tmp_path),
            "--format",
            "json",
            "--agent-name",
            "Codex",
            "--focus",
            "pkg/mid.py",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["kind"] == "code_index_graph"
    assert payload["schema_version"] == 1
    assert payload["live"]["server"] is False
    assert payload["agent"]["name"] == "Codex"
    assert payload["agent"]["active_files"] == ["pkg/mid.py"]
    assert payload["agent"]["active_runs"][0]["agent_name"] == "Codex"
    assert payload["agent"]["status"] == "working"

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert "dir:pkg" in nodes
    assert "file:pkg/low.py" in nodes
    assert "file:pkg/mid.py" in nodes
    assert nodes["file:pkg/mid.py"]["active_work"] is True
    assert nodes["file:pkg/mid.py"]["care_level"] in {
        "low",
        "medium",
        "high",
        "critical",
    }
    assert nodes["file:pkg/mid.py"]["summary"]
    assert nodes["file:pkg/mid.py"]["code"]["included"] is True
    assert "def wrapper" in nodes["file:pkg/mid.py"]["code"]["content"]
    assert isinstance(payload["summary"]["recent_edits"], list)
    assert isinstance(payload["summary"]["recent_files"], list)
    assert "activity" in payload
    assert isinstance(payload["activity"]["recent_files"], list)
    assert payload["activity"]["agent_events"][0]["file_path"] == "pkg/mid.py"
    assert payload["summary"]["recent_files"][0]["file_path"] == "pkg/mid.py"
    assert payload["summary"]["recent_edits"][0]["event_source"] == "agent:Codex"
    assert payload["notes"]["by_node"]["file:pkg/mid.py"]["note"] == "Review wrapper behavior."
    assert isinstance(nodes["file:pkg/mid.py"]["recent_edits"], list)

    relation_edges = [
        edge
        for edge in payload["edges"]
        if edge["source"] == "file:pkg/mid.py"
        and edge["target"] == "file:pkg/low.py"
        and edge["kind"] != "contains"
    ]
    assert relation_edges
    assert any(
        kind in relation_edges[0]["detail"] for kind in ("imports", "calls")
    )


def test_graph_html_writes_standalone_view(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _write_graph_fixture(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    out_path = tmp_path / "graph.html"
    rc = main(
        [
            "graph",
            "--root",
            str(tmp_path),
            "--output",
            str(out_path),
            "--agent-name",
            "Codex",
            "--focus",
            "pkg/mid.py",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert str(out_path) in out
    html = out_path.read_text(encoding="utf-8")
    sidecar = out_path.with_suffix(".json")
    assert sidecar.exists()
    assert "code_index graph" in html
    assert "pkg/mid.py" in html
    assert "graph-data" in html
    assert "tab-chat" in html
    assert "tab-notes" in html
    assert "tab-edits" in html
    assert "tab-debug" in html
    assert "refresh-graph" in html
    assert "refresh-debug" in html
    assert "live-refresh" in html
    assert "layer-mode" in html
    assert "agent-name" in html
    assert "panel-resizer" in html
    assert "breadcrumb-view" in html
    assert "active-files" in html
    assert "file-claims" in html
    assert "agent-runs" in html
    assert "search-results" in html
    assert "related-files" in html
    assert "nav-parent" in html
    assert "nav-center" in html
    assert "nav-expand-all" in html
    assert "nav-collapse-all" in html
    assert "expandedDirs" in html
    assert "DIRECTORY_EXPANSION_DEFAULT_VERSION" in html
    assert "directoryExpansionDefaultVersion" in html
    assert "directoryExpansionMode" in html
    assert "function setDirectoryExpansionMode" in html
    assert "function allDirectoryIds()" in html
    assert "function defaultExpandedDirectoryIds()" in html
    assert "function expandAllDirectories()" in html
    assert "expandedDirs = new Set(defaultExpandedDirectoryIds())" in html
    reset_section = html[html.index('resetView.addEventListener("click"') :]
    assert "directoryExpansionMode: \"all\"" in reset_section
    assert "expandedDirs = new Set(defaultExpandedDirectoryIds())" in reset_section
    assert "data-nav-tree" in html
    assert "--nav-indent" in html
    assert "zoom-in" in html
    assert "fit-view" in html
    assert "expand-neighborhood" in html
    assert "Layered context" in html
    assert "submit-agent-task" in html
    assert "agent-chat-message" in html
    assert "send-agent-message" in html
    assert "agent-provider" in html
    assert "agent_providers" in html
    assert "agent_runtime" in html
    assert "agentProviderRegistry()" in html
    assert "refreshAgentProviders" in html
    assert "renderAgentRuntimeStatus" in html
    assert "defaultChatProvider" in html
    assert 'addEventListener("connection"' in html
    assert "mergeDynamicEdges" in html
    assert "agent_derived" in html
    registry_fallback = html[
        html.index("function agentProviderRegistry()"):
        html.index("function providerOptionHtml")
    ]
    assert '{ id: "configured", display_name: "Configured adapter" }' in registry_fallback
    assert '{ id: "codex", display_name: "Codex" }' not in registry_fallback
    assert '{ id: "claude", display_name: "Claude" }' not in registry_fallback
    assert '{ id: "kimi", display_name: "Kimi" }' not in registry_fallback
    assert '<option value="codex">Codex CLI</option>' not in html
    assert '<option value="claude">Claude CLI</option>' not in html
    assert '<option value="kimi">Kimi Code CLI</option>' not in html
    assert "agent-execution-strategy" in html
    assert "run-followup-execution-strategy" in html
    assert "agent_swarm" in html
    assert "execution_strategy" in html
    assert "agent_runs_path" in html
    assert "agent_preflight_path" in html
    assert "search_path" in html
    assert "graphTokenKey" in html
    assert "fetchGraphGet" in html
    assert "/api/search" in html
    assert "code_index_graph_view" in html
    assert "run-cancel" in html
    assert "addEventListener(\"agent\"" in html
    assert "setInterval" not in html
    assert "tree-view" in html
    assert "recent-files" in html
    assert "color-scheme: dark" in html
    sidecar_payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_payload["kind"] == "code_index_graph"
    assert sidecar_payload["agent"]["name"] == "Codex"
