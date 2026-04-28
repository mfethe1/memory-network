"""Agent activity records used by the live code graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
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
        claims = agent_activity.active_file_claims(conn)
        assert claims[0]["file_path"] == "code_index/commands/graph_cmd.py"
        assert claims[0]["mode"] == "edit"
        assert claims[0]["run_id"] == run["run_id"]

        files = agent_activity.recent_file_activity(conn)
        assert files[0]["file_path"] == "code_index/commands/graph_cmd.py"
        assert files[0]["change_types"] == {"edit": 1}

        ended = agent_activity.end_run(conn, run_id=run["run_id"])
        assert ended["status"] == "completed"
        assert agent_activity.active_runs(conn) == []
        assert agent_activity.active_file_claims(conn) == []
    finally:
        db_mod.close(conn)


def test_active_runs_filters_stale_working_runs(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        stale = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Old active run",
        )
        conn.execute(
            """
            UPDATE agent_runs
               SET started_at = '2026-01-01T00:00:00+00:00',
                   updated_at = '2026-01-01T00:00:00+00:00'
             WHERE run_id = ?
            """,
            (stale["run_id"],),
        )
        fresh = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Current active run",
        )

        active = agent_activity.active_runs(conn)
        assert [run["run_id"] for run in active] == [fresh["run_id"]]
        assert agent_activity.latest_active_run(conn)["run_id"] == fresh["run_id"]
        unbounded = agent_activity.active_runs(conn, max_age_seconds=None)
        assert {run["run_id"] for run in unbounded} == {
            stale["run_id"],
            fresh["run_id"],
        }
    finally:
        db_mod.close(conn)


def test_active_runs_hide_legacy_orphan_tool_runs(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        orphan = agent_activity.start_run(
            conn,
            agent_name="Codex",
            metadata={"source": "graph-server"},
        )
        agent_activity.record_event(
            conn,
            run_id=orphan["run_id"],
            event_type="tool",
            message="stderr with no run id",
        )
        agent_activity.record_event(
            conn,
            run_id=orphan["run_id"],
            event_type="status",
            message="cancelled fake run",
            payload={"status": "cancelled"},
        )
        real = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Real graph task",
        )

        assert [run["run_id"] for run in agent_activity.active_runs(conn)] == [
            real["run_id"]
        ]
        assert agent_activity.latest_active_run(conn)["run_id"] == real["run_id"]
        assert [run["run_id"] for run in agent_activity.recent_runs(conn)] == [
            real["run_id"]
        ]
        assert {
            run["run_id"]
            for run in agent_activity.recent_runs(conn, include_orphan=True)
        } == {orphan["run_id"], real["run_id"]}
    finally:
        db_mod.close(conn)


def test_archived_runs_hide_from_active_and_recent_lists(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        archived = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Archive this run",
        )
        visible = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Keep this run visible",
        )
        updated = agent_activity.archive_run(conn, run_id=archived["run_id"])

        assert updated["archived_at"]
        assert [run["run_id"] for run in agent_activity.active_runs(conn)] == [
            visible["run_id"]
        ]
        assert [run["run_id"] for run in agent_activity.recent_runs(conn)] == [
            visible["run_id"]
        ]
        assert {
            run["run_id"]
            for run in agent_activity.recent_runs(conn, include_archived=True)
        } == {archived["run_id"], visible["run_id"]}
        assert agent_activity.get_run(conn, archived["run_id"])["archived_at"]
    finally:
        db_mod.close(conn)


def test_run_transcript_orders_events_and_includes_decisions(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Keep a run transcript",
            metadata={"selected_paths": ["pkg/api.py"]},
        )
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="test",
            file_path="tests/test_api.py",
            message="Ran the API tests.",
            timestamp="2026-01-01T00:00:03+00:00",
        )
        agent_activity.record_decision(
            conn,
            run_id=run["run_id"],
            decision="Record decisions as agent events.",
            payload={"rationale": "Keeps the ledger append-only."},
            status="accepted",
            timestamp="2026-01-01T00:00:01+00:00",
        )
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="pkg/api.py",
            message="Added transcript support.",
            timestamp="2026-01-01T00:00:02+00:00",
        )

        transcript = agent_activity.run_transcript(conn, run["run_id"])
        assert transcript is not None
        assert [event["event_type"] for event in transcript["events"]] == [
            "decision",
            "edit",
            "test",
        ]
        assert transcript["decisions"][0]["payload"]["decision"] == (
            "Record decisions as agent events."
        )
        assert transcript["decisions"][0]["payload"]["status"] == "accepted"
        assert transcript["summary"]["event_types"] == {
            "decision": 1,
            "edit": 1,
            "test": 1,
        }
        assert transcript["active_files"] == ["tests/test_api.py", "pkg/api.py"]
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
    assert recent_payload["active_claims"][0]["file_path"] == "a.py"
    assert recent_payload["active_claims"][0]["mode"] == "edit"

    rc = main(
        [
            "agent",
            "--root",
            str(tmp_path),
            "release",
            "--run-id",
            run_id,
            "--file",
            "a.py",
        ]
    )
    release_payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert release_payload["claims"][0]["status"] == "released"

    rc = main(["agent", "--root", str(tmp_path), "claims", "--limit", "10"])
    claims_payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert claims_payload["active_claims"] == []


def test_agent_cli_records_decision_and_transcript_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "agent",
                "--root",
                str(tmp_path),
                "start",
                "--agent-name",
                "Codex",
                "--prompt",
                "Patch a.py",
            ]
        )
        == 0
    )
    run_id = json.loads(capsys.readouterr().out)["run"]["run_id"]

    assert (
        main(
            [
                "agent",
                "--root",
                str(tmp_path),
                "decision",
                "--run-id",
                run_id,
                "--message",
                "Keep the transcript API JSON-first.",
                "--payload",
                '{"rationale":"Graph UI can render it directly."}',
                "--status",
                "accepted",
            ]
        )
        == 0
    )
    decision_payload = json.loads(capsys.readouterr().out)
    assert decision_payload["event"]["event_type"] == "decision"
    assert decision_payload["event"]["payload"]["decision"] == (
        "Keep the transcript API JSON-first."
    )
    assert decision_payload["event"]["payload"]["status"] == "accepted"
    assert decision_payload["run"]["status"] == "working"

    assert (
        main(
            [
                "agent",
                "--root",
                str(tmp_path),
                "transcript",
                "--run-id",
                run_id,
                "--json",
            ]
        )
        == 0
    )
    transcript = json.loads(capsys.readouterr().out)
    assert transcript["run"]["run_id"] == run_id
    assert transcript["events"][0]["event_type"] == "decision"
    assert transcript["decisions"][0]["payload"]["rationale"] == (
        "Graph UI can render it directly."
    )


def test_post_run_suggestions_include_diagnostics_and_affected_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "service.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_service.py").write_text(
        "from pkg.service import value\n\n"
        "def test_value():\n"
        "    assert value() == 1\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        file_pk = conn.execute(
            "SELECT file_pk FROM files WHERE file_path = ?",
            ("pkg/service.py",),
        ).fetchone()["file_pk"]
        conn.execute(
            """
            INSERT INTO diagnostics(
                file_pk, tool, code, severity, start_line, end_line, message, observed_at
            ) VALUES (?, 'test', 'W001', 'warning', 1, 1, 'synthetic warning', '2099-01-01T00:00:00+00:00')
            """,
            (file_pk,),
        )
        run = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Patch service",
            metadata={"selected_paths": ["pkg/service.py"]},
        )
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="pkg/service.py",
            message="Edited service.",
        )
        agent_activity.end_run(conn, run_id=run["run_id"], status="completed")
        suggestion_event = agent_activity.record_run_suggestions(
            conn, run_id=run["run_id"]
        )
        transcript = agent_activity.run_transcript(conn, run["run_id"])
    finally:
        db_mod.close(conn)

    assert suggestion_event is not None
    assert suggestion_event["event_type"] == "suggestion"
    assert transcript is not None
    suggestions = transcript["suggestions"]
    assert suggestions["diagnostics"][0]["message"] == "synthetic warning"
    assert suggestions["affected_tests"]
    assert "tests/test_service.py::test_value" in suggestions["runner"]["node_ids"]
    assert {item["kind"] for item in suggestions["suggestions"]} == {
        "diagnostics",
        "affected_tests",
    }
