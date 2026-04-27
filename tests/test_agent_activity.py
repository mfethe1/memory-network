"""Agent activity records used by the live code graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.cli import main


def _activity_db(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_agent_activity_helpers_track_runs_events_and_recent_files(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Improve the graph",
            selected_nodes=["file:code_index/commands/graph_cmd.py"],
        )
        event = agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="code_index/commands/graph_cmd.py",
            symbol_path="build_graph",
            message="Merged agent events into graph activity.",
            timestamp="2099-01-01T00:00:00+00:00",
        )

        assert event["event_type"] == "edit"
        assert event["file_path"] == "code_index/commands/graph_cmd.py"
        active = agent_activity.active_runs(conn)
        assert active[0]["run_id"] == run["run_id"]
        assert active[0]["active_files"] == ["code_index/commands/graph_cmd.py"]

        files = agent_activity.recent_file_activity(conn)
        assert files[0]["file_path"] == "code_index/commands/graph_cmd.py"
        assert files[0]["change_types"] == {"edit": 1}

        ended = agent_activity.end_run(conn, run_id=run["run_id"])
        assert ended["status"] == "completed"
        assert agent_activity.active_runs(conn) == []
    finally:
        db_mod.close(conn)


def test_agent_cli_records_event_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    rc = main(
        [
            "agent",
            "--root",
            str(tmp_path),
            "start",
            "--agent-name",
            "Codex",
            "--prompt",
            "Patch a.py",
            "--selected-node",
            "file:a.py",
        ]
    )
    start_payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    run_id = start_payload["run"]["run_id"]

    rc = main(
        [
            "agent",
            "--root",
            str(tmp_path),
            "event",
            "--run-id",
            run_id,
            "--type",
            "edit",
            "--file",
            "a.py",
            "--message",
            "Edited a.py",
        ]
    )
    event_payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert event_payload["event"]["file_path"] == "a.py"
    assert event_payload["run"]["active_files"] == ["a.py"]

    rc = main(["agent", "--root", str(tmp_path), "recent", "--limit", "10"])
    recent_payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert recent_payload["recent_events"][0]["event_type"] == "edit"
    assert recent_payload["recent_files"][0]["file_path"] == "a.py"
