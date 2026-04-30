from __future__ import annotations

from pathlib import Path

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_lifecycle
from code_index import run_orchestrator


def _activity_db(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_register_process_creates_started_row(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            provider="codex",
            pid=1234,
            command_label="codex run",
            metadata={"adapter": "command"},
        )

        assert process["run_id"] == run["run_id"]
        assert process["transport"] == "local-command"
        assert process["provider"] == "codex"
        assert process["pid"] == 1234
        assert process["command_label"] == "codex run"
        assert process["status"] == "started"
        assert process["started_at"]
        assert process["heartbeat_at"]
        assert process["ended_at"] is None
        assert process["metadata"] == {"adapter": "command"}
    finally:
        db_mod.close(conn)


def test_finish_process_records_terminal_status_and_exit_code(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            pid=1234,
        )

        finished = run_lifecycle.finish_process(
            conn,
            process_id=process["process_id"],
            status="failed",
            exit_code=7,
        )

        assert finished["status"] == "failed"
        assert finished["exit_code"] == 7
        assert finished["ended_at"]
    finally:
        db_mod.close(conn)


def test_process_liveness_by_run_reports_alive_then_dead(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
        )

        assert run_lifecycle.process_liveness_by_run(conn) == {
            run["run_id"]: "alive"
        }

        run_lifecycle.finish_process(
            conn,
            process_id=process["process_id"],
            status="completed",
            exit_code=0,
        )

        assert run_lifecycle.process_liveness_by_run(conn) == {
            run["run_id"]: "dead"
        }
    finally:
        db_mod.close(conn)


def test_process_liveness_by_run_ignores_unknown_status(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
        )
        conn.execute(
            "UPDATE agent_run_processes SET status = ? WHERE process_id = ?",
            ("paused", process["process_id"]),
        )

        assert run_lifecycle.process_liveness_by_run(conn) == {}
    finally:
        db_mod.close(conn)


def test_process_liveness_uses_most_recent_finished_process(tmp_path: Path):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            pid=1001,
        )
        process_b = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            pid=1002,
        )

        run_lifecycle.finish_process(
            conn,
            process_id=process_b["process_id"],
            status="failed",
            exit_code=1,
        )

        assert run_lifecycle.process_liveness_by_run(conn)[run["run_id"]] == "dead"
    finally:
        db_mod.close(conn)


def test_run_orchestrator_apply_uses_process_liveness_for_dead_claimless_run(
    tmp_path: Path,
):
    _config, conn = _activity_db(tmp_path)
    try:
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="work")
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
        )
        run_lifecycle.finish_process(
            conn,
            process_id=process["process_id"],
            status="failed",
            exit_code=1,
        )

        result = run_orchestrator.apply(conn)

        updated = agent_activity.get_run(conn, run["run_id"])
        assert updated is not None
        assert updated["status"] == "review"
        assert result["actions"] == [
            {
                "action": "move_to_review",
                "run_id": run["run_id"],
                "reason": "known_dead_without_active_claims",
            }
        ]
    finally:
        db_mod.close(conn)
