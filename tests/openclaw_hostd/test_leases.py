from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.openclaw_hostd.leases import DEFAULT_NO_PROGRESS_THRESHOLD
from code_index.openclaw_hostd.leases import FleetLeaseController
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_hostd.leases import LeaseConflictError
from code_index.openclaw_hostd.leases import LeaseFencingError
from code_index.openclaw_hostd.leases import LeaseOwnerError
from code_index.openclaw_hostd.leases import SQLiteFleetLeaseStore
from code_index.openclaw_hostd.leases import release_task_lease_on_terminal_status
from code_index.openclaw_hostd.leases import revoke_no_progress_task_leases


def test_two_hosts_cannot_acquire_same_exclusive_task_lease() -> None:
    store = InMemoryFleetLeaseStore()

    first = store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-a",
    )

    assert first.scope == "task"
    assert first.resource_id == "task-123"
    assert first.owner_host_id == "host-a"
    assert first.fencing_revision == 1
    with pytest.raises(LeaseConflictError, match="task-123"):
        store.acquire_lease(
            "task",
            "task-123",
            owner_host_id="host-b",
            owner_run_id="run-b",
        )


def test_sqlite_fleet_lease_store_enforces_conflicts_across_instances(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fleet-leases.db"
    host_a_store = SQLiteFleetLeaseStore(db_path)
    host_b_store = SQLiteFleetLeaseStore(db_path)
    try:
        first = host_a_store.acquire_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-a",
        )

        with pytest.raises(LeaseConflictError, match="host-a"):
            host_b_store.acquire_lease(
                "task",
                "task-123",
                owner_host_id="host-b",
                owner_run_id="run-b",
            )

        assert first.fencing_revision == 1
        assert host_b_store.get_active_lease("task", "task-123") == first
    finally:
        host_a_store.close()
        host_b_store.close()


def test_renewal_requires_owner_and_stale_fencing_cannot_release() -> None:
    store = InMemoryFleetLeaseStore()
    lease = store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-a",
    )

    with pytest.raises(LeaseOwnerError, match="host-a"):
        store.renew_lease(
            "task",
            "task-123",
            owner_host_id="host-b",
            fencing_revision=lease.fencing_revision,
        )

    renewed = store.renew_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        fencing_revision=lease.fencing_revision,
    )

    assert renewed.fencing_revision > lease.fencing_revision
    with pytest.raises(LeaseFencingError, match="stale"):
        store.release_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            fencing_revision=lease.fencing_revision,
        )

    released = store.release_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        fencing_revision=renewed.fencing_revision,
    )

    assert released.status == "released"


def test_stale_lower_fencing_revision_cannot_overwrite_newer_lease() -> None:
    store = InMemoryFleetLeaseStore()
    first = store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-a",
    )
    released = store.release_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        fencing_revision=first.fencing_revision,
    )
    newer = store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-b",
        owner_run_id="run-b",
    )

    with pytest.raises(LeaseFencingError, match="stale"):
        store.overwrite_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-stale",
            fencing_revision=first.fencing_revision,
        )

    assert newer.fencing_revision > released.fencing_revision
    assert store.get_active_lease("task", "task-123") == newer


def test_terminal_local_status_releases_task_lease() -> None:
    store = InMemoryFleetLeaseStore()
    lease = store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-a",
    )

    released = release_task_lease_on_terminal_status(
        store,
        task_id="task-123",
        owner_host_id="host-a",
        fencing_revision=lease.fencing_revision,
        terminal_status="completed",
        run_id="run-a",
    )

    task = store.get_task_record("task-123")
    assert released is not None
    assert released.status == "released"
    assert store.get_active_lease("task", "task-123") is None
    assert task is not None
    assert task.status == "completed"
    assert task.terminal_status == "completed"
    assert task.reassignable is False


def test_fleet_controller_revokes_stale_no_progress_task_and_marks_reassignable() -> None:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    frozen_last_action_at = now - DEFAULT_NO_PROGRESS_THRESHOLD - timedelta(seconds=1)
    store = InMemoryFleetLeaseStore()
    store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-a",
        now=now - timedelta(minutes=12),
    )
    store.record_task_status(
        "task-123",
        status="running",
        host_id="host-a",
        run_id="run-a",
        now=now - timedelta(minutes=12),
    )

    revoked = revoke_no_progress_task_leases(
        store,
        [
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-a",
                "last_action_at": frozen_last_action_at.isoformat(),
            }
        ],
        now=now,
    )

    task = store.get_task_record("task-123")
    assert [item.task_id for item in revoked] == ["task-123"]
    assert store.get_active_lease("task", "task-123", now=now) is None
    assert task is not None
    assert task.status == "reassignable"
    assert task.reassignable is True
    assert task.terminal_status is None


def test_fleet_controller_service_reads_shared_agent_state_and_revokes_stale_task(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "fleet-leases.db"
    host_store = SQLiteFleetLeaseStore(db_path)
    controller_store = SQLiteFleetLeaseStore(db_path)
    try:
        host_store.acquire_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-a",
            now=now - timedelta(minutes=12),
        )
        host_store.record_task_status(
            "task-123",
            status="running",
            host_id="host-a",
            run_id="run-a",
            now=now - timedelta(minutes=12),
        )
        host_store.put_agent_state(
            "host-a.run-a",
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-a",
                "last_action_at": (
                    now - DEFAULT_NO_PROGRESS_THRESHOLD - timedelta(seconds=1)
                ).isoformat(),
            },
        )

        result = FleetLeaseController(controller_store).run_no_progress_check(now=now)

        task = controller_store.get_task_record("task-123")
        assert [item.task_id for item in result.revoked] == ["task-123"]
        assert controller_store.get_active_lease("task", "task-123", now=now) is None
        assert task is not None
        assert task.status == "reassignable"
        assert task.reassignable is True
    finally:
        host_store.close()
        controller_store.close()


def test_completed_task_before_no_progress_threshold_is_never_reassignable() -> None:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    frozen_last_action_at = now - DEFAULT_NO_PROGRESS_THRESHOLD - timedelta(seconds=1)
    store = InMemoryFleetLeaseStore()
    lease = store.acquire_lease(
        "task",
        "task-123",
        owner_host_id="host-a",
        owner_run_id="run-a",
        now=now - timedelta(minutes=2),
    )
    release_task_lease_on_terminal_status(
        store,
        task_id="task-123",
        owner_host_id="host-a",
        fencing_revision=lease.fencing_revision,
        terminal_status="completed",
        run_id="run-a",
        now=now - timedelta(minutes=1),
    )

    revoked = revoke_no_progress_task_leases(
        store,
        [
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-a",
                "last_action_at": frozen_last_action_at.isoformat(),
            }
        ],
        now=now,
    )

    task = store.get_task_record("task-123")
    assert revoked == []
    assert task is not None
    assert task.status == "completed"
    assert task.reassignable is False
    assert task.terminal_status == "completed"


def test_fleet_controller_service_never_reassigns_completed_task(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    store = SQLiteFleetLeaseStore(tmp_path / "fleet-leases.db")
    try:
        lease = store.acquire_lease(
            "task",
            "task-123",
            owner_host_id="host-a",
            owner_run_id="run-a",
            now=now - timedelta(minutes=2),
        )
        release_task_lease_on_terminal_status(
            store,
            task_id="task-123",
            owner_host_id="host-a",
            fencing_revision=lease.fencing_revision,
            terminal_status="completed",
            run_id="run-a",
            now=now - timedelta(minutes=1),
        )
        store.put_agent_state(
            "host-a.run-a",
            {
                "host_id": "host-a",
                "task_id": "task-123",
                "run_id": "run-a",
                "last_action_at": (
                    now - DEFAULT_NO_PROGRESS_THRESHOLD - timedelta(seconds=1)
                ).isoformat(),
            },
        )

        result = FleetLeaseController(store).run_no_progress_check(now=now)

        task = store.get_task_record("task-123")
        assert result.revoked == []
        assert task is not None
        assert task.status == "completed"
        assert task.reassignable is False
    finally:
        store.close()


def test_local_file_claims_continue_to_work_without_nats(tmp_path: Path) -> None:
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    try:
        run = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="edit locally",
        )
        claim = agent_activity.claim_file(
            conn,
            run_id=run["run_id"],
            file_path="pkg/local.py",
            mode="edit",
        )

        active = agent_activity.active_file_claims(conn, file_path="pkg/local.py")

        assert claim["fence_token"] == 1
        assert active[0]["file_path"] == "pkg/local.py"
        assert active[0]["run_id"] == run["run_id"]
    finally:
        db_mod.close(conn)
