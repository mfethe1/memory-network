"""Allowlisted peer recovery runner for OpenClaw hostd."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import shlex
import subprocess
from typing import Any

from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.config import RecoveryTarget


ALLOWED_RECOVERY_COMMAND_KINDS = frozenset(
    {"health-check", "process-check", "service-restart", "index-update"}
)


@dataclass(frozen=True)
class RecoveryCommandResult:
    command_kind: str
    target_host_id: str
    runner_host_id: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_payload(self, *, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "kind": "openclaw.recovery.result",
            "schema_version": 1,
            "command_kind": self.command_kind,
            "target_host_id": self.target_host_id,
            "runner_host_id": self.runner_host_id,
            "status": self.status,
            "request": dict(request),
        }
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        if self.stdout:
            payload["stdout"] = self.stdout
        if self.stderr:
            payload["stderr"] = self.stderr
        if self.error is not None:
            payload["error"] = self.error
        return payload


class RecoveryRunner:
    def __init__(
        self,
        config: HostDaemonConfig,
        *,
        runner_host_id: str,
        nats_client: Any | None = None,
        executor: Any | None = None,
        timeout_seconds: int | float = 120,
    ) -> None:
        self.config = config
        self.runner_host_id = _required_text(runner_host_id, "runner_host_id")
        self.nats_client = nats_client
        self.executor = executor or subprocess.run
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._targets = {target.host_id: target for target in config.recovery_targets}

    def handle_recovery_request(
        self,
        request: Mapping[str, Any],
    ) -> RecoveryCommandResult:
        command_kind = _required_text(request.get("command_kind"), "command_kind")
        target_host_id = _required_text(request.get("target_host_id"), "target_host_id")
        runner_host_id = _required_text(request.get("runner_host_id"), "runner_host_id")
        if runner_host_id != self.runner_host_id:
            return self._rejected(command_kind, target_host_id, "wrong runner")
        if target_host_id == self.runner_host_id:
            return self._rejected(command_kind, target_host_id, "self recovery is not allowed")
        if command_kind not in ALLOWED_RECOVERY_COMMAND_KINDS:
            return self._rejected(command_kind, target_host_id, "command kind is not allowed")
        target = self._targets.get(target_host_id)
        if target is None:
            return self._rejected(command_kind, target_host_id, "target is not configured")

        argv = ssh_command_for_target(target, command_kind)
        try:
            completed = self.executor(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return RecoveryCommandResult(
                command_kind=command_kind,
                target_host_id=target_host_id,
                runner_host_id=self.runner_host_id,
                status="failed",
                error=str(exc),
            )
        return RecoveryCommandResult(
            command_kind=command_kind,
            target_host_id=target_host_id,
            runner_host_id=self.runner_host_id,
            status="completed" if int(completed.returncode) == 0 else "failed",
            returncode=int(completed.returncode),
            stdout=str(getattr(completed, "stdout", "") or "").strip(),
            stderr=str(getattr(completed, "stderr", "") or "").strip(),
        )

    def handle_and_publish(self, request: Mapping[str, Any]) -> RecoveryCommandResult:
        result = self.handle_recovery_request(request)
        if self.nats_client is not None:
            self.nats_client.publish(
                f"openclaw.recovery.{self.runner_host_id}.results",
                result.to_payload(request=request),
            )
        return result

    def _rejected(
        self,
        command_kind: str,
        target_host_id: str,
        error: str,
    ) -> RecoveryCommandResult:
        return RecoveryCommandResult(
            command_kind=command_kind,
            target_host_id=target_host_id,
            runner_host_id=self.runner_host_id,
            status="rejected",
            error=error,
        )


def ssh_command_for_target(
    target: RecoveryTarget,
    command_kind: str,
) -> list[str]:
    return ["ssh", _ssh_destination(target), _remote_command(target, command_kind)]


def _remote_command(target: RecoveryTarget, command_kind: str) -> str:
    if command_kind == "health-check":
        if target.platform == "darwin":
            return (
                "launchctl print "
                f"gui/$(id -u)/{shlex.quote(_service_name(target))} >/dev/null"
            )
        return f"systemctl --user is-active {shlex.quote(_service_name(target))}"
    if command_kind == "process-check":
        return "pgrep -af 'code-index-openclaw-hostd|graph-server|code_index' || true"
    if command_kind == "service-restart":
        if target.platform == "darwin":
            return (
                "launchctl kickstart -k "
                f"gui/$(id -u)/{shlex.quote(_service_name(target))}"
            )
        return f"systemctl --user restart {shlex.quote(_service_name(target))}"
    if command_kind == "index-update":
        python = shlex.quote(target.python or "python3")
        return (
            f"cd {shlex.quote(str(target.repo_root))} && "
            f"{python} -m code_index update --all"
        )
    raise ValueError(f"unsupported recovery command kind: {command_kind}")


def _service_name(target: RecoveryTarget) -> str:
    if target.service_name:
        return target.service_name
    if target.platform == "darwin":
        return "ai.openclaw.memory-claude-m1.hostd"
    return "ai.openclaw.memory-claude-m1.hostd.service"


def _ssh_destination(target: RecoveryTarget) -> str:
    if target.ssh_user:
        return f"{target.ssh_user}@{target.ssh_host}"
    return target.ssh_host


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text
