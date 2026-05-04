"""CMA SSH recovery allowlist and authorization policy for OpenClaw M2."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from code_index.openclaw_controller.models import HOST_HEALTHY
from code_index.openclaw_controller.models import Rejection


ALLOWED_SSH_COMMAND_KINDS: frozenset[str] = frozenset(
    {
        "health-check",
        "process-check",
        "service-restart",
        "index-update",
    }
)


@dataclass(frozen=True)
class SshRecoveryAttempt:
    attempt_id: str
    command_kind: str
    target_host_id: str
    status: str
    rejection_reason: str | None
    rejection_message: str | None
    authorized_at: str | None
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "command_kind": self.command_kind,
            "target_host_id": self.target_host_id,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "rejection_message": self.rejection_message,
            "authorized_at": self.authorized_at,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class SshRecoveryResult:
    status: str
    command_kind: str
    target_host_id: str
    rejection: Rejection | None
    attempt: SshRecoveryAttempt

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "command_kind": self.command_kind,
            "target_host_id": self.target_host_id,
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "attempt": self.attempt.to_dict(),
        }


class SshRecoveryPolicy:
    """Fleet Controller SSH recovery authorization for stale Windows hosts.

    Allowed command kinds: health-check, process-check, service-restart,
    index-update. Authorization requires the target host to be stale, have
    no active fleet leases, and have no active local file claims.

    All authorization attempts — authorized or rejected — are appended to an
    in-memory audit log accessible via ``list_audit_log()``. Production
    deployments should persist this log to a durable fleet event store.
    """

    ALLOWED_COMMAND_KINDS: frozenset[str] = ALLOWED_SSH_COMMAND_KINDS

    def __init__(
        self,
        *,
        fleet_controller: Any,
        lease_store: Any,
        claims_store: Any | None = None,
    ) -> None:
        self._fleet_controller = fleet_controller
        self._lease_store = lease_store
        self._claims_store = claims_store
        self._audit_log: list[SshRecoveryAttempt] = []

    def authorize_recovery(
        self,
        command_kind: str,
        target_host_id: str,
        *,
        now: datetime | None = None,
    ) -> SshRecoveryResult:
        timestamp = _utc(now)
        attempt_id = _attempt_id(command_kind, target_host_id, timestamp)

        rejection = self._check(command_kind, target_host_id, now=timestamp)

        if rejection is not None:
            attempt = SshRecoveryAttempt(
                attempt_id=attempt_id,
                command_kind=command_kind,
                target_host_id=target_host_id,
                status="rejected",
                rejection_reason=rejection.reason,
                rejection_message=rejection.message,
                authorized_at=None,
                recorded_at=timestamp.isoformat(),
            )
            self._audit_log.append(attempt)
            return SshRecoveryResult(
                status="rejected",
                command_kind=command_kind,
                target_host_id=target_host_id,
                rejection=rejection,
                attempt=attempt,
            )

        attempt = SshRecoveryAttempt(
            attempt_id=attempt_id,
            command_kind=command_kind,
            target_host_id=target_host_id,
            status="authorized",
            rejection_reason=None,
            rejection_message=None,
            authorized_at=timestamp.isoformat(),
            recorded_at=timestamp.isoformat(),
        )
        self._audit_log.append(attempt)
        return SshRecoveryResult(
            status="authorized",
            command_kind=command_kind,
            target_host_id=target_host_id,
            rejection=None,
            attempt=attempt,
        )

    def list_audit_log(self) -> list[dict[str, Any]]:
        return [attempt.to_dict() for attempt in self._audit_log]

    def _check(
        self,
        command_kind: str,
        target_host_id: str,
        *,
        now: datetime,
    ) -> Rejection | None:
        if not isinstance(command_kind, str) or not command_kind.strip():
            return Rejection(
                reason="missing_command_kind",
                message="command_kind is required",
            )
        if command_kind not in self.ALLOWED_COMMAND_KINDS:
            return Rejection(
                reason="unknown_command_kind",
                message=(
                    f"SSH recovery command {command_kind!r} is not in the allowlist; "
                    f"allowed: {', '.join(sorted(self.ALLOWED_COMMAND_KINDS))}"
                ),
                details={"allowed_kinds": sorted(self.ALLOWED_COMMAND_KINDS)},
            )
        if not isinstance(target_host_id, str) or not target_host_id.strip():
            return Rejection(
                reason="missing_target_host_id",
                message="target_host_id is required",
            )

        hosts = getattr(self._fleet_controller, "_hosts", {})
        host = hosts.get(target_host_id)
        if host is None:
            return Rejection(
                reason="host_not_found",
                message=(
                    f"target host {target_host_id!r} is not in the fleet inventory"
                ),
                details={"target_host_id": target_host_id},
            )
        health = host.health_at(now)
        if health == HOST_HEALTHY:
            return Rejection(
                reason="host_not_stale",
                message=(
                    "SSH recovery requires a stale or unhealthy host; "
                    f"target host {target_host_id!r} is currently {health!r}"
                ),
                details={"target_host_id": target_host_id, "health": health},
            )

        list_active = getattr(self._lease_store, "list_active_leases", None)
        if list_active is not None:
            active_leases = [
                lease
                for lease in list_active(now=now)
                if lease.owner_host_id == target_host_id
            ]
            if active_leases:
                return Rejection(
                    reason="active_leases",
                    message=(
                        f"SSH recovery cannot proceed while host {target_host_id!r} "
                        "holds active fleet leases"
                    ),
                    details={
                        "target_host_id": target_host_id,
                        "active_lease_count": len(active_leases),
                        "lease_ids": [lease.lease_id for lease in active_leases],
                    },
                )

        if self._claims_store is not None:
            has_claims = getattr(self._claims_store, "has_active_file_claims", None)
            if has_claims is not None and has_claims(target_host_id):
                return Rejection(
                    reason="active_file_claims",
                    message=(
                        f"SSH recovery cannot proceed while host {target_host_id!r} "
                        "has active local file claims"
                    ),
                    details={"target_host_id": target_host_id},
                )

        return None


def _utc(value: datetime | None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _attempt_id(command_kind: str, target_host_id: str, timestamp: datetime) -> str:
    key = f"{command_kind}\0{target_host_id}\0{timestamp.isoformat()}"
    digest = hmac.new(
        b"openclaw-ssh-recovery-attempt-id",
        key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"sshrec_{digest}"
