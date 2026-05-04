"""Task and host message inbox handling for OpenClaw hostd."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterator


NON_MUTATING_MESSAGE_TYPES = frozenset({"chat", "event", "summary", "alert"})
TASK_ASSIGNMENT_KINDS = frozenset(
    {"openclaw.task_assignment", "openclaw.task.assigned"}
)
HOST_DELIVERY_KINDS = frozenset(
    {"openclaw.host_delivery", "openclaw.message_delivery"}
)


class InboxValidationError(ValueError):
    """Raised when an inbox message envelope is invalid."""


@dataclass(frozen=True)
class TaskInboxResult:
    task_id: str
    run_id: str
    status: str
    duplicate: bool
    ack_published: bool


@dataclass(frozen=True)
class MessageInboxResult:
    message_id: str
    delivery_id: str
    status: str
    duplicate: bool
    ack_published: bool


class TaskInbox:
    """Validate assigned tasks, dispatch them once, and publish task ACKs."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        host_id: str,
        graph_client: Any,
        nats_client: Any | None = None,
        outbox: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.host_id = _required_text(host_id, "host_id")
        self.graph_client = graph_client
        self.nats_client = nats_client
        self.outbox = outbox
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.apply_schema()

    def close(self) -> None:
        self.conn.close()

    def apply_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS openclaw_task_inbox (
              task_id TEXT PRIMARY KEY,
              message_id TEXT NOT NULL,
              delivery_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openclaw_task_ack_log (
              message_id TEXT NOT NULL,
              delivery_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              status TEXT NOT NULL,
              ack_published_at TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY(message_id, delivery_id)
            );
            """
        )
        self.conn.commit()

    def handle_task_assignment(self, raw_message: Mapping[str, Any]) -> TaskInboxResult:
        message = _normalise_task_assignment(raw_message, host_id=self.host_id)
        existing = self._task_row(message["task_id"])
        if existing is not None:
            ack_published = self._publish_task_ack_once(
                message,
                run_id=existing["run_id"],
                status="duplicate",
            )
            return TaskInboxResult(
                task_id=message["task_id"],
                run_id=existing["run_id"],
                status="duplicate",
                duplicate=True,
                ack_published=ack_published,
            )

        response = self.graph_client.submit_task(
            task_id=message["task_id"],
            host_id=self.host_id,
            message=message["message"],
            selected_paths=message["selected_paths"],
            provider=message.get("provider"),
            selected_nodes=message["selected_nodes"],
            node=message.get("node"),
            agent_name=message.get("agent_name"),
        )
        if not response.ok:
            error = response.error or "graph-server task dispatch failed"
            raise RuntimeError(error)
        run_id = _run_id(response.payload)
        now = _now()
        with self._transaction():
            self.conn.execute(
                """
                INSERT INTO openclaw_task_inbox (
                  task_id, message_id, delivery_id, run_id, status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    message["task_id"],
                    message["message_id"],
                    message["delivery_id"],
                    run_id,
                    now,
                    now,
                ),
            )
        ack_published = self._publish_task_ack_once(
            message,
            run_id=run_id,
            status="accepted",
        )
        return TaskInboxResult(
            task_id=message["task_id"],
            run_id=run_id,
            status="accepted",
            duplicate=False,
            ack_published=ack_published,
        )

    def _task_row(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM openclaw_task_inbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def _publish_task_ack_once(
        self,
        message: Mapping[str, Any],
        *,
        run_id: str,
        status: str,
    ) -> bool:
        if self._task_ack_row(message["message_id"], message["delivery_id"]):
            return False
        payload = {
            "kind": "openclaw.task_ack",
            "schema_version": 1,
            "host_id": self.host_id,
            "task_id": message["task_id"],
            "message_id": message["message_id"],
            "delivery_id": message["delivery_id"],
            "status": status,
            "run_id": run_id,
        }
        subject = f"openclaw.task.{self.host_id}.ack"
        ack_published = _publish_or_enqueue(
            subject,
            payload,
            nats_client=self.nats_client,
            outbox=self.outbox,
        )
        with self._transaction():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_task_ack_log (
                  message_id, delivery_id, task_id, status, ack_published_at,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message["message_id"],
                    message["delivery_id"],
                    message["task_id"],
                    status,
                    _now() if ack_published else None,
                    _now(),
                ),
            )
        return ack_published

    def _task_ack_row(self, message_id: str, delivery_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM openclaw_task_ack_log
             WHERE message_id = ?
               AND delivery_id = ?
            """,
            (message_id, delivery_id),
        ).fetchone()
        return dict(row) if row else None

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()


class HostInbox:
    """Validate host room deliveries and publish idempotent delivery ACKs."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        host_id: str,
        nats_client: Any | None = None,
        outbox: Any | None = None,
        command_ref_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.host_id = _required_text(host_id, "host_id")
        self.nats_client = nats_client
        self.outbox = outbox
        self.command_ref_verifier = command_ref_verifier
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.apply_schema()

    def close(self) -> None:
        self.conn.close()

    def apply_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS openclaw_message_inbox_acks (
              message_id TEXT NOT NULL,
              delivery_id TEXT NOT NULL,
              status TEXT NOT NULL,
              ack_published_at TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY(message_id, delivery_id)
            );
            """
        )
        self.conn.commit()

    def handle_message_delivery(
        self,
        raw_message: Mapping[str, Any],
    ) -> MessageInboxResult:
        message = _normalise_host_delivery(
            raw_message,
            host_id=self.host_id,
            command_ref_verifier=self.command_ref_verifier,
        )
        existing = self._message_ack_row(message["message_id"], message["delivery_id"])
        if existing is not None:
            return MessageInboxResult(
                message_id=message["message_id"],
                delivery_id=message["delivery_id"],
                status=existing["status"],
                duplicate=True,
                ack_published=False,
            )
        payload = {
            "kind": "openclaw.message_delivery_ack",
            "schema_version": 1,
            "host_id": self.host_id,
            "message_id": message["message_id"],
            "delivery_id": message["delivery_id"],
            "status": "acked",
        }
        subject = f"openclaw.host.{self.host_id}.messages.ack"
        ack_published = _publish_or_enqueue(
            subject,
            payload,
            nats_client=self.nats_client,
            outbox=self.outbox,
        )
        now = _now()
        with self._transaction():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_message_inbox_acks (
                  message_id, delivery_id, status, ack_published_at, created_at
                ) VALUES (?, ?, 'acked', ?, ?)
                """,
                (
                    message["message_id"],
                    message["delivery_id"],
                    now if ack_published else None,
                    now,
                ),
            )
        return MessageInboxResult(
            message_id=message["message_id"],
            delivery_id=message["delivery_id"],
            status="acked",
            duplicate=False,
            ack_published=ack_published,
        )

    def _message_ack_row(
        self,
        message_id: str,
        delivery_id: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM openclaw_message_inbox_acks
             WHERE message_id = ?
               AND delivery_id = ?
            """,
            (message_id, delivery_id),
        ).fetchone()
        return dict(row) if row else None

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()


def _normalise_task_assignment(
    raw_message: Mapping[str, Any],
    *,
    host_id: str,
) -> dict[str, Any]:
    if not isinstance(raw_message, Mapping):
        raise InboxValidationError("task assignment must be an object")
    kind = _required_text(raw_message.get("kind"), "kind")
    if kind not in TASK_ASSIGNMENT_KINDS:
        raise InboxValidationError("kind must be an OpenClaw task assignment")
    _require_schema_version_one(raw_message.get("schema_version"))
    target_host_id = _required_text(raw_message.get("host_id"), "host_id")
    if target_host_id != host_id:
        raise InboxValidationError("host_id does not match this host")
    message = {
        "task_id": _required_text(raw_message.get("task_id"), "task_id"),
        "message_id": _required_text(raw_message.get("message_id"), "message_id"),
        "delivery_id": _required_text(raw_message.get("delivery_id"), "delivery_id"),
        "message": _required_text(raw_message.get("message"), "message"),
        "selected_paths": _string_list(raw_message.get("selected_paths")),
        "selected_nodes": _string_list(raw_message.get("selected_nodes")),
    }
    provider = _optional_text(raw_message.get("provider"))
    if provider is not None:
        message["provider"] = provider
    agent_name = _optional_text(raw_message.get("agent_name"))
    if agent_name is not None:
        message["agent_name"] = agent_name
    node = raw_message.get("node")
    if node is not None:
        if not isinstance(node, Mapping):
            raise InboxValidationError("node must be an object")
        message["node"] = dict(node)
    return message


def _normalise_host_delivery(
    raw_message: Mapping[str, Any],
    *,
    host_id: str,
    command_ref_verifier: Callable[[Mapping[str, Any]], bool] | None,
) -> dict[str, Any]:
    if not isinstance(raw_message, Mapping):
        raise InboxValidationError("host delivery must be an object")
    kind = _required_text(raw_message.get("kind"), "kind")
    if kind not in HOST_DELIVERY_KINDS:
        raise InboxValidationError("kind must be an OpenClaw host delivery")
    _require_schema_version_one(raw_message.get("schema_version"))
    target_host_id = _required_text(raw_message.get("host_id"), "host_id")
    if target_host_id != host_id:
        raise InboxValidationError("host_id does not match this host")
    message_type = str(raw_message.get("message_type") or "chat").strip().lower()
    command_ref = raw_message.get("command_ref")
    if message_type == "command":
        if not isinstance(command_ref, Mapping):
            raise InboxValidationError("command_ref is required for command delivery")
        if command_ref_verifier is None:
            raise InboxValidationError("command_ref verifier is required")
        if not command_ref_verifier(command_ref):
            raise InboxValidationError("command_ref verification failed")
    elif message_type not in NON_MUTATING_MESSAGE_TYPES:
        raise InboxValidationError(
            "message_type must be command, chat, event, summary, or alert"
        )
    elif command_ref is not None:
        raise InboxValidationError("non-mutating deliveries cannot include command_ref")
    return {
        "message_id": _required_text(raw_message.get("message_id"), "message_id"),
        "delivery_id": _required_text(raw_message.get("delivery_id"), "delivery_id"),
        "message_type": message_type,
        "command_ref": command_ref,
    }


def _publish_or_enqueue(
    subject: str,
    payload: Mapping[str, Any],
    *,
    nats_client: Any | None,
    outbox: Any | None,
) -> bool:
    if nats_client is not None:
        try:
            nats_client.publish(subject, dict(payload))
            return True
        except Exception:
            if outbox is None:
                return False
    if outbox is not None:
        outbox.enqueue(subject, dict(payload))
    return False


def _run_id(payload: Mapping[str, Any]) -> str:
    run = payload.get("run") if isinstance(payload.get("run"), Mapping) else {}
    value = run.get("run_id") or payload.get("run_id")
    return _required_text(value, "run_id")


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InboxValidationError(f"{field_name} is required")
    return text


def _require_schema_version_one(value: Any) -> None:
    try:
        version = int(value or 0)
    except (TypeError, ValueError):
        version = 0
    if version != 1:
        raise InboxValidationError("schema_version must be 1")


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = list(value)
    else:
        raise InboxValidationError("expected a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
