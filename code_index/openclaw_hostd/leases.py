"""Fleet-level OpenClaw leases and no-progress helpers."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import threading
from typing import Any


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


class InMemoryFleetLeaseStore:
    """Thread-safe in-memory fleet lease store for tests and local services."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str], FleetLease] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._tasks: dict[str, FleetTaskRecord] = {}

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
            if active.owner_host_id != owner_host_id:
                raise LeaseOwnerError(
                    f"{key[0]} lease for {key[1]} is owned by "
                    f"{active.owner_host_id}, not {owner_host_id}"
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
            if active.owner_host_id != owner_host_id:
                raise LeaseOwnerError(
                    f"{key[0]} lease for {key[1]} is owned by "
                    f"{active.owner_host_id}, not {owner_host_id}"
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


def _lease_scope(value: str) -> str:
    scope = _required_text(value, "scope").lower()
    if scope not in LEASE_SCOPES:
        raise ValueError("lease scope must be host, repo, or task")
    return scope


def release_task_lease_on_terminal_status(
    lease_store: InMemoryFleetLeaseStore,
    *,
    task_id: str,
    owner_host_id: str,
    fencing_revision: int,
    terminal_status: str,
    run_id: str | None = None,
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
        fencing_revision=fencing_revision,
        now=timestamp,
    )
    lease_store.record_task_status(
        task_id,
        status=status,
        host_id=owner_host_id,
        run_id=run_id,
        now=timestamp,
    )
    return released


def revoke_no_progress_task_leases(
    lease_store: InMemoryFleetLeaseStore,
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
    fallback: Mapping[str, Any] | None = None
    for state in states:
        host_id = _optional_text(state.get("host_id"))
        run_id = _optional_text(state.get("run_id"))
        if host_id and host_id != lease.owner_host_id:
            continue
        if lease.owner_run_id and run_id and run_id != lease.owner_run_id:
            continue
        if host_id == lease.owner_host_id and (
            not lease.owner_run_id or run_id == lease.owner_run_id
        ):
            return state
        if fallback is None:
            fallback = state
    return fallback


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
