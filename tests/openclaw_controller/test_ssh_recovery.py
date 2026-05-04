"""Tests for the CMA SSH recovery allowlist and authorization policy (M2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from code_index.openclaw_controller.scheduler import FleetController
from code_index.openclaw_controller.ssh_recovery import (
    ALLOWED_SSH_COMMAND_KINDS,
    SshRecoveryPolicy,
    SshRecoveryResult,
)
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_messaging.store import MessagingStore


SIGNING_SECRET = "test-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------
# Fixtures / helpers
# ------------------------------------------------------------------


class FakeClaimsStore:
    def __init__(self, hosts_with_claims: set[str] | None = None) -> None:
        self._claims: set[str] = set(hosts_with_claims or ())

    def has_active_file_claims(self, host_id: str) -> bool:
        return host_id in self._claims

    def add_claim(self, host_id: str) -> None:
        self._claims.add(host_id)

    def remove_claim(self, host_id: str) -> None:
        self._claims.discard(host_id)


def _make_controller(
    tmp_path: Any,
    *,
    lease_store: InMemoryFleetLeaseStore | None = None,
) -> tuple[FleetController, InMemoryFleetLeaseStore]:
    store = MessagingStore(tmp_path / "msg.db", signing_secret=SIGNING_SECRET)
    leases = lease_store or InMemoryFleetLeaseStore()
    controller = FleetController(
        messaging_store=store,
        lease_store=leases,
        restart_cooldown_seconds=90,
    )
    return controller, leases


def _register_stale_host(
    controller: FleetController,
    host_id: str,
    *,
    now: datetime = NOW,
    heartbeat_interval: int = 10,
) -> None:
    """Register a host that will be stale at `now` (last heartbeat far in past)."""
    stale_time = now - timedelta(seconds=heartbeat_interval * 3 + 1)
    controller.record_host_heartbeat(
        {
            "host_id": host_id,
            "heartbeat_interval_seconds": heartbeat_interval,
            "capabilities": {
                "repo_roots": [{"path": r"E:\Repos\repo-a", "exists": True}],
                "providers": [{"id": "codex", "display_name": "Codex", "capabilities": ["task_run"]}],
            },
        },
        now=stale_time,
    )


def _register_healthy_host(
    controller: FleetController,
    host_id: str,
    *,
    now: datetime = NOW,
) -> None:
    controller.record_host_heartbeat(
        {
            "host_id": host_id,
            "heartbeat_interval_seconds": 10,
            "capabilities": {
                "repo_roots": [{"path": r"E:\Repos\repo-a", "exists": True}],
                "providers": [{"id": "codex", "display_name": "Codex", "capabilities": ["task_run"]}],
            },
        },
        now=now,
    )


def _policy(
    controller: FleetController,
    leases: InMemoryFleetLeaseStore,
    claims_store: FakeClaimsStore | None = None,
) -> SshRecoveryPolicy:
    return SshRecoveryPolicy(
        fleet_controller=controller,
        lease_store=leases,
        claims_store=claims_store,
    )


# ------------------------------------------------------------------
# Allowed command kinds
# ------------------------------------------------------------------


def test_allowed_command_kinds_are_exactly_four() -> None:
    assert ALLOWED_SSH_COMMAND_KINDS == {
        "health-check",
        "process-check",
        "service-restart",
        "index-update",
    }


def test_policy_allowed_command_kinds_matches_module_constant(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    p = _policy(controller, leases)
    assert p.ALLOWED_COMMAND_KINDS == ALLOWED_SSH_COMMAND_KINDS


# ------------------------------------------------------------------
# Rejection: unknown command kind
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_kind",
    [
        "shell",
        "reboot",
        "assign",
        "cancel",
        "update",
        "rm -rf /",
        "",
        "  ",
        "HEALTH-CHECK",  # case-sensitive
    ],
)
def test_reject_unknown_command_kind(tmp_path: Any, bad_kind: str) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    p = _policy(controller, leases)
    result = p.authorize_recovery(bad_kind, "host-01", now=NOW)
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.reason in ("unknown_command_kind", "missing_command_kind")


# ------------------------------------------------------------------
# Rejection: host not stale
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "command_kind",
    list(ALLOWED_SSH_COMMAND_KINDS),
)
def test_reject_healthy_host(tmp_path: Any, command_kind: str) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_healthy_host(controller, "host-01")
    p = _policy(controller, leases)
    result = p.authorize_recovery(command_kind, "host-01", now=NOW)
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.reason == "host_not_stale"


def test_reject_host_not_found(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    p = _policy(controller, leases)
    result = p.authorize_recovery("health-check", "ghost-host", now=NOW)
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.reason == "host_not_found"


# ------------------------------------------------------------------
# Rejection: active leases
# ------------------------------------------------------------------


def test_reject_host_with_active_repo_lease(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    leases.acquire_lease(
        "repo",
        r"E:\Repos\repo-a",
        owner_host_id="host-01",
        ttl_seconds=1800,
        now=NOW,
    )
    p = _policy(controller, leases)
    result = p.authorize_recovery("health-check", "host-01", now=NOW)
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.reason == "active_leases"
    assert result.rejection.details is not None
    assert result.rejection.details["active_lease_count"] >= 1


def test_reject_host_with_active_task_lease(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    leases.acquire_lease(
        "task",
        "task-abc",
        owner_host_id="host-01",
        ttl_seconds=1800,
        now=NOW,
    )
    p = _policy(controller, leases)
    result = p.authorize_recovery("service-restart", "host-01", now=NOW)
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.reason == "active_leases"


def test_other_host_lease_does_not_block_recovery(tmp_path: Any) -> None:
    """Leases held by a different host must not block recovery of target host."""
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    leases.acquire_lease(
        "repo",
        r"E:\Repos\repo-b",
        owner_host_id="host-02",
        ttl_seconds=1800,
        now=NOW,
    )
    p = _policy(controller, leases)
    result = p.authorize_recovery("health-check", "host-01", now=NOW)
    assert result.status == "authorized"


# ------------------------------------------------------------------
# Rejection: active file claims
# ------------------------------------------------------------------


def test_reject_host_with_active_file_claims(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    claims = FakeClaimsStore({"host-01"})
    p = _policy(controller, leases, claims_store=claims)
    result = p.authorize_recovery("index-update", "host-01", now=NOW)
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.reason == "active_file_claims"


def test_claims_store_absent_does_not_block_recovery(tmp_path: Any) -> None:
    """Without a claims_store, the claim check is skipped."""
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    p = _policy(controller, leases, claims_store=None)
    result = p.authorize_recovery("process-check", "host-01", now=NOW)
    assert result.status == "authorized"


# ------------------------------------------------------------------
# Authorization: happy path
# ------------------------------------------------------------------


@pytest.mark.parametrize("command_kind", sorted(ALLOWED_SSH_COMMAND_KINDS))
def test_authorize_all_allowed_command_kinds(tmp_path: Any, command_kind: str) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    p = _policy(controller, leases)
    result = p.authorize_recovery(command_kind, "host-01", now=NOW)
    assert result.status == "authorized", (
        f"Expected authorized for {command_kind!r}, got {result.rejection}"
    )
    assert result.rejection is None
    assert result.attempt.authorized_at is not None


# ------------------------------------------------------------------
# Audit log
# ------------------------------------------------------------------


def test_audit_log_records_all_attempts(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    _register_healthy_host(controller, "host-02")
    p = _policy(controller, leases)

    p.authorize_recovery("health-check", "host-01", now=NOW)   # authorized
    p.authorize_recovery("reboot", "host-01", now=NOW)         # rejected: unknown kind
    p.authorize_recovery("health-check", "host-02", now=NOW)   # rejected: not stale

    log = p.list_audit_log()
    assert len(log) == 3
    statuses = [entry["status"] for entry in log]
    assert statuses.count("authorized") == 1
    assert statuses.count("rejected") == 2


def test_audit_log_entry_fields(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    p = _policy(controller, leases)
    p.authorize_recovery("service-restart", "host-01", now=NOW)
    log = p.list_audit_log()
    entry = log[0]
    assert entry["command_kind"] == "service-restart"
    assert entry["target_host_id"] == "host-01"
    assert entry["status"] == "authorized"
    assert entry["attempt_id"].startswith("sshrec_")
    assert entry["recorded_at"] is not None


def test_audit_log_rejected_entry_has_rejection_info(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_healthy_host(controller, "host-01")
    p = _policy(controller, leases)
    p.authorize_recovery("health-check", "host-01", now=NOW)
    log = p.list_audit_log()
    entry = log[0]
    assert entry["status"] == "rejected"
    assert entry["rejection_reason"] is not None
    assert entry["rejection_message"] is not None
    assert entry["authorized_at"] is None


# ------------------------------------------------------------------
# Result serialisation
# ------------------------------------------------------------------


def test_result_to_dict_authorized(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_stale_host(controller, "host-01")
    p = _policy(controller, leases)
    result = p.authorize_recovery("index-update", "host-01", now=NOW)
    d = result.to_dict()
    assert d["status"] == "authorized"
    assert d["command_kind"] == "index-update"
    assert d["target_host_id"] == "host-01"
    assert d["rejection"] is None
    assert isinstance(d["attempt"], dict)


def test_result_to_dict_rejected(tmp_path: Any) -> None:
    controller, leases = _make_controller(tmp_path)
    p = _policy(controller, leases)
    result = p.authorize_recovery("health-check", "ghost-host", now=NOW)
    d = result.to_dict()
    assert d["status"] == "rejected"
    assert d["rejection"] is not None
    assert d["rejection"]["reason"] == "host_not_found"
