"""Fleet Controller scheduling and handoff authorization service."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from code_index.openclaw_controller.models import AssignmentDetails
from code_index.openclaw_controller.models import AssignmentResult
from code_index.openclaw_controller.models import HandoffResult
from code_index.openclaw_controller.models import HOST_HEALTHY
from code_index.openclaw_controller.models import HostInventoryRecord
from code_index.openclaw_controller.models import Rejection
from code_index.openclaw_controller.models import TaskAssignment
from code_index.openclaw_hostd.leases import LeaseConflictError
from code_index.openclaw_messaging.store import MessagingStore


DEFAULT_RUN_STALE_AFTER = timedelta(minutes=10)


class FleetController:
    """Testable controller core for Slice 6 fleet assignment policy."""

    def __init__(
        self,
        *,
        messaging_store: MessagingStore,
        lease_store: Any,
        nats_client: Any | None = None,
        run_stale_after: timedelta = DEFAULT_RUN_STALE_AFTER,
        restart_cooldown_seconds: int | float = 90,
        lease_ttl_seconds: int | float | None = 1800,
    ) -> None:
        self.messaging_store = messaging_store
        self.lease_store = lease_store
        self.nats_client = nats_client
        self.run_stale_after = run_stale_after
        self.restart_cooldown = timedelta(seconds=max(0, float(restart_cooldown_seconds)))
        self.lease_ttl_seconds = lease_ttl_seconds
        self._hosts: dict[str, HostInventoryRecord] = {}
        self._agent_states: dict[tuple[str, str], dict[str, Any]] = {}
        self._context_health: dict[tuple[str, str], dict[str, Any]] = {}
        self._handoffs: dict[tuple[str, str], dict[str, Any]] = {}
        self._restart_authorized_at: dict[str, datetime] = {}

    def record_host_heartbeat(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc(now)
        host = HostInventoryRecord.from_heartbeat(payload, now=timestamp)
        self._hosts[host.host_id] = host
        return host.to_projection(
            now=timestamp,
            context_health=self._host_context_health(host.host_id),
            handoff_state=self._host_handoff_state(host.host_id),
        )

    def record_agent_state(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        del now
        host_id = _required_text(payload.get("host_id"), "host_id")
        run_id = _required_text(payload.get("run_id"), "run_id")
        state = dict(self._agent_states.get((host_id, run_id), {}))
        state.update(dict(payload))
        self._agent_states[(host_id, run_id)] = state
        put_agent_state = getattr(self.lease_store, "put_agent_state", None)
        if put_agent_state is not None:
            put_agent_state(f"{host_id}.{run_id}", state)
        return state

    def record_run_event(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc(now)
        host_id = _required_text(payload.get("host_id"), "host_id")
        run_id = _required_text(payload.get("run_id"), "run_id")
        key = (host_id, run_id)
        state = dict(self._agent_states.get(key, {}))
        state["host_id"] = host_id
        state["run_id"] = run_id
        task_id = _optional_text(payload.get("task_id"))
        if task_id is not None:
            state["task_id"] = task_id
        status = _optional_text(payload.get("status")) or _optional_text(
            payload.get("run_status")
        )
        if status is not None:
            state["run_status"] = status
        state["last_event_at"] = (
            _parse_datetime(payload.get("generated_at")) or timestamp
        ).isoformat()
        state["last_event_type"] = _optional_text(payload.get("event_type"))
        self._agent_states[key] = state
        return state

    def record_context_health(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc(now)
        host_id = _required_text(payload.get("host_id"), "host_id")
        run_id = _required_text(payload.get("run_id"), "run_id")
        health = dict(payload)
        health.setdefault("recorded_at", timestamp.isoformat())
        self._context_health[(host_id, run_id)] = health
        return health

    def assign_task_from_command_ref(
        self,
        command_ref: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> AssignmentResult:
        timestamp = _utc(now)
        message_id = _optional_text(command_ref.get("message_id"))
        status_rejection = self._command_ref_status_rejection(command_ref)
        if status_rejection is not None:
            return self._assignment_rejected(
                reason=status_rejection.reason,
                message=status_rejection.message,
                message_id=message_id,
                details=status_rejection.details,
            )
        if not self.messaging_store.verify_command_ref(command_ref):
            return self._assignment_rejected(
                reason="invalid_command_ref",
                message="command reference is missing, expired, unsigned, or invalid",
                message_id=message_id,
            )
        stored_command = self.messaging_store.get_command_ref_for_message(
            str(command_ref["message_id"])
        )
        if stored_command is None:
            return self._assignment_rejected(
                reason="invalid_command_ref",
                message="command reference does not exist",
                message_id=message_id,
            )
        if stored_command["command_type"] != "assign_task":
            return self._assignment_rejected(
                reason="unsupported_command_type",
                message="controller task creation requires an assign_task command",
                message_id=message_id,
            )
        claimed_command = self.messaging_store.claim_command_ref(
            stored_command["command_id"],
        )
        if claimed_command is None:
            return self._command_ref_claim_rejected(
                stored_command["message_id"],
            )
        stored_command = claimed_command
        message = self.messaging_store.get_message(stored_command["message_id"])
        room = self.messaging_store.get_room(message["room_id"])
        try:
            details = self._assignment_details(stored_command, message, room)
        except ValueError as exc:
            return self._reject_claimed_assignment(
                stored_command["command_id"],
                reason="missing_assignment_context",
                message=str(exc),
                message_id=stored_command["message_id"],
            )

        selected_host, rejection = self._select_host(
            details,
            message_id=stored_command["message_id"],
            now=timestamp,
        )
        if selected_host is None:
            return self._reject_claimed_assignment(
                stored_command["command_id"],
                reason=rejection.reason,
                message=rejection.message,
                message_id=stored_command["message_id"],
                details=rejection.details,
            )
        delivery = self._host_delivery(
            stored_command["message_id"],
            host_id=selected_host.host_id,
        )
        if delivery is None:
            return self._reject_claimed_assignment(
                stored_command["command_id"],
                reason="delivery_missing",
                message="originating message has no delivery record for selected host",
                message_id=stored_command["message_id"],
                details={"host_id": selected_host.host_id},
            )

        existing_repo_lease = self.lease_store.get_active_lease(
            "repo",
            details.repo_root,
            now=timestamp,
        )
        existing_task_lease = self.lease_store.get_active_lease(
            "task",
            details.task_id,
            now=timestamp,
        )
        repo_lease = None
        try:
            repo_lease = self.lease_store.acquire_lease(
                "repo",
                details.repo_root,
                owner_host_id=selected_host.host_id,
                ttl_seconds=self.lease_ttl_seconds,
                now=timestamp,
            )
            task_lease = self.lease_store.acquire_lease(
                "task",
                details.task_id,
                owner_host_id=selected_host.host_id,
                ttl_seconds=self.lease_ttl_seconds,
                now=timestamp,
            )
        except LeaseConflictError as exc:
            if repo_lease is not None and existing_repo_lease is None:
                self._release_lease_quietly(
                    "repo",
                    repo_lease.resource_id,
                    owner_host_id=selected_host.host_id,
                    fencing_revision=repo_lease.fencing_revision,
                    now=timestamp,
                )
            return self._reject_claimed_assignment(
                stored_command["command_id"],
                reason=(
                    "repo_lease_conflict"
                    if "repo lease" in str(exc)
                    else "task_lease_conflict"
                ),
                message=str(exc),
                message_id=stored_command["message_id"],
            )

        subject = f"openclaw.task.{selected_host.host_id}.assigned"
        payload = self._task_publish_payload(
            details,
            host_id=selected_host.host_id,
            message_id=stored_command["message_id"],
            delivery_id=delivery["delivery_id"],
        )
        try:
            self._publish(subject, payload)
        except Exception as exc:
            self._release_new_leases(
                repo_lease=repo_lease,
                task_lease=task_lease,
                existing_repo_lease=existing_repo_lease,
                existing_task_lease=existing_task_lease,
                host_id=selected_host.host_id,
                now=timestamp,
            )
            return self._reject_claimed_assignment(
                stored_command["command_id"],
                reason="nats_publish_failed",
                message=str(exc),
                message_id=stored_command["message_id"],
            )

        self.messaging_store.mark_command_ref_status(
            stored_command["command_id"],
            status="assigned",
        )
        assignment = TaskAssignment(
            task_id=details.task_id,
            host_id=selected_host.host_id,
            repo_root=details.repo_root,
            provider=details.provider,
            message_id=stored_command["message_id"],
            delivery_id=delivery["delivery_id"],
            nats_subject=subject,
            repo_lease_id=repo_lease.lease_id,
            task_lease_id=task_lease.lease_id,
        )
        return AssignmentResult(
            status="assigned",
            assignment=assignment,
            rejection=None,
            room_message_update={
                "message_id": stored_command["message_id"],
                "event_type": "fleet_assignment",
                "status": "assigned",
                "summary": (
                    f"Assigned {details.task_id} to {selected_host.host_id} "
                    f"with {details.provider}."
                ),
                "assignment": assignment.to_dict(),
            },
        )

    def submit_handoff_proposal(
        self,
        proposal: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> HandoffResult:
        timestamp = _utc(now)
        message_id = _optional_text(proposal.get("message_id"))
        try:
            handoff_id = _required_text(proposal.get("handoff_id"), "handoff_id")
            host_id = _required_text(proposal.get("host_id"), "host_id")
            task_id = _required_text(proposal.get("task_id"), "task_id")
            run_id = _required_text(proposal.get("run_id"), "run_id")
            repo_root = _required_text(proposal.get("repo_root"), "repo_root")
            provider = _required_text(proposal.get("provider"), "provider")
            required_provider_capabilities = _first_string_tuple(
                proposal,
                (
                    "required_provider_capabilities",
                    "provider_capabilities",
                    "required_capabilities",
                ),
            )
        except ValueError as exc:
            return self._handoff_rejected(
                reason="invalid_handoff_proposal",
                message=str(exc),
                message_id=message_id,
            )

        host = self._hosts.get(host_id)
        if host is None:
            return self._handoff_rejected(
                reason="host_ineligible",
                message="handoff target host is not eligible for this repo/provider",
                message_id=message_id,
            )

        repo_lease = self.lease_store.get_active_lease(
            "repo",
            repo_root,
            now=timestamp,
        )
        task_lease = self.lease_store.get_active_lease(
            "task",
            task_id,
            now=timestamp,
        )
        if (
            repo_lease is None
            or repo_lease.owner_host_id != host_id
            or task_lease is None
            or task_lease.owner_host_id != host_id
            or task_lease.owner_run_id != run_id
        ):
            return self._handoff_rejected(
                reason="lease_invalid",
                message="handoff restart requires active repo and task leases owned by the target run",
                message_id=message_id,
            )

        if not self._host_is_eligible(
            host,
            repo_root=repo_root,
            provider=provider,
            required_provider_capabilities=required_provider_capabilities,
            now=timestamp,
        ):
            return self._handoff_rejected(
                reason="host_ineligible",
                message="handoff target host is not eligible for this repo/provider",
                message_id=message_id,
            )

        last_authorized = self._restart_authorized_at.get(run_id)
        if (
            last_authorized is not None
            and timestamp - last_authorized < self.restart_cooldown
        ):
            return self._handoff_rejected(
                reason="restart_cooldown_active",
                message="fresh provider restart cooldown is still active",
                message_id=message_id,
                details={
                    "run_id": run_id,
                    "last_authorized_at": last_authorized.isoformat(),
                    "cooldown_seconds": int(self.restart_cooldown.total_seconds()),
                },
            )

        handoff = {
            "handoff_id": handoff_id,
            "status": "authorized",
            "host_id": host_id,
            "task_id": task_id,
            "run_id": run_id,
            "repo_root": repo_root,
            "provider": provider,
            "required_provider_capabilities": list(required_provider_capabilities),
            "authorized_at": timestamp.isoformat(),
            "repo_lease_id": repo_lease.lease_id,
            "task_lease_id": task_lease.lease_id,
            "reason": _optional_text(proposal.get("reason")),
        }
        self._restart_authorized_at[run_id] = timestamp
        self._handoffs[(task_id, run_id)] = handoff
        return HandoffResult(
            status="authorized",
            handoff=handoff,
            rejection=None,
            room_message_update={
                "message_id": message_id,
                "event_type": "fleet_handoff",
                "status": "authorized",
                "summary": f"Authorized fresh provider handoff for {run_id}.",
                "handoff": handoff,
            },
        )

    def project_fleet(self, *, now: datetime | None = None) -> dict[str, Any]:
        timestamp = _utc(now)
        hosts = [
            host.to_projection(
                now=timestamp,
                context_health=self._host_context_health(host.host_id),
                handoff_state=self._host_handoff_state(host.host_id),
            )
            for host in sorted(self._hosts.values(), key=lambda item: item.host_id)
        ]
        run_keys = set(self._agent_states) | set(self._context_health)
        for handoff in self._handoffs.values():
            host_id = _optional_text(handoff.get("host_id"))
            run_id = _optional_text(handoff.get("run_id"))
            if host_id and run_id:
                run_keys.add((host_id, run_id))
        runs = [
            self._run_projection(
                key,
                self._agent_states.get(key, {}),
                now=timestamp,
            )
            for key in sorted(run_keys)
        ]
        return {"hosts": hosts, "runs": runs}

    def _assignment_details(
        self,
        command: Mapping[str, Any],
        message: Mapping[str, Any],
        room: Mapping[str, Any],
    ) -> AssignmentDetails:
        room_metadata = room.get("metadata") if isinstance(room.get("metadata"), Mapping) else {}
        message_metadata = (
            message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
        )
        assignment = {}
        if isinstance(room_metadata.get("assignment"), Mapping):
            assignment.update(room_metadata["assignment"])
        if isinstance(message_metadata.get("assignment"), Mapping):
            assignment.update(message_metadata["assignment"])
        task_id = _optional_text(command.get("task_id"))
        if task_id is None:
            target_scope = message.get("target_scope")
            if isinstance(target_scope, Mapping):
                task_id = _optional_text(target_scope.get("task_id"))
        return AssignmentDetails(
            task_id=_required_text(task_id, "task_id"),
            repo_root=_required_text(assignment.get("repo_root"), "repo_root"),
            provider=_required_text(assignment.get("provider"), "provider"),
            message=_required_text(message.get("body"), "message"),
            selected_paths=_string_tuple(assignment.get("selected_paths")),
            selected_nodes=_string_tuple(assignment.get("selected_nodes")),
            required_provider_capabilities=_first_string_tuple(
                assignment,
                (
                    "required_provider_capabilities",
                    "provider_capabilities",
                    "required_capabilities",
                ),
            ),
            node=(
                dict(assignment["node"])
                if isinstance(assignment.get("node"), Mapping)
                else None
            ),
            agent_name=_optional_text(assignment.get("agent_name")),
        )

    def _select_host(
        self,
        details: AssignmentDetails,
        *,
        message_id: str,
        now: datetime,
    ) -> tuple[HostInventoryRecord | None, Rejection]:
        failures: list[dict[str, str]] = []
        for host in sorted(self._hosts.values(), key=lambda item: item.host_id):
            reason = self._host_rejection_reason(
                host,
                details,
                message_id=message_id,
                now=now,
            )
            if reason is not None:
                failures.append({"host_id": host.host_id, "reason": reason})
                continue
            return host, Rejection("", "")
        reason = _dominant_rejection_reason(failures)
        messages = {
            "repo_lease_conflict": "repo lease is held by another host",
            "task_lease_conflict": "task lease is already active",
            "no_eligible_hosts": "no eligible host can run this task",
        }
        return None, Rejection(
            reason=reason,
            message=messages.get(reason, "no eligible host can run this task"),
            details={"candidates": failures},
        )

    def _host_rejection_reason(
        self,
        host: HostInventoryRecord,
        details: AssignmentDetails,
        *,
        message_id: str | None = None,
        now: datetime,
    ) -> str | None:
        if host.health_at(now) != HOST_HEALTHY:
            return "host_health"
        if not host.supports_repo_root(details.repo_root):
            return "repo_root_mismatch"
        if not host.supports_provider(details.provider):
            return "provider_unavailable"
        if not host.supports_provider_capabilities(
            details.provider,
            details.required_provider_capabilities,
        ):
            return "provider_capability_missing"
        if message_id is not None and self._host_delivery(
            message_id,
            host_id=host.host_id,
        ) is None:
            return "delivery_missing"
        repo_lease = self.lease_store.get_active_lease(
            "repo",
            details.repo_root,
            now=now,
        )
        if repo_lease is not None and repo_lease.owner_host_id != host.host_id:
            return "repo_lease_conflict"
        task_lease = self.lease_store.get_active_lease(
            "task",
            details.task_id,
            now=now,
        )
        if task_lease is not None:
            return "task_lease_conflict"
        return None

    def _host_is_eligible(
        self,
        host: HostInventoryRecord,
        *,
        repo_root: str,
        provider: str,
        required_provider_capabilities: tuple[str, ...] = (),
        now: datetime,
    ) -> bool:
        return (
            host.health_at(now) == HOST_HEALTHY
            and host.supports_repo_root(repo_root)
            and host.supports_provider(provider)
            and host.supports_provider_capabilities(
                provider,
                required_provider_capabilities,
            )
        )

    def _host_delivery(
        self,
        message_id: str,
        *,
        host_id: str,
    ) -> dict[str, Any] | None:
        for delivery in self.messaging_store.list_deliveries(message_id):
            if delivery["recipient_kind"] == "host" and delivery["recipient_id"] == host_id:
                return delivery
        return None

    def _task_publish_payload(
        self,
        details: AssignmentDetails,
        *,
        host_id: str,
        message_id: str,
        delivery_id: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "openclaw.task.assigned",
            "schema_version": 1,
            "host_id": host_id,
            "task_id": details.task_id,
            "message_id": message_id,
            "delivery_id": delivery_id,
            "message": details.message,
            "repo_root": details.repo_root,
            "provider": details.provider,
            "selected_paths": list(details.selected_paths),
            "selected_nodes": list(details.selected_nodes),
        }
        if details.required_provider_capabilities:
            payload["required_provider_capabilities"] = list(
                details.required_provider_capabilities
            )
        if details.node is not None:
            payload["node"] = dict(details.node)
        if details.agent_name is not None:
            payload["agent_name"] = details.agent_name
        return payload

    def _publish(self, subject: str, payload: Mapping[str, Any]) -> None:
        if self.nats_client is None:
            raise RuntimeError("NATS publisher is not configured")
        publish = getattr(self.nats_client, "publish", None)
        if publish is None:
            raise RuntimeError("NATS publisher has no publish()")
        publish(subject, dict(payload))

    def _release_new_leases(
        self,
        *,
        repo_lease: Any,
        task_lease: Any,
        existing_repo_lease: Any | None,
        existing_task_lease: Any | None,
        host_id: str,
        now: datetime,
    ) -> None:
        if existing_task_lease is None:
            self.lease_store.release_lease(
                "task",
                task_lease.resource_id,
                owner_host_id=host_id,
                fencing_revision=task_lease.fencing_revision,
                now=now,
            )
        if existing_repo_lease is None:
            self.lease_store.release_lease(
                "repo",
                repo_lease.resource_id,
                owner_host_id=host_id,
                fencing_revision=repo_lease.fencing_revision,
                now=now,
            )

    def _release_lease_quietly(
        self,
        scope: str,
        resource_id: str,
        *,
        owner_host_id: str,
        fencing_revision: int,
        now: datetime,
    ) -> None:
        try:
            self.lease_store.release_lease(
                scope,
                resource_id,
                owner_host_id=owner_host_id,
                fencing_revision=fencing_revision,
                now=now,
            )
        except Exception:
            return

    def _command_ref_status_rejection(
        self,
        command_ref: Mapping[str, Any],
    ) -> Rejection | None:
        message_id = _optional_text(command_ref.get("message_id"))
        command_id = _optional_text(command_ref.get("command_id"))
        if message_id is None or command_id is None:
            return None
        stored = self.messaging_store.get_command_ref_for_message(message_id)
        if stored is None or stored["command_id"] != command_id:
            return None
        details = {"command_id": command_id, "status": stored["status"]}
        if stored["status"] == "active":
            return Rejection(
                reason="command_ref_claimed",
                message="command reference is already being assigned",
                details=details,
            )
        if stored["status"] in {"assigned", "rejected", "cancelled"}:
            return Rejection(
                reason="command_ref_consumed",
                message="command reference has already been consumed",
                details=details,
            )
        return None

    def _command_ref_claim_rejected(
        self,
        message_id: str,
    ) -> AssignmentResult:
        stored = self.messaging_store.get_command_ref_for_message(message_id)
        status = _optional_text(stored.get("status")) if stored is not None else None
        command_id = _optional_text(stored.get("command_id")) if stored is not None else None
        details = {"status": status or "unknown"}
        if command_id is not None:
            details["command_id"] = command_id
        if status == "active":
            return self._assignment_rejected(
                reason="command_ref_claimed",
                message="command reference is already being assigned",
                message_id=message_id,
                details=details,
            )
        if status in {"assigned", "rejected", "cancelled"}:
            return self._assignment_rejected(
                reason="command_ref_consumed",
                message="command reference has already been consumed",
                message_id=message_id,
                details=details,
            )
        return self._assignment_rejected(
            reason="command_ref_claim_failed",
            message="command reference could not be claimed for assignment",
            message_id=message_id,
            details=details,
        )

    def _reject_claimed_assignment(
        self,
        command_id: str,
        *,
        reason: str,
        message: str,
        message_id: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> AssignmentResult:
        self.messaging_store.mark_command_ref_status(command_id, status="rejected")
        return self._assignment_rejected(
            reason=reason,
            message=message,
            message_id=message_id,
            details=details,
        )

    def _assignment_rejected(
        self,
        *,
        reason: str,
        message: str,
        message_id: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> AssignmentResult:
        rejection = Rejection(reason=reason, message=message, details=details)
        return AssignmentResult(
            status="rejected",
            assignment=None,
            rejection=rejection,
            room_message_update={
                "message_id": message_id,
                "event_type": "fleet_assignment",
                "status": "rejected",
                "summary": message,
                "rejection": rejection.to_dict(),
            },
        )

    def _handoff_rejected(
        self,
        *,
        reason: str,
        message: str,
        message_id: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> HandoffResult:
        rejection = Rejection(reason=reason, message=message, details=details)
        return HandoffResult(
            status="rejected",
            handoff=None,
            rejection=rejection,
            room_message_update={
                "message_id": message_id,
                "event_type": "fleet_handoff",
                "status": "rejected",
                "summary": message,
                "rejection": rejection.to_dict(),
            },
        )

    def _run_projection(
        self,
        key: tuple[str, str],
        state: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        host_id, run_id = key
        context_health = self._context_health.get((host_id, run_id), {})
        task_id = _optional_text(state.get("task_id")) or _optional_text(
            context_health.get("task_id")
        )
        last_action_at = _parse_datetime(state.get("last_action_at"))
        last_event_at = _parse_datetime(state.get("last_event_at"))
        last_observed_at = _latest_datetime(last_action_at, last_event_at)
        health = "unknown"
        if last_observed_at is not None:
            health = (
                "stale"
                if now - last_observed_at >= self.run_stale_after
                else "healthy"
            )
        handoff = self._handoff_for_run(host_id=host_id, task_id=task_id, run_id=run_id)
        return {
            "host_id": host_id,
            "task_id": task_id,
            "run_id": run_id,
            "agent_run_status": _optional_text(state.get("run_status"))
            or _optional_text(state.get("status")),
            "run_health": health,
            "last_action_at": last_action_at.isoformat() if last_action_at else None,
            "last_event_at": last_event_at.isoformat() if last_event_at else None,
            "last_observed_at": (
                last_observed_at.isoformat() if last_observed_at else None
            ),
            "context_health": dict(context_health),
            "handoff_state": dict(handoff or {}),
        }

    def _handoff_for_run(
        self,
        *,
        host_id: str,
        task_id: str | None,
        run_id: str,
    ) -> dict[str, Any] | None:
        if task_id is not None:
            handoff = self._handoffs.get((task_id, run_id))
            if handoff is not None:
                return handoff
        for handoff in self._handoffs.values():
            if handoff.get("host_id") == host_id and handoff.get("run_id") == run_id:
                return handoff
        return None

    def _host_context_health(self, host_id: str) -> dict[str, Any]:
        return {
            run_id: health
            for (state_host_id, run_id), health in self._context_health.items()
            if state_host_id == host_id
        }

    def _host_handoff_state(self, host_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for (_task_id, run_id), handoff in self._handoffs.items():
            if handoff.get("host_id") == host_id:
                result[run_id] = handoff
        return result


def _dominant_rejection_reason(failures: list[dict[str, str]]) -> str:
    reasons = [failure["reason"] for failure in failures]
    for reason in ("repo_lease_conflict", "task_lease_conflict"):
        if reason in reasons:
            return reason
    return "no_eligible_hosts"


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _first_string_tuple(
    payload: Mapping[str, Any],
    names: tuple[str, ...],
) -> tuple[str, ...]:
    for name in names:
        value = _string_tuple(payload.get(name))
        if value:
            return value
    return ()


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_datetime(value: object) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _latest_datetime(*values: datetime | None) -> datetime | None:
    parsed = [value for value in values if value is not None]
    if not parsed:
        return None
    return max(parsed)


def _utc(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)
