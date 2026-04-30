from __future__ import annotations

from pathlib import Path

from code_index import agent_activity
from code_index import agent_swarm
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import run_lifecycle
from code_index import run_orchestrator


def _config(tmp_path: Path) -> cfg_mod.Config:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    return config


def test_default_implementer_claims_edit():
    config = agent_swarm.normalize_swarm_config(
        {"execution_strategy": "agent_swarm"},
        request_provider="kimi",
    )
    roles = {role["role"]: role for role in config["roles"]}

    assert roles["coordinator"]["claim_mode"] == "read"
    assert roles["implementer"]["claim_mode"] == "edit"
    assert roles["reviewer"]["claim_mode"] == "read"


def test_is_swarm_parent_requires_lead_metadata():
    parent = {
        "metadata": {
            "execution_strategy": "swarm",
            "swarm": {"role": "lead", "completion_policy": "all_children_terminal"},
        }
    }
    child = {
        "metadata": {
            "execution_strategy": "swarm",
            "parent_run_id": "parent-1",
            "swarm": {"role": "implementer"},
        }
    }
    legacy_without_lead = {"metadata": {"execution_strategy": "swarm"}}

    assert agent_swarm.is_swarm_parent(parent) is True
    assert agent_swarm.is_swarm_parent(child) is False
    assert agent_swarm.is_swarm_parent(legacy_without_lead) is False


def test_child_runs_returns_children_by_parent_metadata(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {"role": "lead"},
            },
        )
        child = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child",
            metadata={
                "execution_strategy": "swarm",
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
        )

        children = agent_activity.child_runs(conn, parent_run_id=parent["run_id"])

        assert [item["run_id"] for item in children] == [child["run_id"]]
        assert children[0]["metadata"]["parent_run_id"] == parent["run_id"]
    finally:
        db_mod.close(conn)


def test_reconcile_parent_moves_to_review_when_children_complete(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {
                    "role": "lead",
                    "completion_policy": "all_children_terminal",
                },
            },
            status="working",
        )
        child_1 = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child 1",
            metadata={
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
            status="completed",
        )
        child_2 = agent_activity.start_run(
            conn,
            agent_name="Kimi Reviewer",
            prompt="child 2",
            metadata={
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "reviewer"},
            },
            status="completed",
        )

        result = agent_swarm.reconcile_swarm_parent(
            conn,
            parent_run_id=parent["run_id"],
        )

        assert result["status"] == "review"
        assert result["changed"] is True
        assert {item["run_id"] for item in result["children"]} == {
            child_1["run_id"],
            child_2["run_id"],
        }
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "review"
    finally:
        db_mod.close(conn)


def test_reconcile_parent_ignores_non_lead_swarm_run(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        not_parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="not a lead",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {"role": "implementer"},
            },
            status="working",
        )
        agent_activity.start_run(
            conn,
            agent_name="Kimi Reviewer",
            prompt="child",
            metadata={
                "parent_run_id": not_parent["run_id"],
                "swarm": {"role": "reviewer"},
            },
            status="completed",
        )

        result = agent_swarm.reconcile_swarm_parent(
            conn,
            parent_run_id=not_parent["run_id"],
        )

        assert result["changed"] is False
        assert result["reason"] == "not_swarm_parent"
        assert agent_activity.get_run(conn, not_parent["run_id"])["status"] == (
            "working"
        )
    finally:
        db_mod.close(conn)


def test_reconcile_parent_waits_for_expected_child_count(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {
                    "role": "lead",
                    "completion_policy": "all_children_terminal",
                    "child_count": 2,
                },
            },
            status="working",
        )
        agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="only visible child",
            metadata={
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
            status="completed",
        )

        result = agent_swarm.reconcile_swarm_parent(
            conn,
            parent_run_id=parent["run_id"],
        )

        assert result["changed"] is False
        assert result["reason"] == "incomplete_swarm"
        assert result["expected_child_count"] == 2
        assert result["visible_child_count"] == 1
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "working"
    finally:
        db_mod.close(conn)


def test_reconcile_parent_treats_archived_child_as_incomplete(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {
                    "role": "lead",
                    "completion_policy": "all_children_terminal",
                    "child_count": 2,
                },
            },
            status="working",
        )
        archived = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="archived child",
            metadata={
                "execution_strategy": "swarm",
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
            status="completed",
        )
        visible = agent_activity.start_run(
            conn,
            agent_name="Kimi Reviewer",
            prompt="visible child",
            metadata={
                "execution_strategy": "swarm",
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "reviewer"},
            },
            status="completed",
        )
        agent_activity.archive_run(conn, run_id=archived["run_id"])

        result = agent_swarm.reconcile_swarm_parent(
            conn,
            parent_run_id=parent["run_id"],
        )

        assert result["changed"] is False
        assert result["reason"] == "incomplete_swarm"
        assert result["expected_child_count"] == 2
        assert result["visible_child_count"] == 1
        assert [child["run_id"] for child in result["children"]] == [
            visible["run_id"]
        ]
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "working"
    finally:
        db_mod.close(conn)


def test_reconcile_parent_marks_failed_child_attention(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata=agent_swarm.swarm_parent_metadata(
                {"source": "test"},
                {
                    "enabled": True,
                    "execution_strategy": "swarm",
                    "provider": "kimi",
                    "size": 2,
                    "roles": [],
                },
            ),
            status="working",
        )
        failed = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child failed",
            metadata={
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
            status="failed",
        )
        agent_activity.start_run(
            conn,
            agent_name="Kimi Reviewer",
            prompt="child review",
            metadata={
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "reviewer"},
            },
            status="completed",
        )

        result = agent_swarm.reconcile_swarm_parent(
            conn,
            parent_run_id=parent["run_id"],
        )

        updated = agent_activity.get_run(conn, parent["run_id"])
        events = agent_activity.recent_events(conn, limit=5)

        assert result["status"] == "review"
        assert result["requires_attention"] is True
        assert updated["metadata"]["swarm"]["requires_attention"] is True
        assert updated["metadata"]["swarm"]["failed_child_count"] == 1
        assert updated["metadata"]["swarm"]["failed_children"][0]["run_id"] == failed[
            "run_id"
        ]
        status_event = next(
            event for event in events if event["run_id"] == parent["run_id"]
        )
        assert status_event["payload"]["swarm"]["requires_attention"] is True
        assert status_event["payload"]["swarm"]["failed_children"][0]["run_id"] == (
            failed["run_id"]
        )
    finally:
        db_mod.close(conn)


def test_run_orchestrator_reconciles_swarm_parent(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {"role": "lead"},
            },
            status="working",
        )
        agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child",
            metadata={
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
            status="completed",
        )

        result = run_orchestrator.apply(conn)

        assert result["swarm_reconciliations"][0]["status"] == "review"
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "review"
    finally:
        db_mod.close(conn)


def test_run_orchestrator_reconciles_after_moving_dead_child_to_review(
    tmp_path: Path,
):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="parent",
            metadata={
                "execution_strategy": "swarm",
                "swarm": {"role": "lead", "child_count": 1},
            },
            status="working",
        )
        child = agent_activity.start_run(
            conn,
            agent_name="Kimi Implementer",
            prompt="child",
            metadata={
                "execution_strategy": "swarm",
                "parent_run_id": parent["run_id"],
                "swarm": {"role": "implementer"},
            },
            status="working",
        )
        process = run_lifecycle.register_process(
            conn,
            run_id=child["run_id"],
            transport="local-command",
        )
        run_lifecycle.finish_process(
            conn,
            process_id=process["process_id"],
            status="failed",
            exit_code=1,
        )

        result = run_orchestrator.apply(conn)

        assert result["actions"] == [
            {
                "action": "move_to_review",
                "run_id": child["run_id"],
                "reason": "known_dead_without_active_claims",
            }
        ]
        assert result["swarm_reconciliations"][0]["status"] == "review"
        assert agent_activity.get_run(conn, child["run_id"])["status"] == "review"
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "review"
    finally:
        db_mod.close(conn)


def test_run_orchestrator_materializes_unblocked_queued_swarm_parent(
    tmp_path: Path,
):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        blocker = agent_activity.start_run(
            conn,
            agent_name="Kimi Blocker",
            prompt="blocker",
            status="working",
        )
        swarm = agent_swarm.normalize_swarm_config(
            {
                "execution_strategy": "agent_swarm",
                "swarm": {
                    "enabled": True,
                    "provider": "kimi",
                    "size": 3,
                },
            },
            request_provider="kimi",
        )
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="blocked swarm parent",
            metadata=agent_swarm.swarm_parent_metadata(
                {
                    "source": "test",
                    "provider": "kimi",
                    "selected_paths": ["pkg/__init__.py"],
                    "node": {
                        "kind": "file",
                        "path": "pkg/__init__.py",
                    },
                },
                swarm,
            ),
            status="working",
        )
        agent_activity.add_run_blockers(
            conn,
            run_id=parent["run_id"],
            blocked_by_run_ids=[blocker["run_id"]],
            reason="wait for blocker",
        )
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "blocked"
        assert agent_activity.child_runs(conn, parent_run_id=parent["run_id"]) == []

        agent_activity.end_run(conn, run_id=blocker["run_id"], status="completed")
        assert agent_activity.get_run(conn, parent["run_id"])["status"] == "queued"

        result = run_orchestrator.apply(conn)

        actions = [
            action
            for action in result["actions"]
            if action["action"] == "materialize_swarm_children"
        ]
        assert actions == [
            {
                "action": "materialize_swarm_children",
                "run_id": parent["run_id"],
                "reason": "blocked_swarm_resumed",
                "status": "working",
                "child_count": 3,
                "expected_child_count": 3,
            }
        ]
        updated_parent = agent_activity.get_run(conn, parent["run_id"])
        children = agent_activity.child_runs(conn, parent_run_id=parent["run_id"])
        claims = agent_activity.active_file_claims(
            conn,
            file_path="pkg/__init__.py",
            limit=20,
        )

        assert updated_parent["status"] == "working"
        assert updated_parent["metadata"]["swarm"]["children_materialized"] is True
        assert len(children) == 3
        assert all(child["metadata"]["execution_strategy"] == "swarm" for child in children)
        assert all(child["metadata"]["parent_run_id"] == parent["run_id"] for child in children)
        assert {
            child["metadata"]["swarm"]["role"]
            for child in children
        } == {"coordinator", "implementer", "reviewer"}
        assert [claim["mode"] for claim in claims].count("edit") == 1
    finally:
        db_mod.close(conn)


def test_run_orchestrator_does_not_rematerialize_archived_swarm_children(
    tmp_path: Path,
):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        swarm = agent_swarm.normalize_swarm_config(
            {
                "execution_strategy": "agent_swarm",
                "swarm": {
                    "enabled": True,
                    "provider": "kimi",
                    "size": 2,
                },
            },
            request_provider="kimi",
        )
        parent = agent_activity.start_run(
            conn,
            agent_name="Kimi Swarm Lead",
            prompt="blocked swarm parent",
            metadata=agent_swarm.swarm_parent_metadata(
                {
                    "source": "test",
                    "provider": "kimi",
                    "selected_paths": ["pkg/__init__.py"],
                    "node": {
                        "kind": "file",
                        "path": "pkg/__init__.py",
                    },
                },
                swarm,
            ),
            status="queued",
        )

        first = run_orchestrator.apply(conn)
        first_children = agent_activity.child_runs(conn, parent_run_id=parent["run_id"])
        for child in first_children:
            agent_activity.archive_run(conn, run_id=child["run_id"])
        conn.execute(
            "UPDATE agent_runs SET status = 'queued' WHERE run_id = ?",
            (parent["run_id"],),
        )

        second = run_orchestrator.apply(conn)
        archived_child_ids = {
            run["run_id"]
            for run in agent_activity.recent_runs(
                conn,
                limit=10,
                include_archived=True,
            )
            if (run.get("metadata") or {}).get("parent_run_id") == parent["run_id"]
        }

        assert first["actions"][0]["action"] == "materialize_swarm_children"
        assert len(first_children) == 2
        assert second["actions"] == []
        assert agent_activity.child_runs(conn, parent_run_id=parent["run_id"]) == []
        assert archived_child_ids == {child["run_id"] for child in first_children}
    finally:
        db_mod.close(conn)
