"""CLI smoke tests via build_parser + main() in-process."""

from __future__ import annotations

import json
import textwrap
from io import StringIO
from pathlib import Path

import pytest

from code_index.cli import main


def _tiny_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        textwrap.dedent(
            """
            def hello() -> str:
                return "hello"


            class Thing:
                def do(self) -> int:
                    return 42
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_cli_init_update_symbol_doctor(tmp_path: Path, capsys: pytest.CaptureFixture):
    _tiny_repo(tmp_path)
    # init
    rc = main(["init", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["stats"]["files_parsed"] >= 1

    # update (no-op)
    rc = main(["update", "--root", str(tmp_path), "--all", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["stats"]["chunks_created"] == 0

    # symbol lookup
    rc = main(["symbol", "--root", str(tmp_path), "--json", "Thing"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    names = [r["canonical_name"] for r in payload["results"]]
    assert any("Thing" in n for n in names)

    # doctor
    rc = main(["doctor", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["index_exists"] is True
    assert payload["fts_consistency"]["ok"] is True


def test_mcp_serve_describe_emits_tool_and_resource_surface(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    _tiny_repo(tmp_path)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    rc = main(["mcp-serve", "--root", str(tmp_path), "--describe"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["server"] == "code_index"
    assert payload["transport"] == "stdio"
    tool_names = {t["name"] for t in payload["tools"]}
    # Default surface is READ-ONLY (slice 10, Task D). Mutating tools are
    # exposed only when `--allow-writes` is passed.
    assert {
        "search_text",
        "search_query",
        "search_ast",
        "find_symbol",
        "impact",
        "affected_tests",
        "doctor",
        "code_graph",
        "agent_activity",
    } <= tool_names
    assert "update" not in tool_names
    assert "rebuild_fts" not in tool_names
    resource_uris = {r["uri"] for r in payload["resources"]}
    assert "codeindex://repo-map" in resource_uris
    assert "codeindex://doctor" in resource_uris
    assert "codeindex://graph" in resource_uris


def test_graph_server_subparser_exists():
    from code_index.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["graph-server", "--port", "8767", "--quiet"])
    assert ns.command == "graph-server"
    assert ns.port == 8767
    assert ns.quiet is True
