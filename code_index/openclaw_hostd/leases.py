"""Fleet-level OpenClaw leases and no-progress helpers."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from typing import Iterator
from typing import Protocol


LEASE_SCOPES = frozenset({"host", "repo", "task"})
DEFAULT_NO_PROGRESS_THRESHOLD = timedelta(minutes=10)
TERMINAL_TASK_STATUSES = frozenset(
    {
        "completed",
        "done",
        "failed",
        "cancelled",
        "canceled",
        "review",
        "needs_review",
        "needs-review",
    }
)


class LeaseError(RuntimeError):
    """Base class for fleet lease failures."""


class LeaseConflictError(LeaseError):
    """Raised when an active conflicting lease already exists."""


class LeaseFencingError(LeaseError):
    """Raised when a lease mutation uses a stale fencing revision."""


class LeaseOwnerError(LeaseError):
    """Raised when a lease mutation is attempted by the wrong owner."""


class FleetLeaseStore(Protocol):
    def acquire_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> "FleetLease":
        ...

    def renew_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> "FleetLease":
        ...

    def release_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        now: datetime | None = None,
    ) -> "FleetLease":
        ...

    def bind_lease_owner_run(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        current_owner_run_id: str | None = None,
        new_owner_run_id: str,
        fencing_revision: int,
        ttl_seconds: int | float | None = None,
        now: datetime | None = None,
    ) -> "FleetLease":
        ...

    def revoke_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        fencing_revision: int,
        now: datetime | None = None,
    ) -> "FleetLease":
        ...

    def get_active_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        now: datetime | None = None,
    ) -> "FleetLease | None":
        ...

    def list_active_leases(
        self,
        *,
        scope: str | None = None,
        now: datetime | None = None,
    ) -> list["FleetLease"]:
        ...

    def record_task_status(
        self,
        task_id: str,
        *,
        status: str,
        host_id: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> "FleetTaskRecord":
        ...

    def mark_task_reassignable(
        self,
        task_id: str,
        *,
        host_id: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> "FleetTaskRecord":
        ...

    def get_task_record(self, task_id: str) -> "FleetTaskRecord | None":
        ...

    def list_agent_states(self) -> list[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class FleetLease:
    lease_id: str
    scope: str
    resource_id: str
    owner_host_id: str
    owner_run_id: str | None
    fencing_revision: int
    status: str
    acquired_at: datetime
    updated_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class FleetTaskRecord:
    task_id: str
    status: str
    host_id: str | None
    run_id: str | None
    terminal_status: str | None
    reassignable: bool
    updated_at: datetime


@dataclass(frozen=True)
class NoProgressRevocation:
    task_id: str
    host_id: str
    run_id: str | None
    lease_id: str
    fencing_revision: int
    last_action_at: datetime
    threshold_seconds: int


@dataclass(frozen=True)
class NoProgressCheckResult:
    revoked: list[NoProgressRevocation]
    task_records: list[FleetTaskRecord]


class InMemoryFleetLeaseStore:
    """Thread-safe in-memory fleet lease store for tests and local services."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str], FleetLease] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._tasks: dict[str, FleetTaskRecord] = {}
        self._agent_states: dict[str, Mapping[str, Any]] = {}

    def acquire_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._lock:
            active = self._active_lease_for_key(key, now=timestamp)
            if active is not None:
                if active.owner_host_id == owner_host_id:
                    _raise_if_owner_run_conflicts(
                        key,
                        active,
                        owner_run_id=owner_run_id,
                        error_type=LeaseConflictError,
                    )
                    return active
                raise LeaseConflictError(
                    f"{key[0]} lease conflict for {key[1]} held by "
                    f"{active.owner_host_id}"
                )
            revision = self._next_revision(key)
            lease = FleetLease(
                lease_id=f"{key[0]}-{_lease_id_part(key[1])}-{revision}",
                scope=key[0],
                resource_id=key[1],
                owner_host_id=owner_host_id,
                owner_run_id=_optional_text(owner_run_id),
                fencing_revision=revision,
                status="active",
                acquired_at=timestamp,
                updated_at=timestamp,
                expires_at=_expires_at(timestamp, ttl_seconds),
            )
            self._leases[key] = lease
            if key[0] == "task":
                self.record_task_status(
                    key[1],
                    status="leased",
                    host_id=owner_host_id,
                    run_id=lease.owner_run_id,
                    now=timestamp,
                )
            return lease

    def renew_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._lock:
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            _require_lease_owner(
                key,
                active,
                owner_host_id=owner_host_id,
                owner_run_id=owner_run_id,
            )
            renewed = _replace_lease(
                active,
                fencing_revision=self._next_revision(key),
                updated_at=timestamp,
                expires_at=_expires_at(timestamp, ttl_seconds),
            )
            self._leases[key] = renewed
            return renewed

    def release_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._lock:
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            _require_lease_owner(
                key,
                active,
                owner_host_id=owner_host_id,
                owner_run_id=owner_run_id,
            )
            released = _replace_lease(
                active,
                fencing_revision=self._next_revision(key),
                status="released",
                updated_at=timestamp,
                expires_at=None,
            )
            self._leases[key] = released
            return released

    def bind_lease_owner_run(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        current_owner_run_id: str | None = None,
        new_owner_run_id: str,
        fencing_revision: int,
        ttl_seconds: int | float | None = None,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        new_owner_run_id = _required_text(new_owner_run_id, "new_owner_run_id")
        timestamp = _utc(now)
        with self._lock:
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            _require_lease_owner(
                key,
                active,
                owner_host_id=owner_host_id,
                owner_run_id=current_owner_run_id,
            )
            if active.owner_run_id == new_owner_run_id:
                return active
            rebound = _replace_lease(
                active,
                owner_run_id=new_owner_run_id,
                fencing_revision=self._next_revision(key),
                updated_at=timestamp,
                expires_at=(
                    active.expires_at
                    if ttl_seconds is None
                    else _expires_at(timestamp, ttl_seconds)
                ),
            )
            self._leases[key] = rebound
            return rebound

    def revoke_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        fencing_revision: int,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        timestamp = _utc(now)
        with self._lock:
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            revoked = _replace_lease(
                active,
                fencing_revision=self._next_revision(key),
                status="revoked",
                updated_at=timestamp,
                expires_at=None,
            )
            self._leases[key] = revoked
            return revoked

    def overwrite_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._lock:
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            revision = self._next_revision(key)
            lease = FleetLease(
                lease_id=f"{key[0]}-{_lease_id_part(key[1])}-{revision}",
                scope=key[0],
                resource_id=key[1],
                owner_host_id=owner_host_id,
                owner_run_id=_optional_text(owner_run_id),
                fencing_revision=revision,
                status="active",
                acquired_at=active.acquired_at,
                updated_at=timestamp,
                expires_at=_expires_at(timestamp, ttl_seconds),
            )
            self._leases[key] = lease
            return lease

    def get_active_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        now: datetime | None = None,
    ) -> FleetLease | None:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        with self._lock:
            return self._active_lease_for_key(key, now=_utc(now))

    def list_active_leases(
        self,
        *,
        scope: str | None = None,
        now: datetime | None = None,
    ) -> list[FleetLease]:
        timestamp = _utc(now)
        requested_scope = _lease_scope(scope) if scope is not None else None
        with self._lock:
            active: list[FleetLease] = []
            for key in list(self._leases):
                if requested_scope is not None and key[0] != requested_scope:
                    continue
                lease = self._active_lease_for_key(key, now=timestamp)
                if lease is not None:
                    active.append(lease)
            return sorted(active, key=lambda lease: (lease.scope, lease.resource_id))

    def record_task_status(
        self,
        task_id: str,
        *,
        status: str,
        host_id: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> FleetTaskRecord:
        task_id = _required_text(task_id, "task_id")
        status = _required_text(status, "status").lower()
        timestamp = _utc(now)
        terminal_status = status if status in TERMINAL_TASK_STATUSES else None
        with self._lock:
            previous = self._tasks.get(task_id)
            if previous is not None:
                host_id = _optional_text(host_id) or previous.host_id
                run_id = _optional_text(run_id) or previous.run_id
            else:
                host_id = _optional_text(host_id)
                run_id = _optional_text(run_id)
            record = FleetTaskRecord(
                task_id=task_id,
                status=status,
                host_id=host_id,
                run_id=run_id,
                terminal_status=terminal_status,
                reassignable=False,
                updated_at=timestamp,
            )
            self._tasks[task_id] = record
            return record

    def mark_task_reassignable(
        self,
        task_id: str,
        *,
        host_id: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> FleetTaskRecord:
        task_id = _required_text(task_id, "task_id")
        timestamp = _utc(now)
        with self._lock:
            previous = self._tasks.get(task_id)
            if previous is not None:
                host_id = _optional_text(host_id) or previous.host_id
                run_id = _optional_text(run_id) or previous.run_id
            else:
                host_id = _optional_text(host_id)
                run_id = _optional_text(run_id)
            record = FleetTaskRecord(
                task_id=task_id,
                status="reassignable",
                host_id=host_id,
                run_id=run_id,
                terminal_status=None,
                reassignable=True,
                updated_at=timestamp,
            )
            self._tasks[task_id] = record
            return record

    def get_task_record(self, task_id: str) -> FleetTaskRecord | None:
        task_id = _required_text(task_id, "task_id")
        with self._lock:
            return self._tasks.get(task_id)

    def put_agent_state(
        self,
        key: str,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        del now
        key = _required_text(key, "key")
        with self._lock:
            self._agent_states[key] = dict(payload)

    def list_agent_states(self) -> list[Mapping[str, Any]]:
        with self._lock:
            return [dict(value) for _, value in sorted(self._agent_states.items())]

    def _active_lease_for_key(
        self,
        key: tuple[str, str],
        *,
        now: datetime,
    ) -> FleetLease | None:
        lease = self._leases.get(key)
        if lease is None or lease.status != "active":
            return None
        if lease.expires_at is not None and lease.expires_at <= now:
            self._leases[key] = _replace_lease(
                lease,
                status="expired",
                updated_at=now,
            )
            return None
        return lease

    def _next_revision(self, key: tuple[str, str]) -> int:
        revision = self._revisions.get(key, 0) + 1
        self._revisions[key] = revision
        return revision

    def _require_current_fence(
        self,
        key: tuple[str, str],
        *,
        fencing_revision: int,
        now: datetime,
    ) -> FleetLease:
        active = self._active_lease_for_key(key, now=now)
        if active is None:
            raise LeaseFencingError(f"{key[0]} lease for {key[1]} is not active")
        if active.fencing_revision != int(fencing_revision):
            raise LeaseFencingError(
                f"stale fencing revision for {key[0]} lease {key[1]}: "
                f"expected {active.fencing_revision}, got {fencing_revision}"
            )
        return active


class SQLiteFleetLeaseStore:
    """SQLite-backed fleet lease store shareable across host daemon processes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _configure_sqlite(self.conn)
        self.apply_schema()

    def close(self) -> None:
        self.conn.close()

    def apply_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS openclaw_fleet_leases (
                  scope TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  lease_id TEXT NOT NULL,
                  owner_host_id TEXT NOT NULL,
                  owner_run_id TEXT,
                  fencing_revision INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  acquired_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  expires_at TEXT,
                  PRIMARY KEY(scope, resource_id)
                );

                CREATE TABLE IF NOT EXISTS openclaw_fleet_task_states (
                  task_id TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  host_id TEXT,
                  run_id TEXT,
                  terminal_status TEXT,
                  reassignable INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS openclaw_agent_states (
                  state_key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()

    def acquire_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._transaction():
            active = self._active_lease_for_key(key, now=timestamp)
            if active is not None:
                if active.owner_host_id == owner_host_id:
                    _raise_if_owner_run_conflicts(
                        key,
                        active,
                        owner_run_id=owner_run_id,
                        error_type=LeaseConflictError,
                    )
                    return active
                raise LeaseConflictError(
                    f"{key[0]} lease conflict for {key[1]} held by "
                    f"{active.owner_host_id}"
                )
            revision = self._next_revision(key)
            lease = FleetLease(
                lease_id=f"{key[0]}-{_lease_id_part(key[1])}-{revision}",
                scope=key[0],
                resource_id=key[1],
                owner_host_id=owner_host_id,
                owner_run_id=_optional_text(owner_run_id),
                fencing_revision=revision,
                status="active",
                acquired_at=timestamp,
                updated_at=timestamp,
                expires_at=_expires_at(timestamp, ttl_seconds),
            )
            self._upsert_lease(lease)
            if key[0] == "task":
                self._upsert_task_record(
                    key[1],
                    status="leased",
                    host_id=owner_host_id,
                    run_id=lease.owner_run_id,
                    now=timestamp,
                    reassignable=False,
                )
            return lease

    def renew_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._transaction():
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            _require_lease_owner(
                key,
                active,
                owner_host_id=owner_host_id,
                owner_run_id=owner_run_id,
            )
            renewed = _replace_lease(
                active,
                fencing_revision=self._next_revision(key),
                updated_at=timestamp,
                expires_at=_expires_at(timestamp, ttl_seconds),
            )
            self._upsert_lease(renewed)
            return renewed

    def release_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._transaction():
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            _require_lease_owner(
                key,
                active,
                owner_host_id=owner_host_id,
                owner_run_id=owner_run_id,
            )
            released = _replace_lease(
                active,
                fencing_revision=self._next_revision(key),
                status="released",
                updated_at=timestamp,
                expires_at=None,
            )
            self._upsert_lease(released)
            return released

    def bind_lease_owner_run(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        current_owner_run_id: str | None = None,
        new_owner_run_id: str,
        fencing_revision: int,
        ttl_seconds: int | float | None = None,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        new_owner_run_id = _required_text(new_owner_run_id, "new_owner_run_id")
        timestamp = _utc(now)
        with self._transaction():
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            _require_lease_owner(
                key,
                active,
                owner_host_id=owner_host_id,
                owner_run_id=current_owner_run_id,
            )
            if active.owner_run_id == new_owner_run_id:
                return active
            rebound = _replace_lease(
                active,
                owner_run_id=new_owner_run_id,
                fencing_revision=self._next_revision(key),
                updated_at=timestamp,
                expires_at=(
                    active.expires_at
                    if ttl_seconds is None
                    else _expires_at(timestamp, ttl_seconds)
                ),
            )
            self._upsert_lease(rebound)
            return rebound

    def revoke_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        fencing_revision: int,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        timestamp = _utc(now)
        with self._transaction():
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            revoked = _replace_lease(
                active,
                fencing_revision=self._next_revision(key),
                status="revoked",
                updated_at=timestamp,
                expires_at=None,
            )
            self._upsert_lease(revoked)
            return revoked

    def overwrite_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        owner_run_id: str | None = None,
        fencing_revision: int,
        ttl_seconds: int | float | None = 1800,
        now: datetime | None = None,
    ) -> FleetLease:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        owner_host_id = _required_text(owner_host_id, "owner_host_id")
        timestamp = _utc(now)
        with self._transaction():
            active = self._require_current_fence(
                key,
                fencing_revision=fencing_revision,
                now=timestamp,
            )
            revision = self._next_revision(key)
            lease = FleetLease(
                lease_id=f"{key[0]}-{_lease_id_part(key[1])}-{revision}",
                scope=key[0],
                resource_id=key[1],
                owner_host_id=owner_host_id,
                owner_run_id=_optional_text(owner_run_id),
                fencing_revision=revision,
                status="active",
                acquired_at=active.acquired_at,
                updated_at=timestamp,
                expires_at=_expires_at(timestamp, ttl_seconds),
            )
            self._upsert_lease(lease)
            return lease

    def get_active_lease(
        self,
        scope: str,
        resource_id: str,
        *,
        now: datetime | None = None,
    ) -> FleetLease | None:
        key = (_lease_scope(scope), _required_text(resource_id, "resource_id"))
        with self._transaction():
            return self._active_lease_for_key(key, now=_utc(now))

    def list_active_leases(
        self,
        *,
        scope: str | None = None,
        now: datetime | None = None,
    ) -> list[FleetLease]:
        timestamp = _utc(now)
        requested_scope = _lease_scope(scope) if scope is not None else None
        with self._transaction():
            rows = self.conn.execute(
                """
                SELECT *
                  FROM openclaw_fleet_leases
                 ORDER BY scope, resource_id
                """
            ).fetchall()
            active: list[FleetLease] = []
            for row in rows:
                key = (str(row["scope"]), str(row["resource_id"]))
                if requested_scope is not None and key[0] != requested_scope:
                    continue
                lease = self._active_lease_for_key(key, now=timestamp)
                if lease is not None:
                    active.append(lease)
            return active

    def record_task_status(
        self,
        task_id: str,
        *,
        status: str,
        host_id: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> FleetTaskRecord:
        task_id = _required_text(task_id, "task_id")
        status = _required_text(status, "status").lower()
        timestamp = _utc(now)
        with self._transaction():
            return self._upsert_task_record(
                task_id,
                status=status,
                host_id=host_id,
                run_id=run_id,
                now=timestamp,
                reassignable=False,
            )

    def mark_task_reassignable(
        self,
        task_id: str,
        *,
        host_id: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> FleetTaskRecord:
        task_id = _required_text(task_id, "task_id")
        timestamp = _utc(now)
        with self._transaction():
            return self._upsert_task_record(
                task_id,
                status="reassignable",
                host_id=host_id,
                run_id=run_id,
                now=timestamp,
                reassignable=True,
            )

    def get_task_record(self, task_id: str) -> FleetTaskRecord | None:
        task_id = _required_text(task_id, "task_id")
        with self._lock:
            row = self.conn.execute(
                """
                SELECT *
                  FROM openclaw_fleet_task_states
                 WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def put_agent_state(
        self,
        key: str,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        key = _required_text(key, "key")
        timestamp = _utc(now)
        with self._transaction():
            self.conn.execute(
                """
                INSERT INTO openclaw_agent_states(
                  state_key, payload_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  updated_at = excluded.updated_at
                """,
                (key, _json_dumps(dict(payload)), _datetime_text(timestamp)),
            )

    def list_agent_states(self) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT payload_json
                  FROM openclaw_agent_states
                 ORDER BY state_key
                """
            ).fetchall()
        return [_json_loads(str(row["payload_json"])) for row in rows]

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def _lease_row(self, key: tuple[str, str]) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
              FROM openclaw_fleet_leases
             WHERE scope = ?
               AND resource_id = ?
            """,
            key,
        ).fetchone()

    def _active_lease_for_key(
        self,
        key: tuple[str, str],
        *,
        now: datetime,
    ) -> FleetLease | None:
        row = self._lease_row(key)
        if row is None or str(row["status"] or "") != "active":
            return None
        expires_at = _parse_datetime(row["expires_at"])
        if expires_at is not None and expires_at <= now:
            self.conn.execute(
                """
                UPDATE openclaw_fleet_leases
                   SET status = 'expired',
                       updated_at = ?
                 WHERE scope = ?
                   AND resource_id = ?
                   AND status = 'active'
                """,
                (_datetime_text(now), key[0], key[1]),
            )
            return None
        return _row_to_lease(row)

    def _next_revision(self, key: tuple[str, str]) -> int:
        row = self._lease_row(key)
        if row is None:
            return 1
        return int(row["fencing_revision"] or 0) + 1

    def _require_current_fence(
        self,
        key: tuple[str, str],
        *,
        fencing_revision: int,
        now: datetime,
    ) -> FleetLease:
        active = self._active_lease_for_key(key, now=now)
        if active is None:
            raise LeaseFencingError(f"{key[0]} lease for {key[1]} is not active")
        if active.fencing_revision != int(fencing_revision):
            raise LeaseFencingError(
                f"stale fencing revision for {key[0]} lease {key[1]}: "
                f"expected {active.fencing_revision}, got {fencing_revision}"
            )
        return active

    def _upsert_lease(self, lease: FleetLease) -> None:
        self.conn.execute(
            """
            INSERT INTO openclaw_fleet_leases(
              scope, resource_id, lease_id, owner_host_id, owner_run_id,
              fencing_revision, status, acquired_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, resource_id) DO UPDATE SET
              lease_id = excluded.lease_id,
              owner_host_id = excluded.owner_host_id,
              owner_run_id = excluded.owner_run_id,
              fencing_revision = excluded.fencing_revision,
              status = excluded.status,
              acquired_at = excluded.acquired_at,
              updated_at = excluded.updated_at,
              expires_at = excluded.expires_at
            """,
            (
                lease.scope,
                lease.resource_id,
                lease.lease_id,
                lease.owner_host_id,
                lease.owner_run_id,
                lease.fencing_revision,
                lease.status,
                _datetime_text(lease.acquired_at),
                _datetime_text(lease.updated_at),
                _datetime_text(lease.expires_at),
            ),
        )

    def _upsert_task_record(
        self,
        task_id: str,
        *,
        status: str,
        host_id: str | None,
        run_id: str | None,
        now: datetime,
        reassignable: bool,
    ) -> FleetTaskRecord:
        previous = self.conn.execute(
            """
            SELECT *
              FROM openclaw_fleet_task_states
             WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if previous is not None:
            host_id = _optional_text(host_id) or previous["host_id"]
            run_id = _optional_text(run_id) or previous["run_id"]
        else:
            host_id = _optional_text(host_id)
            run_id = _optional_text(run_id)
        terminal_status = status if status in TERMINAL_TASK_STATUSES else None
        record = FleetTaskRecord(
            task_id=task_id,
            status=status,
            host_id=host_id,
            run_id=run_id,
            terminal_status=terminal_status,
            reassignable=reassignable,
            updated_at=now,
        )
        self.conn.execute(
            """
            INSERT INTO openclaw_fleet_task_states(
              task_id, status, host_id, run_id, terminal_status,
              reassignable, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              status = excluded.status,
              host_id = excluded.host_id,
              run_id = excluded.run_id,
              terminal_status = excluded.terminal_status,
              reassignable = excluded.reassignable,
              updated_at = excluded.updated_at
            """,
            (
                record.task_id,
                record.status,
                record.host_id,
                record.run_id,
                record.terminal_status,
                1 if record.reassignable else 0,
                _datetime_text(record.updated_at),
            ),
        )
        return record


@dataclass(frozen=True)
class FleetLeaseController:
    lease_store: FleetLeaseStore
    no_progress_threshold: timedelta = DEFAULT_NO_PROGRESS_THRESHOLD

    def run_no_progress_check(
        self,
        *,
        now: datetime | None = None,
    ) -> NoProgressCheckResult:
        revoked = revoke_no_progress_task_leases(
            self.lease_store,
            self.lease_store.list_agent_states(),
            now=now,
            threshold=self.no_progress_threshold,
        )
        return NoProgressCheckResult(
            revoked=revoked,
            task_records=[
                record
                for item in revoked
                if (record := self.lease_store.get_task_record(item.task_id))
                is not None
            ],
        )


def _lease_scope(value: str) -> str:
    scope = _required_text(value, "scope").lower()
    if scope not in LEASE_SCOPES:
        raise ValueError("lease scope must be host, repo, or task")
    return scope


def _require_lease_owner(
    key: tuple[str, str],
    lease: FleetLease,
    *,
    owner_host_id: str,
    owner_run_id: str | None = None,
) -> None:
    if lease.owner_host_id != owner_host_id:
        raise LeaseOwnerError(
            f"{key[0]} lease for {key[1]} is owned by "
            f"{lease.owner_host_id}, not {owner_host_id}"
        )
    _raise_if_owner_run_conflicts(
        key,
        lease,
        owner_run_id=owner_run_id,
        error_type=LeaseOwnerError,
    )


def _raise_if_owner_run_conflicts(
    key: tuple[str, str],
    lease: FleetLease,
    *,
    owner_run_id: str | None,
    error_type: type[LeaseError],
) -> None:
    requested_run_id = _optional_text(owner_run_id)
    if requested_run_id is None or lease.owner_run_id is None:
        return
    if lease.owner_run_id == requested_run_id:
        return
    raise error_type(
        f"{key[0]} lease for {key[1]} is owned by run "
        f"{lease.owner_run_id}, not {requested_run_id}"
    )


def release_task_lease_on_terminal_status(
    lease_store: FleetLeaseStore,
    *,
    task_id: str,
    owner_host_id: str,
    fencing_revision: int,
    terminal_status: str,
    run_id: str | None = None,
    owner_run_id: str | None = None,
    now: datetime | None = None,
) -> FleetLease | None:
    status = _required_text(terminal_status, "terminal_status").lower()
    if status not in TERMINAL_TASK_STATUSES:
        return None
    timestamp = _utc(now)
    released = lease_store.release_lease(
        "task",
        task_id,
        owner_host_id=owner_host_id,
        owner_run_id=_optional_text(owner_run_id) or _optional_text(run_id),
        fencing_revision=fencing_revision,
        now=timestamp,
    )
    lease_store.record_task_status(
        task_id,
        status=status,
        host_id=owner_host_id,
        run_id=_optional_text(run_id) or _optional_text(owner_run_id),
        now=timestamp,
    )
    return released


def revoke_no_progress_task_leases(
    lease_store: FleetLeaseStore,
    agent_states: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    threshold: timedelta = DEFAULT_NO_PROGRESS_THRESHOLD,
) -> list[NoProgressRevocation]:
    timestamp = _utc(now)
    threshold_seconds = max(0, int(threshold.total_seconds()))
    states_by_task = _agent_states_by_task(agent_states)
    revoked: list[NoProgressRevocation] = []
    for lease in lease_store.list_active_leases(scope="task", now=timestamp):
        task = lease_store.get_task_record(lease.resource_id)
        if task is not None and task.terminal_status is not None:
            continue
        state = _matching_agent_state(
            states_by_task.get(lease.resource_id, ()),
            lease=lease,
        )
        if state is None or _state_is_terminal(state):
            continue
        last_action_at = _parse_datetime(state.get("last_action_at"))
        if last_action_at is None:
            continue
        if timestamp - last_action_at < threshold:
            continue
        lease_store.revoke_lease(
            "task",
            lease.resource_id,
            fencing_revision=lease.fencing_revision,
            now=timestamp,
        )
        lease_store.mark_task_reassignable(
            lease.resource_id,
            host_id=lease.owner_host_id,
            run_id=lease.owner_run_id,
            now=timestamp,
        )
        revoked.append(
            NoProgressRevocation(
                task_id=lease.resource_id,
                host_id=lease.owner_host_id,
                run_id=lease.owner_run_id,
                lease_id=lease.lease_id,
                fencing_revision=lease.fencing_revision,
                last_action_at=last_action_at,
                threshold_seconds=threshold_seconds,
            )
        )
    return revoked


def _agent_states_by_task(
    agent_states: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for raw_state in agent_states:
        state = _state_payload(raw_state)
        task_id = _optional_text(state.get("task_id"))
        if task_id is None:
            continue
        result.setdefault(task_id, []).append(state)
    return result


def _state_payload(raw_state: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw_state.get("payload")
    if isinstance(payload, Mapping):
        return payload
    return raw_state


def _matching_agent_state(
    states: Iterable[Mapping[str, Any]],
    *,
    lease: FleetLease,
) -> Mapping[str, Any] | None:
    if lease.owner_run_id is None:
        return None
    for state in states:
        if _optional_text(state.get("host_id")) != lease.owner_host_id:
            continue
        if _optional_text(state.get("run_id")) == lease.owner_run_id:
            return state
    return None


def _state_is_terminal(state: Mapping[str, Any]) -> bool:
    for key in ("terminal_status", "status", "run_status"):
        value = _optional_text(state.get(key))
        if value and value.lower() in TERMINAL_TASK_STATUSES:
            return True
    return False


def _parse_datetime(value: Any) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _utc(parsed)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _configure_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def _row_to_lease(row: sqlite3.Row) -> FleetLease:
    return FleetLease(
        lease_id=str(row["lease_id"]),
        scope=str(row["scope"]),
        resource_id=str(row["resource_id"]),
        owner_host_id=str(row["owner_host_id"]),
        owner_run_id=_optional_text(row["owner_run_id"]),
        fencing_revision=int(row["fencing_revision"] or 0),
        status=str(row["status"]),
        acquired_at=_parse_datetime(row["acquired_at"]) or _utc(None),
        updated_at=_parse_datetime(row["updated_at"]) or _utc(None),
        expires_at=_parse_datetime(row["expires_at"]),
    )


def _row_to_task(row: sqlite3.Row) -> FleetTaskRecord:
    return FleetTaskRecord(
        task_id=str(row["task_id"]),
        status=str(row["status"]),
        host_id=_optional_text(row["host_id"]),
        run_id=_optional_text(row["run_id"]),
        terminal_status=_optional_text(row["terminal_status"]),
        reassignable=bool(row["reassignable"]),
        updated_at=_parse_datetime(row["updated_at"]) or _utc(None),
    )


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _utc(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _expires_at(
    now: datetime,
    ttl_seconds: int | float | None,
) -> datetime | None:
    if ttl_seconds is None:
        return None
    seconds = float(ttl_seconds)
    if seconds <= 0:
        return now
    return now + timedelta(seconds=seconds)


def _lease_id_part(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-") or "x"


def _replace_lease(lease: FleetLease, **changes: object) -> FleetLease:
    values = {
        "lease_id": lease.lease_id,
        "scope": lease.scope,
        "resource_id": lease.resource_id,
        "owner_host_id": lease.owner_host_id,
        "owner_run_id": lease.owner_run_id,
        "fencing_revision": lease.fencing_revision,
        "status": lease.status,
        "acquired_at": lease.acquired_at,
        "updated_at": lease.updated_at,
        "expires_at": lease.expires_at,
    }
    values.update(changes)
    return FleetLease(**values)  # type: ignore[arg-type]
