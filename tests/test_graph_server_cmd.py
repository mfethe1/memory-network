"""HTTP coverage for the live graph server."""

from __future__ import annotations

import argparse
import json
import threading
import textwrap
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index import agent_activity
from code_index.cli import main
from code_index.commands.graph_notes import graph_notes_block
from code_index.commands.graph_server_cmd import _make_handler


def _request_json(url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_graph_server_serves_graph_and_records_notes_and_events(
    tmp_path: Path, capsys
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        textwrap.dedent(
            """
            def value() -> int:
                return 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=["pkg/a.py"],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        graph = _request_json(f"{base_url}/repo-graph.json")
        assert graph["live"]["server"] is True
        assert graph["live"]["events_path"] == "/events"
        assert graph["agent"]["active_files"] == ["pkg/a.py"]
        assert "file:pkg/a.py" in {node["id"] for node in graph["nodes"]}

        saved = _request_json(
            f"{base_url}/api/notes",
            {
                "node_id": "file:pkg/a.py",
                "path": "pkg/a.py",
                "node_kind": "file",
                "care_level": "medium",
                "summary": "a.py",
                "note": "Review this node before editing.",
            },
        )
        assert saved["ok"] is True
        assert saved["note"]["node_id"] == "file:pkg/a.py"
        assert graph_notes_block(tmp_path)["by_node"]["file:pkg/a.py"]["note"] == (
            "Review this node before editing."
        )

        event = _request_json(
            f"{base_url}/api/agent-events",
            {
                "agent_name": "Codex",
                "event_type": "edit",
                "file_path": "pkg/a.py",
                "message": "Editing a.py",
            },
        )
        assert event["ok"] is True
        conn = db_mod.connect(config.db_path)
        try:
            recent = agent_activity.recent_file_activity(conn, limit=1)
        finally:
            db_mod.close(conn)
        assert recent[0]["file_path"] == "pkg/a.py"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
