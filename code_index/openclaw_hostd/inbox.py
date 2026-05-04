"""Task and host message inbox handling for OpenClaw hostd."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

from code_index.openclaw_hostd.leases import LeaseConflictError
from code_index.openclaw_hostd.leases import release_task_lease_on_terminal_status


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
        lease_store: Any | None = None,
        lease_ttl_seconds: int | float | None = 1800,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.host_id = _required_text(host_id, "host_id")
        self.graph_client = graph_client
        self.nats_client = nats_client
        self.outbox = outbox
        self.lease_store = lease_store
        self.lease_ttl_seconds = lease_ttl_seconds
        self._lock = threading.RLock()
        self._inflight_task_ids: set[str] = set()
        self._inflight_task_acks: set[tuple[str, str]] = set()
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
                CREATE TABLE IF NOT EXISTS openclaw_task_inbox (
                  task_id TEXT PRIMARY KEY,
                  message_id TEXT NOT NULL,
                  delivery_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  lease_id TEXT,
                  lease_scope TEXT,
                  lease_resource_id TEXT,
                  lease_fencing_revision INTEGER,
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
            _ensure_columns(
                self.conn,
                "openclaw_task_inbox",
                {
                    "lease_id": "TEXT",
                    "lease_scope": "TEXT",
                    "lease_resource_id": "TEXT",
                    "lease_fencing_revision": "INTEGER",
                },
            )
            self.conn.commit()

    def handle_task_assignment(self, raw_message: Mapping[str, Any]) -> TaskInboxResult:
        message = _normalise_task_assignment(raw_message, host_id=self.host_id)
        existing_task = self._task_row(message["task_id"])
        if existing_task is not None and not _recoverable_task_row(existing_task):
            run_id = str(existing_task.get("run_id") or "").strip()
            ack_published = self._publish_task_ack_once(
                message,
                run_id=run_id,
                status="duplicate",
            )
            return TaskInboxResult(
                task_id=message["task_id"],
                run_id=run_id,
                status="duplicate",
                duplicate=True,
                ack_published=ack_published,
            )
        conflict_replay = self._task_ack_row(
            message["message_id"],
            message["delivery_id"],
        )
        if (
            conflict_replay is not None
            and str(conflict_replay.get("status") or "") == "lease_conflict"
        ):
            return TaskInboxResult(
                task_id=message["task_id"],
                run_id="",
                status="lease_conflict",
                duplicate=True,
                ack_published=False,
            )
        planned_run_id = _planned_run_id(self.host_id, message["task_id"])
        task_lease = self._acquire_task_lease(
            message["task_id"],
            run_id=planned_run_id,
        )
        if task_lease is None and self.lease_store is not None:
            ack_published = self._publish_task_ack_once(
                message,
                run_id="",
                status="lease_conflict",
            )
            return TaskInboxResult(
                task_id=message["task_id"],
                run_id="",
                status="lease_conflict",
                duplicate=False,
                ack_published=ack_published,
            )
        existing, should_submit = self._reserve_task_submission(
            message,
            task_lease=task_lease,
        )
        planned_run_id = str(existing.get("run_id") or "").strip()
        if not should_submit:
            if _recoverable_task_row(existing):
                return TaskInboxResult(
                    task_id=message["task_id"],
                    run_id=planned_run_id,
                    status="processing",
                    duplicate=True,
                    ack_published=False,
                )
            ack_published = self._publish_task_ack_once(
                message,
                run_id=planned_run_id,
                status="duplicate",
            )
            return TaskInboxResult(
                task_id=message["task_id"],
                run_id=planned_run_id,
                status="duplicate",
                duplicate=True,
                ack_published=ack_published,
            )

        try:
            response = self.graph_client.submit_task(
                task_id=message["task_id"],
                host_id=self.host_id,
                message=message["message"],
                selected_paths=message["selected_paths"],
                provider=message.get("provider"),
                selected_nodes=message["selected_nodes"],
                node=message.get("node"),
                agent_name=message.get("agent_name"),
                run_id=planned_run_id,
            )
            if not response.ok:
                error = response.error or "graph-server task dispatch failed"
                self._update_task_status(message["task_id"], status="failed")
                self.release_task_lease_on_terminal_status(
                    message["task_id"],
                    terminal_status="failed",
                    run_id=planned_run_id,
                )
                raise RuntimeError(error)
            run_id = _run_id(response.payload)
            self._finalize_task(message["task_id"], run_id=run_id, status="accepted")
            if self.lease_store is not None:
                self.lease_store.record_task_status(
                    message["task_id"],
                    status="accepted",
                    host_id=self.host_id,
                    run_id=run_id,
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
        finally:
            with self._lock:
                self._inflight_task_ids.discard(message["task_id"])

    def release_task_lease_on_terminal_status(
        self,
        task_id: str,
        *,
        terminal_status: str,
        run_id: str | None = None,
    ) -> Any | None:
        if self.lease_store is None:
            return None
        row = self._task_row(task_id)
        if row is None:
            return None
        fencing_revision = row.get("lease_fencing_revision")
        if fencing_revision is None:
            return None
        released = release_task_lease_on_terminal_status(
            self.lease_store,
            task_id=task_id,
            owner_host_id=self.host_id,
            fencing_revision=int(fencing_revision),
            terminal_status=terminal_status,
            run_id=run_id or str(row.get("run_id") or "").strip() or None,
        )
        if released is not None:
            self._update_task_status(
                task_id,
                status=str(terminal_status or "").strip().lower(),
                run_id=run_id,
            )
        return released

    def _task_row(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM openclaw_task_inbox WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def _reserve_task(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        existing, _should_submit = self._reserve_task_submission(message)
        return existing

    def _reserve_task_submission(
        self,
        message: Mapping[str, Any],
        *,
        task_lease: Any | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = _now()
        planned_run_id = _planned_run_id(self.host_id, message["task_id"])
        lease_id, lease_scope, lease_resource_id, lease_fencing_revision = (
            _lease_row_values(task_lease)
        )
        try:
            with self._transaction():
                self.conn.execute(
                    """
                    INSERT INTO openclaw_task_inbox (
                      task_id, message_id, delivery_id, run_id, status,
                      lease_id, lease_scope, lease_resource_id,
                      lease_fencing_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message["task_id"],
                        message["message_id"],
                        message["delivery_id"],
                        planned_run_id,
                        lease_id,
                        lease_scope,
                        lease_resource_id,
                        lease_fencing_revision,
                        now,
                        now,
                    ),
                )
                self._inflight_task_ids.add(message["task_id"])
        except sqlite3.IntegrityError:
            existing = self._task_row(message["task_id"])
            if existing is None:
                raise
            if _recoverable_task_row(existing):
                with self._lock:
                    if message["task_id"] in self._inflight_task_ids:
                        return existing, False
                    self._inflight_task_ids.add(message["task_id"])
                self._update_task_status(
                    message["task_id"],
                    status="processing",
                    run_id=str(existing.get("run_id") or "").strip()
                    or planned_run_id,
                )
                if task_lease is not None:
                    self._update_task_lease(message["task_id"], task_lease)
                refreshed = self._task_row(message["task_id"]) or existing
                return refreshed, True
            return existing, False
        return {
            "task_id": message["task_id"],
            "message_id": message["message_id"],
            "delivery_id": message["delivery_id"],
            "run_id": planned_run_id,
            "status": "processing",
            "lease_id": lease_id,
            "lease_scope": lease_scope,
            "lease_resource_id": lease_resource_id,
            "lease_fencing_revision": lease_fencing_revision,
            "created_at": now,
            "updated_at": now,
        }, True

    def _finalize_task(self, task_id: str, *, run_id: str, status: str) -> None:
        with self._transaction():
            self.conn.execute(
                """
                UPDATE openclaw_task_inbox
                   SET run_id = ?,
                       status = ?,
                       updated_at = ?
                 WHERE task_id = ?
                """,
                (run_id, status, _now(), task_id),
            )

    def _update_task_lease(self, task_id: str, task_lease: Any) -> None:
        lease_id, lease_scope, lease_resource_id, lease_fencing_revision = (
            _lease_row_values(task_lease)
        )
        with self._transaction():
            self.conn.execute(
                """
                UPDATE openclaw_task_inbox
                   SET lease_id = ?,
                       lease_scope = ?,
                       lease_resource_id = ?,
                       lease_fencing_revision = ?,
                       updated_at = ?
                 WHERE task_id = ?
                """,
                (
                    lease_id,
                    lease_scope,
                    lease_resource_id,
                    lease_fencing_revision,
                    _now(),
                    task_id,
                ),
            )

    def _acquire_task_lease(self, task_id: str, *, run_id: str) -> Any | None:
        if self.lease_store is None:
            return None
        try:
            return self.lease_store.acquire_lease(
                "task",
                task_id,
                owner_host_id=self.host_id,
                owner_run_id=run_id,
                ttl_seconds=self.lease_ttl_seconds,
            )
        except LeaseConflictError:
            return None

    def _update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        run_id: str | None = None,
    ) -> None:
        with self._transaction():
            if run_id:
                self.conn.execute(
                    """
                    UPDATE openclaw_task_inbox
                       SET status = ?,
                           run_id = ?,
                           updated_at = ?
                     WHERE task_id = ?
                    """,
                    (status, run_id, _now(), task_id),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE openclaw_task_inbox
                       SET status = ?,
                           updated_at = ?
                     WHERE task_id = ?
                    """,
                    (status, _now(), task_id),
                )

    def _publish_task_ack_once(
        self,
        message: Mapping[str, Any],
        *,
        run_id: str,
        status: str,
    ) -> bool:
        ack_key = (message["message_id"], message["delivery_id"])
        with self._lock:
            if ack_key in self._inflight_task_acks:
                return False
        ack_row = self._reserve_task_ack(message, status=status)
        if ack_row.get("ack_published_at"):
            return False
        ack_status = str(ack_row.get("status") or status)
        payload = {
            "kind": "openclaw.task_ack",
            "schema_version": 1,
            "host_id": self.host_id,
            "task_id": message["task_id"],
            "message_id": message["message_id"],
            "delivery_id": message["delivery_id"],
            "status": ack_status,
            "run_id": run_id,
        }
        subject = f"openclaw.task.{self.host_id}.ack"
        with self._lock:
            self._inflight_task_acks.add(ack_key)
        try:
            ack_published = _publish_or_enqueue(
                subject,
                payload,
                nats_client=self.nats_client,
                outbox=self.outbox,
            )
            if ack_published:
                self._mark_task_ack_published(
                    message["message_id"],
                    message["delivery_id"],
                )
            return ack_published
        finally:
            with self._lock:
                self._inflight_task_acks.discard(ack_key)

    def _reserve_task_ack(
        self,
        message: Mapping[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        now = _now()
        with self._transaction():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_task_ack_log (
                  message_id, delivery_id, task_id, status, ack_published_at,
                  created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (
                    message["message_id"],
                    message["delivery_id"],
                    message["task_id"],
                    status,
                    now,
                ),
            )
        row = self._task_ack_row(message["message_id"], message["delivery_id"])
        assert row is not None
        return row

    def _mark_task_ack_published(self, message_id: str, delivery_id: str) -> None:
        with self._transaction():
            self.conn.execute(
                """
                UPDATE openclaw_task_ack_log
                   SET ack_published_at = COALESCE(ack_published_at, ?)
                 WHERE message_id = ?
                   AND delivery_id = ?
                """,
                (_now(), message_id, delivery_id),
            )

    def _task_ack_row(self, message_id: str, delivery_id: str) -> dict[str, Any] | None:
        with self._lock:
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
        with self._lock:
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
        self._lock = threading.RLock()
        self._inflight_message_acks: set[tuple[str, str]] = set()
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
        ack_key = (message["message_id"], message["delivery_id"])
        with self._lock:
            if ack_key in self._inflight_message_acks:
                existing = self._message_ack_row(
                    message["message_id"],
                    message["delivery_id"],
                )
                status = "acked" if existing is None else existing["status"]
                return MessageInboxResult(
                    message_id=message["message_id"],
                    delivery_id=message["delivery_id"],
                    status=status,
                    duplicate=True,
                    ack_published=False,
                )
        ack_row, inserted = self._reserve_message_ack(
            message["message_id"],
            message["delivery_id"],
        )
        if ack_row.get("ack_published_at"):
            return MessageInboxResult(
                message_id=message["message_id"],
                delivery_id=message["delivery_id"],
                status=ack_row["status"],
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
        with self._lock:
            self._inflight_message_acks.add(ack_key)
        try:
            ack_published = _publish_or_enqueue(
                subject,
                payload,
                nats_client=self.nats_client,
                outbox=self.outbox,
            )
            if ack_published:
                self._mark_message_ack_published(
                    message["message_id"],
                    message["delivery_id"],
                )
        finally:
            with self._lock:
                self._inflight_message_acks.discard(ack_key)
        return MessageInboxResult(
            message_id=message["message_id"],
            delivery_id=message["delivery_id"],
            status="acked",
            duplicate=not inserted,
            ack_published=ack_published,
        )

    def _reserve_message_ack(
        self,
        message_id: str,
        delivery_id: str,
    ) -> tuple[dict[str, Any], bool]:
        now = _now()
        with self._transaction():
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_message_inbox_acks (
                  message_id, delivery_id, status, ack_published_at, created_at
                ) VALUES (?, ?, 'acked', NULL, ?)
                """,
                (
                    message_id,
                    delivery_id,
                    now,
                ),
            )
        row = self._message_ack_row(message_id, delivery_id)
        assert row is not None
        return row, cursor.rowcount == 1

    def _mark_message_ack_published(self, message_id: str, delivery_id: str) -> None:
        with self._transaction():
            self.conn.execute(
                """
                UPDATE openclaw_message_inbox_acks
                   SET ack_published_at = COALESCE(ack_published_at, ?)
                 WHERE message_id = ?
                   AND delivery_id = ?
                """,
                (_now(), message_id, delivery_id),
            )

    def _message_ack_row(
        self,
        message_id: str,
        delivery_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
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
        with self._lock:
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
    room_id = None
    if message_type != "command":
        room_id = _required_text(raw_message.get("room_id"), "room_id")
    return {
        "message_id": _required_text(raw_message.get("message_id"), "message_id"),
        "delivery_id": _required_text(raw_message.get("delivery_id"), "delivery_id"),
        "message_type": message_type,
        "command_ref": command_ref,
        "room_id": room_id,
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
        return True
    return False


def _configure_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")


def _ensure_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: Mapping[str, str],
) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for name, definition in columns.items():
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")


def _lease_row_values(task_lease: Any | None) -> tuple[str | None, str | None, str | None, int | None]:
    if task_lease is None:
        return None, None, None, None
    lease_id = str(getattr(task_lease, "lease_id", "") or "").strip() or None
    scope = str(getattr(task_lease, "scope", "") or "").strip() or None
    resource_id = str(getattr(task_lease, "resource_id", "") or "").strip() or None
    fencing_revision = getattr(task_lease, "fencing_revision", None)
    if fencing_revision is not None:
        fencing_revision = int(fencing_revision)
    return lease_id, scope, resource_id, fencing_revision


def _recoverable_task_row(row: Mapping[str, Any] | None) -> bool:
    if not row:
        return False
    status = str(row.get("status") or "").strip().lower()
    return status in {"processing", "failed", "recoverable"}


def _planned_run_id(host_id: str, task_id: str) -> str:
    digest = hashlib.sha256(f"{host_id}\0{task_id}".encode("utf-8")).hexdigest()
    return f"run-openclaw-{digest[:32]}"


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
