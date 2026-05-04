"""SQLite-backed OpenClaw Messaging Service store."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from code_index.openclaw_messaging.models import (
    ADAPTER_TYPES,
    MESSAGE_TYPES,
    ROOM_KINDS,
    SENDER_KINDS,
    MessagingError,
    normalize_recipient_list,
    require_choice,
)


DEFAULT_SIGNATURE_KEY_ID = "local-dev"


class MessagingStore:
    """Durable room, message, delivery, adapter, and command-ref storage."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        signing_secret: str,
    ) -> None:
        self.db_path = db_path
        if not str(signing_secret or "").strip():
            raise MessagingError("signing_secret is required")
        self.signing_secret = signing_secret.encode("utf-8")
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.apply_schema()

    def close(self) -> None:
        self.conn.close()

    def apply_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS openclaw_rooms (
              room_id TEXT PRIMARY KEY,
              room_kind TEXT NOT NULL,
              display_name TEXT NOT NULL,
              repo_id TEXT,
              task_id TEXT,
              run_id TEXT,
              host_id TEXT,
              parent_room_id TEXT,
              notification_policy TEXT,
              created_at TEXT NOT NULL,
              archived_at TEXT,
              metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openclaw_messages (
              message_id TEXT PRIMARY KEY,
              room_id TEXT NOT NULL REFERENCES openclaw_rooms(room_id),
              sender_kind TEXT NOT NULL,
              sender_id TEXT NOT NULL,
              target_scope_json TEXT NOT NULL,
              message_type TEXT NOT NULL,
              body TEXT NOT NULL,
              context_handles_json TEXT NOT NULL,
              adapter_id TEXT,
              platform_ref_json TEXT NOT NULL,
              trace_id TEXT,
              correlation_id TEXT,
              parent_message_id TEXT,
              idempotency_key TEXT UNIQUE,
              created_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openclaw_message_deliveries (
              delivery_id TEXT PRIMARY KEY,
              message_id TEXT NOT NULL REFERENCES openclaw_messages(message_id),
              recipient_kind TEXT NOT NULL,
              recipient_id TEXT NOT NULL,
              delivery_key TEXT NOT NULL,
              delivery_status TEXT NOT NULL,
              nats_sequence INTEGER,
              delivered_at TEXT,
              acked_at TEXT,
              error TEXT,
              metadata_json TEXT NOT NULL,
              UNIQUE(message_id, delivery_key)
            );

            CREATE TABLE IF NOT EXISTS openclaw_command_refs (
              command_id TEXT PRIMARY KEY,
              message_id TEXT NOT NULL UNIQUE
                REFERENCES openclaw_messages(message_id),
              command_type TEXT NOT NULL,
              target_host_id TEXT,
              task_id TEXT,
              run_id TEXT,
              lease_id TEXT,
              signed_payload TEXT NOT NULL,
              signature_key_id TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openclaw_messaging_adapters (
              adapter_id TEXT PRIMARY KEY,
              adapter_type TEXT NOT NULL,
              display_name TEXT NOT NULL,
              status TEXT NOT NULL,
              capabilities_json TEXT NOT NULL,
              rate_limits_json TEXT NOT NULL,
              auth_key_id TEXT,
              last_seen_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              command_promotion_enabled INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS openclaw_platform_room_mappings (
              mapping_id TEXT PRIMARY KEY,
              adapter_id TEXT NOT NULL,
              platform_room_id TEXT NOT NULL,
              platform_thread_id TEXT,
              platform_thread_key TEXT NOT NULL DEFAULT '',
              room_id TEXT NOT NULL REFERENCES openclaw_rooms(room_id),
              sync_mode TEXT NOT NULL,
              route_policy_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              archived_at TEXT,
              metadata_json TEXT NOT NULL,
              UNIQUE(adapter_id, platform_room_id, platform_thread_key)
            );

            CREATE TABLE IF NOT EXISTS openclaw_external_identities (
              identity_link_id TEXT PRIMARY KEY,
              adapter_id TEXT NOT NULL,
              platform_user_id TEXT NOT NULL,
              openclaw_identity_id TEXT NOT NULL,
              display_name TEXT,
              scopes_json TEXT NOT NULL,
              verified_at TEXT,
              revoked_at TEXT,
              metadata_json TEXT NOT NULL,
              UNIQUE(adapter_id, platform_user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_openclaw_rooms_kind_task
              ON openclaw_rooms(room_kind, task_id);
            CREATE INDEX IF NOT EXISTS idx_openclaw_rooms_kind_run
              ON openclaw_rooms(room_kind, run_id);
            CREATE INDEX IF NOT EXISTS idx_openclaw_messages_room_created
              ON openclaw_messages(room_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_openclaw_deliveries_message
              ON openclaw_message_deliveries(message_id);
            CREATE INDEX IF NOT EXISTS idx_openclaw_platform_mappings_lookup
              ON openclaw_platform_room_mappings(
                adapter_id,
                platform_room_id,
                platform_thread_key
              );
            """
        )
        self.conn.commit()

    def create_room(
        self,
        *,
        room_kind: str,
        display_name: str,
        repo_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        host_id: str | None = None,
        parent_room_id: str | None = None,
        notification_policy: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        room_id: str | None = None,
    ) -> dict[str, Any]:
        kind = require_choice(room_kind, choices=ROOM_KINDS, field_name="room_kind")
        name = str(display_name or "").strip()
        if not name:
            raise MessagingError("display_name is required")
        now = _now()
        room_id = room_id or _new_id("room")
        with self._transaction():
            self.conn.execute(
                """
                INSERT INTO openclaw_rooms (
                  room_id, room_kind, display_name, repo_id, task_id, run_id,
                  host_id, parent_room_id, notification_policy, created_at,
                  archived_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    room_id,
                    kind,
                    name,
                    _clean(repo_id),
                    _clean(task_id),
                    _clean(run_id),
                    _clean(host_id),
                    _clean(parent_room_id),
                    _clean(notification_policy),
                    now,
                    _dump(dict(metadata or {})),
                ),
            )
        return self.get_room(room_id)

    def get_room(self, room_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM openclaw_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown room_id: {room_id}")
        return _room(row)

    def list_rooms(self) -> list[dict[str, Any]]:
        return [
            _room(row)
            for row in self.conn.execute(
                """
                SELECT * FROM openclaw_rooms
                 WHERE archived_at IS NULL
                 ORDER BY created_at, room_id
                """
            )
        ]

    def get_room_projection(self, room_id: str) -> dict[str, Any]:
        room = self.get_room(room_id)
        metadata = room["metadata"]
        participants = _metadata_participants(metadata)
        if room["room_kind"] == "task":
            participants.extend(_swarm_participants(metadata))
        return {"room": room, "participants": _dedupe_participants(participants)}

    def preview_target(self, target_scope: Mapping[str, Any]) -> dict[str, Any]:
        scope = _normalize_target_scope(target_scope)
        kind = scope["kind"]
        if kind == "host":
            recipients = [
                {"recipient_kind": "host", "recipient_id": _required(scope, "host_id")}
            ]
        elif kind == "run":
            recipients = [
                {"recipient_kind": "run", "recipient_id": _required(scope, "run_id")}
            ]
        else:
            room = self._room_for_target(scope)
            recipients = []
            if room is not None:
                metadata = room["metadata"]
                recipients.extend(_target_list(metadata.get("default_delivery_targets")))
                if kind == "task":
                    recipients.extend(_target_list(metadata.get("notification_targets")))
        return {
            "target_scope": scope,
            "recipients": normalize_recipient_list(recipients),
        }

    def create_message(
        self,
        *,
        room_id: str,
        sender_kind: str,
        sender_id: str,
        body: str,
        target_scope: Mapping[str, Any] | None = None,
        message_type: str = "chat",
        context_handles: list[dict[str, Any]] | None = None,
        adapter_id: str | None = None,
        platform_ref: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        correlation_id: str | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        recipients: list[dict[str, Any]] | None = None,
        command_type: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        existing = self._message_by_idempotency(idempotency_key)
        if existing is not None:
            return self._message_result(existing, created=False)

        room = self.get_room(room_id)
        sender_kind = require_choice(
            sender_kind,
            choices=SENDER_KINDS,
            field_name="sender_kind",
        )
        message_type = require_choice(
            message_type,
            choices=MESSAGE_TYPES,
            field_name="message_type",
        )
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            raise MessagingError("sender_id is required")
        body_text = str(body or "").strip()
        if not body_text:
            raise MessagingError("body is required")
        scope = (
            _normalize_target_scope(target_scope)
            if target_scope is not None
            else self._target_scope_for_room(room)
        )
        delivery_targets = (
            normalize_recipient_list(recipients)
            if recipients is not None
            else self.preview_target(scope)["recipients"]
        )
        now = _now()
        message_id = _new_id("msg")
        command: dict[str, Any] | None = None
        try:
            with self._transaction():
                self.conn.execute(
                    """
                    INSERT INTO openclaw_messages (
                      message_id, room_id, sender_kind, sender_id,
                      target_scope_json, message_type, body,
                      context_handles_json, adapter_id, platform_ref_json,
                      trace_id, correlation_id, parent_message_id,
                      idempotency_key, created_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        room_id,
                        sender_kind,
                        sender_id,
                        _dump(scope),
                        message_type,
                        body_text,
                        _dump(list(context_handles or [])),
                        _clean(adapter_id),
                        _dump(dict(platform_ref or {})),
                        _clean(trace_id),
                        _clean(correlation_id),
                        _clean(parent_message_id),
                        _clean(idempotency_key),
                        now,
                        _dump(dict(metadata or {})),
                    ),
                )
                if message_type == "command":
                    command = self._create_command_ref_locked(
                        message_id=message_id,
                        command_type=command_type or "run_message",
                        target_scope=scope,
                        sender_id=sender_id,
                        body=body_text,
                        created_at=now,
                        expires_at=expires_at,
                    )
                self._create_deliveries_locked(
                    message_id=message_id,
                    recipients=delivery_targets,
                    command_id=command["command_id"] if command else None,
                )
        except sqlite3.IntegrityError:
            existing = self._message_by_idempotency(idempotency_key)
            if existing is None:
                raise
            return self._message_result(existing, created=False)
        return self._message_result(self.get_message(message_id), created=True)

    def ingest_adapter_message(
        self,
        *,
        adapter_id: str,
        platform_user_id: str,
        room_id: str,
        body: str,
        message_type: str = "chat",
        command_type: str | None = None,
        platform_ref: Mapping[str, Any] | None = None,
        target_scope: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        parent_message_id: str | None = None,
        trace_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        adapter_id = str(adapter_id or "").strip()
        platform_user_id = str(platform_user_id or "").strip()
        if not adapter_id or not platform_user_id:
            raise MessagingError("adapter_id and platform_user_id are required")
        ref = dict(platform_ref or {})
        key = idempotency_key or adapter_idempotency_key(adapter_id, ref)
        existing = self._message_by_idempotency(key)
        if existing is not None:
            return self._message_result(existing, created=False)

        room = self.get_room(room_id)
        effective_target_scope = (
            _normalize_target_scope(target_scope)
            if target_scope is not None
            else self._target_scope_for_room(room)
        )
        identity = self.get_external_identity(adapter_id, platform_user_id)
        sender_id = (
            identity["openclaw_identity_id"]
            if identity is not None
            else f"{adapter_id}:{platform_user_id}"
        )
        requested_type = require_choice(
            message_type,
            choices=MESSAGE_TYPES,
            field_name="message_type",
        )
        allowed_command = requested_type == "command" and self.can_promote_adapter_command(
            adapter_id,
            platform_user_id,
            room_id=room_id,
            platform_ref=ref,
            command_type=command_type,
            target_scope=effective_target_scope,
        )
        effective_type = "command" if allowed_command else (
            "chat" if requested_type == "command" else requested_type
        )
        effective_metadata = dict(metadata or {})
        recipients: list[dict[str, Any]] | None = None
        if requested_type == "command" and not allowed_command:
            effective_metadata.update(
                {
                    "command_promotion": "blocked",
                    "requested_message_type": "command",
                    "requested_command_type": command_type,
                }
            )
            recipients = []

        return self.create_message(
            room_id=room_id,
            sender_kind="human",
            sender_id=sender_id,
            body=body,
            target_scope=effective_target_scope,
            message_type=effective_type,
            adapter_id=adapter_id,
            platform_ref=ref,
            trace_id=trace_id,
            correlation_id=correlation_id,
            parent_message_id=parent_message_id,
            idempotency_key=key,
            metadata=effective_metadata,
            recipients=recipients,
            command_type=command_type,
        )

    def get_message(self, message_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM openclaw_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown message_id: {message_id}")
        return _message(row)

    def list_messages(self, room_id: str) -> list[dict[str, Any]]:
        return [
            _message(row)
            for row in self.conn.execute(
                """
                SELECT * FROM openclaw_messages
                 WHERE room_id = ?
                 ORDER BY created_at, message_id
                """,
                (room_id,),
            )
        ]

    def list_message_events(self) -> list[dict[str, Any]]:
        messages = [
            _message(row)
            for row in self.conn.execute(
                "SELECT * FROM openclaw_messages ORDER BY created_at, message_id"
            )
        ]
        return [
            {
                "message": message,
                "deliveries": self.list_deliveries(message["message_id"]),
            }
            for message in messages
        ]

    def list_deliveries(self, message_id: str) -> list[dict[str, Any]]:
        return [
            _delivery(row)
            for row in self.conn.execute(
                """
                SELECT * FROM openclaw_message_deliveries
                 WHERE message_id = ?
                 ORDER BY recipient_kind, recipient_id, delivery_key
                """,
                (message_id,),
            )
        ]

    def ack_delivery(
        self,
        *,
        message_id: str,
        recipient_kind: str | None = None,
        recipient_id: str | None = None,
        delivery_id: str | None = None,
        delivery_key: str | None = None,
        status: str = "acked",
        error: str | None = None,
    ) -> dict[str, Any]:
        status = str(status or "acked").strip().lower()
        if status not in {"delivered", "acked", "failed", "expired"}:
            raise MessagingError("ack status must be delivered, acked, failed, or expired")
        existing = self._delivery_for_ack(
            message_id=message_id,
            recipient_kind=recipient_kind,
            recipient_id=recipient_id,
            delivery_id=delivery_id,
            delivery_key=delivery_key,
        )
        now = _now()
        delivered_at = existing["delivered_at"]
        acked_at = existing["acked_at"]
        current_status = str(existing["delivery_status"] or "queued")
        if _delivery_status_rank(status) < _delivery_status_rank(current_status):
            return existing
        if status in {"delivered", "acked"} and not delivered_at:
            delivered_at = now
        if status == "acked":
            acked_at = now
        with self._transaction():
            self.conn.execute(
                """
                UPDATE openclaw_message_deliveries
                   SET delivery_status = ?,
                       delivered_at = ?,
                       acked_at = ?,
                       error = ?
                 WHERE delivery_id = ?
                """,
                (status, delivered_at, acked_at, _clean(error), existing["delivery_id"]),
            )
        return self._get_delivery(existing["delivery_id"])

    def get_command_ref_for_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM openclaw_command_refs WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return _command(row) if row else None

    def list_command_refs(self) -> list[dict[str, Any]]:
        return [
            _command(row)
            for row in self.conn.execute(
                "SELECT * FROM openclaw_command_refs ORDER BY created_at, command_id"
            )
        ]

    def verify_command_ref(self, command_ref: Mapping[str, Any]) -> bool:
        try:
            command_id = str(command_ref["command_id"])
            message_id = str(command_ref["message_id"])
            signed = json.loads(str(command_ref["signed_payload"]))
            payload = signed["payload"]
            signature = str(signed["signature"])
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, Mapping):
            return False
        expected = _sign_payload(self.signing_secret, payload)
        if not hmac.compare_digest(signature, expected):
            return False
        row = self.conn.execute(
            "SELECT * FROM openclaw_command_refs WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return False
        stored = _command(row)
        if stored["message_id"] != message_id:
            return False
        for key in (
            "command_id",
            "message_id",
            "command_type",
            "signed_payload",
            "signature_key_id",
            "expires_at",
            "status",
        ):
            if str(command_ref.get(key)) != str(stored.get(key)):
                return False
        if stored["signature_key_id"] != DEFAULT_SIGNATURE_KEY_ID:
            return False
        if stored["status"] not in {"pending", "active"}:
            return False
        if str(payload.get("command_id")) != stored["command_id"]:
            return False
        if str(payload.get("message_id")) != stored["message_id"]:
            return False
        if str(payload.get("command_type")) != stored["command_type"]:
            return False
        if str(payload.get("expires_at")) != stored["expires_at"]:
            return False
        expires_at = _parse_datetime(stored["expires_at"])
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return False
        message = self.get_message(stored["message_id"])
        if str(payload.get("sender_id")) != message["sender_id"]:
            return False
        expected_body_hash = hashlib.sha256(
            message["body"].encode("utf-8")
        ).hexdigest()
        if str(payload.get("body_sha256")) != expected_body_hash:
            return False
        target_scope = payload.get("target_scope")
        if not isinstance(target_scope, Mapping):
            return False
        if _clean(target_scope.get("host_id")) != stored["target_host_id"]:
            return False
        if _clean(target_scope.get("task_id")) != stored["task_id"]:
            return False
        if _clean(target_scope.get("run_id")) != stored["run_id"]:
            return False
        return True

    def register_adapter(
        self,
        *,
        adapter_id: str,
        adapter_type: str,
        display_name: str,
        status: str = "active",
        capabilities: Mapping[str, Any] | None = None,
        rate_limits: Mapping[str, Any] | None = None,
        auth_key_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        command_promotion_enabled: bool = False,
    ) -> dict[str, Any]:
        adapter_id = str(adapter_id or "").strip()
        if not adapter_id:
            raise MessagingError("adapter_id is required")
        adapter_type = require_choice(
            adapter_type,
            choices=ADAPTER_TYPES,
            field_name="adapter_type",
        )
        now = _now()
        with self._transaction():
            self.conn.execute(
                """
                INSERT INTO openclaw_messaging_adapters (
                  adapter_id, adapter_type, display_name, status,
                  capabilities_json, rate_limits_json, auth_key_id,
                  last_seen_at, created_at, updated_at, metadata_json,
                  command_promotion_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                ON CONFLICT(adapter_id) DO UPDATE SET
                  adapter_type = excluded.adapter_type,
                  display_name = excluded.display_name,
                  status = excluded.status,
                  capabilities_json = excluded.capabilities_json,
                  rate_limits_json = excluded.rate_limits_json,
                  auth_key_id = excluded.auth_key_id,
                  updated_at = excluded.updated_at,
                  metadata_json = excluded.metadata_json,
                  command_promotion_enabled = excluded.command_promotion_enabled
                """,
                (
                    adapter_id,
                    adapter_type,
                    str(display_name or adapter_id),
                    status,
                    _dump(dict(capabilities or {})),
                    _dump(dict(rate_limits or {})),
                    _clean(auth_key_id),
                    now,
                    now,
                    _dump(dict(metadata or {})),
                    1 if command_promotion_enabled else 0,
                ),
            )
        return self.get_adapter(adapter_id)

    def get_adapter(self, adapter_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM openclaw_messaging_adapters WHERE adapter_id = ?",
            (adapter_id,),
        ).fetchone()
        return _adapter(row) if row else None

    def list_adapters(self) -> list[dict[str, Any]]:
        return [
            _adapter(row)
            for row in self.conn.execute(
                "SELECT * FROM openclaw_messaging_adapters ORDER BY adapter_id"
            )
        ]

    def set_adapter_command_promotion(self, adapter_id: str, *, enabled: bool) -> None:
        with self._transaction():
            cursor = self.conn.execute(
                """
                UPDATE openclaw_messaging_adapters
                   SET command_promotion_enabled = ?,
                       updated_at = ?
                 WHERE adapter_id = ?
                """,
                (1 if enabled else 0, _now(), adapter_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"unknown adapter_id: {adapter_id}")

    def link_external_identity(
        self,
        *,
        adapter_id: str,
        platform_user_id: str,
        openclaw_identity_id: str,
        scopes: Iterable[str] = (),
        display_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        adapter_id = str(adapter_id or "").strip()
        platform_user_id = str(platform_user_id or "").strip()
        openclaw_identity_id = str(openclaw_identity_id or "").strip()
        if not adapter_id or not platform_user_id or not openclaw_identity_id:
            raise MessagingError(
                "adapter_id, platform_user_id, and openclaw_identity_id are required"
            )
        now = _now()
        with self._transaction():
            self.conn.execute(
                """
                INSERT INTO openclaw_external_identities (
                  identity_link_id, adapter_id, platform_user_id,
                  openclaw_identity_id, display_name, scopes_json, verified_at,
                  revoked_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(adapter_id, platform_user_id) DO UPDATE SET
                  openclaw_identity_id = excluded.openclaw_identity_id,
                  display_name = excluded.display_name,
                  scopes_json = excluded.scopes_json,
                  verified_at = excluded.verified_at,
                  revoked_at = NULL,
                  metadata_json = excluded.metadata_json
                """,
                (
                    _new_id("identity"),
                    adapter_id,
                    platform_user_id,
                    openclaw_identity_id,
                    _clean(display_name),
                    _dump(_scope_list(scopes)),
                    now,
                    _dump(dict(metadata or {})),
                ),
            )
        identity = self.get_external_identity(adapter_id, platform_user_id)
        if identity is None:
            raise RuntimeError("identity link was not persisted")
        return identity

    def get_external_identity(
        self,
        adapter_id: str,
        platform_user_id: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM openclaw_external_identities
             WHERE adapter_id = ?
               AND platform_user_id = ?
               AND verified_at IS NOT NULL
               AND revoked_at IS NULL
            """,
            (adapter_id, platform_user_id),
        ).fetchone()
        return _identity(row) if row else None

    def can_promote_adapter_command(
        self,
        adapter_id: str,
        platform_user_id: str,
        *,
        room_id: str | None = None,
        platform_ref: Mapping[str, Any] | None = None,
        command_type: str | None = None,
        target_scope: Mapping[str, Any] | None = None,
    ) -> bool:
        adapter = self.get_adapter(adapter_id)
        if not adapter or not adapter["command_promotion_enabled"]:
            return False
        identity = self.get_external_identity(adapter_id, platform_user_id)
        if not identity:
            return False
        if "command:write" not in set(identity["scopes"]):
            return False
        ref = dict(platform_ref or {})
        platform_room_id = _clean(
            ref.get("platform_room_id") or ref.get("chat_id") or ref.get("room_id")
        )
        if not room_id or not platform_room_id:
            return False
        mapping = self.find_platform_room_mapping(
            adapter_id=adapter_id,
            platform_room_id=platform_room_id,
            platform_thread_id=_clean(
                ref.get("platform_thread_id") or ref.get("thread_id")
            ),
        )
        if mapping is None or mapping["room_id"] != room_id:
            return False
        return _route_policy_allows_command(
            mapping["route_policy"],
            command_type=command_type,
            target_scope=target_scope,
        )

    def map_platform_room(
        self,
        *,
        adapter_id: str,
        platform_room_id: str,
        room_id: str,
        platform_thread_id: str | None = None,
        sync_mode: str = "bidirectional",
        route_policy: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_room(room_id)
        now = _now()
        thread_key = _thread_key(platform_thread_id)
        existing = self._exact_platform_room_mapping(
            adapter_id=adapter_id,
            platform_room_id=str(platform_room_id),
            platform_thread_id=platform_thread_id,
        )
        with self._transaction():
            if existing is not None:
                self.conn.execute(
                    """
                    UPDATE openclaw_platform_room_mappings
                       SET room_id = ?,
                           sync_mode = ?,
                           route_policy_json = ?,
                           archived_at = NULL,
                           metadata_json = ?
                     WHERE mapping_id = ?
                    """,
                    (
                        room_id,
                        sync_mode,
                        _dump(dict(route_policy or {})),
                        _dump(dict(metadata or {})),
                        existing["mapping_id"],
                    ),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO openclaw_platform_room_mappings (
                      mapping_id, adapter_id, platform_room_id,
                      platform_thread_id, platform_thread_key, room_id,
                      sync_mode, route_policy_json, created_at, archived_at,
                      metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        _new_id("mapping"),
                        adapter_id,
                        str(platform_room_id),
                        _clean(platform_thread_id),
                        thread_key,
                        room_id,
                        sync_mode,
                        _dump(dict(route_policy or {})),
                        now,
                        _dump(dict(metadata or {})),
                    ),
                )
        mapping = self.find_platform_room_mapping(
            adapter_id=adapter_id,
            platform_room_id=str(platform_room_id),
            platform_thread_id=platform_thread_id,
        )
        if mapping is None:
            raise RuntimeError("platform room mapping was not persisted")
        return mapping

    def find_platform_room_mapping(
        self,
        *,
        adapter_id: str,
        platform_room_id: str,
        platform_thread_id: str | None = None,
    ) -> dict[str, Any] | None:
        thread = _clean(platform_thread_id)
        thread_key = _thread_key(platform_thread_id)
        row = None
        if thread is not None:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_platform_room_mappings
                 WHERE adapter_id = ?
                   AND platform_room_id = ?
                   AND platform_thread_key = ?
                   AND archived_at IS NULL
                """,
                (adapter_id, platform_room_id, thread_key),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_platform_room_mappings
                 WHERE adapter_id = ?
                   AND platform_room_id = ?
                   AND platform_thread_key = ''
                   AND archived_at IS NULL
                """,
                (adapter_id, platform_room_id),
            ).fetchone()
        return _mapping(row) if row else None

    def list_platform_room_mappings(self) -> list[dict[str, Any]]:
        return [
            _mapping(row)
            for row in self.conn.execute(
                """
                SELECT * FROM openclaw_platform_room_mappings
                 WHERE archived_at IS NULL
                 ORDER BY adapter_id, platform_room_id, platform_thread_key
                """
            )
        ]

    def _exact_platform_room_mapping(
        self,
        *,
        adapter_id: str,
        platform_room_id: str,
        platform_thread_id: str | None = None,
    ) -> dict[str, Any] | None:
        thread = _clean(platform_thread_id)
        thread_key = _thread_key(platform_thread_id)
        if thread is None:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_platform_room_mappings
                 WHERE adapter_id = ?
                   AND platform_room_id = ?
                   AND platform_thread_key = ''
                   AND archived_at IS NULL
                """,
                (adapter_id, platform_room_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_platform_room_mappings
                 WHERE adapter_id = ?
                   AND platform_room_id = ?
                   AND platform_thread_key = ?
                   AND archived_at IS NULL
                """,
                (adapter_id, platform_room_id, thread_key),
            ).fetchone()
        return _mapping(row) if row else None

    def _message_result(
        self,
        message: dict[str, Any],
        *,
        created: bool,
    ) -> dict[str, Any]:
        return {
            "created": created,
            "message": message,
            "deliveries": self.list_deliveries(message["message_id"]),
            "command_ref": self.get_command_ref_for_message(message["message_id"]),
        }

    def _message_by_idempotency(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        row = self.conn.execute(
            "SELECT * FROM openclaw_messages WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return _message(row) if row else None

    def _room_for_target(self, scope: Mapping[str, Any]) -> dict[str, Any] | None:
        kind = scope["kind"]
        if kind == "fleet":
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_rooms
                 WHERE room_kind = 'fleet'
                   AND archived_at IS NULL
                 ORDER BY created_at
                 LIMIT 1
                """
            ).fetchone()
        elif kind == "task":
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_rooms
                 WHERE room_kind = 'task'
                   AND task_id = ?
                   AND archived_at IS NULL
                 ORDER BY created_at
                 LIMIT 1
                """,
                (_required(scope, "task_id"),),
            ).fetchone()
        elif kind == "swarm":
            task_id = _clean(scope.get("task_id"))
            if task_id:
                row = self.conn.execute(
                    """
                    SELECT * FROM openclaw_rooms
                     WHERE room_kind = 'swarm'
                       AND task_id = ?
                       AND archived_at IS NULL
                     ORDER BY created_at
                     LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
            else:
                row = self.conn.execute(
                    """
                    SELECT * FROM openclaw_rooms
                     WHERE room_kind = 'swarm'
                       AND archived_at IS NULL
                     ORDER BY created_at
                     LIMIT 1
                    """
                ).fetchone()
        else:
            row = None
        return _room(row) if row else None

    def _target_scope_for_room(self, room: Mapping[str, Any]) -> dict[str, Any]:
        kind = room["room_kind"]
        if kind == "fleet":
            return {"kind": "fleet"}
        if kind == "repo":
            return {"kind": "repo", "repo_id": room.get("repo_id")}
        if kind == "task":
            return {"kind": "task", "task_id": room.get("task_id")}
        if kind == "run":
            return {"kind": "run", "run_id": room.get("run_id")}
        if kind == "host":
            return {"kind": "host", "host_id": room.get("host_id")}
        if kind == "swarm":
            scope = {"kind": "swarm"}
            if room.get("task_id"):
                scope["task_id"] = room.get("task_id")
            return scope
        return {"kind": kind}

    def _create_command_ref_locked(
        self,
        *,
        message_id: str,
        command_type: str,
        target_scope: Mapping[str, Any],
        sender_id: str,
        body: str,
        created_at: str,
        expires_at: str | None,
    ) -> dict[str, Any]:
        command_id = _new_id("cmd")
        expires_at = expires_at or (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).isoformat(timespec="milliseconds")
        payload = {
            "command_id": command_id,
            "message_id": message_id,
            "command_type": command_type,
            "target_scope": dict(target_scope),
            "sender_id": sender_id,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "created_at": created_at,
            "expires_at": expires_at,
        }
        signed_payload = {
            "alg": "HMAC-SHA256",
            "payload": payload,
            "signature": _sign_payload(self.signing_secret, payload),
        }
        self.conn.execute(
            """
            INSERT INTO openclaw_command_refs (
              command_id, message_id, command_type, target_host_id, task_id,
              run_id, lease_id, signed_payload, signature_key_id, expires_at,
              status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'pending', ?)
            """,
            (
                command_id,
                message_id,
                command_type,
                _clean(target_scope.get("host_id")),
                _clean(target_scope.get("task_id")),
                _clean(target_scope.get("run_id")),
                _dump(signed_payload),
                DEFAULT_SIGNATURE_KEY_ID,
                expires_at,
                created_at,
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM openclaw_command_refs WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        return _command(row)

    def _create_deliveries_locked(
        self,
        *,
        message_id: str,
        recipients: list[dict[str, Any]],
        command_id: str | None,
    ) -> None:
        for recipient in normalize_recipient_list(recipients):
            metadata = {
                key: value
                for key, value in recipient.items()
                if key not in {"recipient_kind", "recipient_id"}
            }
            delivery_key = _delivery_key(recipient)
            if command_id and recipient["recipient_kind"] in {
                "host",
                "run",
                "agent",
                "controller",
            }:
                metadata["command_id"] = command_id
            self.conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_message_deliveries (
                  delivery_id, message_id, recipient_kind, recipient_id,
                  delivery_key, delivery_status, nats_sequence, delivered_at,
                  acked_at, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, NULL, ?)
                """,
                (
                    _new_id("delivery"),
                    message_id,
                    recipient["recipient_kind"],
                    recipient["recipient_id"],
                    delivery_key,
                    _dump(metadata),
                ),
            )

    def _delivery_for_ack(
        self,
        *,
        message_id: str,
        recipient_kind: str | None,
        recipient_id: str | None,
        delivery_id: str | None,
        delivery_key: str | None,
    ) -> dict[str, Any]:
        row = None
        if delivery_id:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_message_deliveries
                 WHERE delivery_id = ?
                   AND message_id = ?
                """,
                (delivery_id, message_id),
            ).fetchone()
        elif delivery_key:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_message_deliveries
                 WHERE message_id = ?
                   AND delivery_key = ?
                """,
                (message_id, delivery_key),
            ).fetchone()
        elif recipient_kind and recipient_id:
            rows = self.conn.execute(
                """
                SELECT * FROM openclaw_message_deliveries
                 WHERE message_id = ?
                   AND recipient_kind = ?
                   AND recipient_id = ?
                 ORDER BY delivery_key
                """,
                (message_id, recipient_kind, recipient_id),
            ).fetchall()
            if len(rows) > 1:
                raise MessagingError(
                    "ambiguous delivery acknowledgement; use delivery_id or delivery_key"
                )
            row = rows[0] if rows else None
        if row is None:
            raise KeyError(f"delivery not found for message_id: {message_id}")
        return _delivery(row)

    def _get_delivery(self, delivery_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM openclaw_message_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown delivery_id: {delivery_id}")
        return _delivery(row)

    @contextmanager
    def _transaction(self):
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()


def adapter_idempotency_key(adapter_id: str, platform_ref: Mapping[str, Any]) -> str:
    room_id = str(
        platform_ref.get("platform_room_id")
        or platform_ref.get("chat_id")
        or platform_ref.get("room_id")
        or ""
    )
    thread_id = str(
        platform_ref.get("platform_thread_id")
        or platform_ref.get("thread_id")
        or ""
    )
    event_id = str(
        platform_ref.get("platform_event_id")
        or platform_ref.get("event_id")
        or platform_ref.get("platform_message_id")
        or platform_ref.get("message_id")
        or ""
    )
    if not adapter_id or not room_id or not event_id:
        raise MessagingError(
            "adapter idempotency requires adapter_id, platform room id, and event id"
        )
    return f"{adapter_id}:{room_id}:{thread_id}:{event_id}"


def _room(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "room_id": row["room_id"],
        "room_kind": row["room_kind"],
        "display_name": row["display_name"],
        "repo_id": row["repo_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "host_id": row["host_id"],
        "parent_room_id": row["parent_room_id"],
        "notification_policy": row["notification_policy"],
        "created_at": row["created_at"],
        "archived_at": row["archived_at"],
        "metadata": _load(row["metadata_json"], {}),
    }


def _message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "room_id": row["room_id"],
        "sender_kind": row["sender_kind"],
        "sender_id": row["sender_id"],
        "target_scope": _load(row["target_scope_json"], {}),
        "message_type": row["message_type"],
        "body": row["body"],
        "context_handles": _load(row["context_handles_json"], []),
        "adapter_id": row["adapter_id"],
        "platform_ref": _load(row["platform_ref_json"], {}),
        "trace_id": row["trace_id"],
        "correlation_id": row["correlation_id"],
        "parent_message_id": row["parent_message_id"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "metadata": _load(row["metadata_json"], {}),
    }


def _delivery(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "delivery_id": row["delivery_id"],
        "message_id": row["message_id"],
        "recipient_kind": row["recipient_kind"],
        "recipient_id": row["recipient_id"],
        "delivery_key": row["delivery_key"],
        "delivery_status": row["delivery_status"],
        "nats_sequence": row["nats_sequence"],
        "delivered_at": row["delivered_at"],
        "acked_at": row["acked_at"],
        "error": row["error"],
        "metadata": _load(row["metadata_json"], {}),
    }


def _command(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "command_id": row["command_id"],
        "message_id": row["message_id"],
        "command_type": row["command_type"],
        "target_host_id": row["target_host_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "lease_id": row["lease_id"],
        "signed_payload": row["signed_payload"],
        "signature_key_id": row["signature_key_id"],
        "expires_at": row["expires_at"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _adapter(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "adapter_id": row["adapter_id"],
        "adapter_type": row["adapter_type"],
        "display_name": row["display_name"],
        "status": row["status"],
        "capabilities": _load(row["capabilities_json"], {}),
        "rate_limits": _load(row["rate_limits_json"], {}),
        "auth_key_id": row["auth_key_id"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "metadata": _load(row["metadata_json"], {}),
        "command_promotion_enabled": bool(row["command_promotion_enabled"]),
    }


def _mapping(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "mapping_id": row["mapping_id"],
        "adapter_id": row["adapter_id"],
        "platform_room_id": row["platform_room_id"],
        "platform_thread_id": row["platform_thread_id"],
        "platform_thread_key": row["platform_thread_key"],
        "room_id": row["room_id"],
        "sync_mode": row["sync_mode"],
        "route_policy": _load(row["route_policy_json"], {}),
        "created_at": row["created_at"],
        "archived_at": row["archived_at"],
        "metadata": _load(row["metadata_json"], {}),
    }


def _identity(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "identity_link_id": row["identity_link_id"],
        "adapter_id": row["adapter_id"],
        "platform_user_id": row["platform_user_id"],
        "openclaw_identity_id": row["openclaw_identity_id"],
        "display_name": row["display_name"],
        "scopes": _load(row["scopes_json"], []),
        "verified_at": row["verified_at"],
        "revoked_at": row["revoked_at"],
        "metadata": _load(row["metadata_json"], {}),
    }


def _metadata_participants(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in metadata.get("participants", [])
        if isinstance(item, Mapping)
        and item.get("participant_kind")
        and item.get("participant_id")
    ]


def _swarm_participants(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    swarm = metadata.get("swarm") if isinstance(metadata.get("swarm"), Mapping) else {}
    participants: list[dict[str, Any]] = []
    lead = swarm.get("lead_run") if isinstance(swarm.get("lead_run"), Mapping) else {}
    if lead.get("run_id"):
        participants.append(
            {
                "participant_kind": "run",
                "participant_id": str(lead["run_id"]),
                "display_name": str(lead.get("agent_name") or "Swarm Lead"),
                "role": "swarm_lead",
                "title": "Swarm Lead",
            }
        )
    children = swarm.get("child_runs") if isinstance(swarm.get("child_runs"), list) else []
    for child in children:
        if not isinstance(child, Mapping) or not child.get("run_id"):
            continue
        role = str(child.get("role") or "agent").strip().lower()
        participants.append(
            {
                "participant_kind": "run",
                "participant_id": str(child["run_id"]),
                "display_name": str(child.get("agent_name") or child["run_id"]),
                "role": role,
                "title": str(child.get("title") or role.replace("_", " ").title()),
            }
        )
    return participants


def _dedupe_participants(participants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for participant in participants:
        key = (
            str(participant.get("participant_kind") or ""),
            str(participant.get("participant_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(participant)
    return out


def _target_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _normalize_target_scope(target_scope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(target_scope, Mapping):
        raise MessagingError("target_scope must be an object")
    kind = str(target_scope.get("kind") or "").strip().lower()
    if kind not in {"fleet", "repo", "task", "swarm", "run", "host"}:
        raise MessagingError("target_scope.kind must be fleet, repo, task, swarm, run, or host")
    out = {"kind": kind}
    for key in ("repo_id", "task_id", "run_id", "host_id"):
        value = _clean(target_scope.get(key))
        if value is not None:
            out[key] = value
    if kind == "repo":
        _required(out, "repo_id")
    if kind == "task":
        _required(out, "task_id")
    if kind == "run":
        _required(out, "run_id")
    if kind == "host":
        _required(out, "host_id")
    return out


def _required(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise MessagingError(f"{key} is required")
    return value


def _scope_list(scopes: Iterable[str]) -> list[str]:
    out: list[str] = []
    for scope in scopes:
        text = str(scope or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _delivery_status_rank(status: str) -> int:
    return {
        "queued": 0,
        "delivered": 1,
        "acked": 2,
        "failed": 3,
        "expired": 3,
    }.get(str(status or "").strip().lower(), 0)


def _delivery_key(recipient: Mapping[str, Any]) -> str:
    target = {
        "recipient_kind": recipient.get("recipient_kind"),
        "recipient_id": recipient.get("recipient_id"),
        "platform_room_id": recipient.get("platform_room_id"),
        "platform_thread_id": recipient.get("platform_thread_id"),
        "platform_message_id": recipient.get("platform_message_id"),
        "webhook_url": recipient.get("webhook_url"),
        "email": recipient.get("email"),
    }
    target = {key: value for key, value in target.items() if value is not None}
    return hashlib.sha256(_dump(target).encode("utf-8")).hexdigest()


def _thread_key(platform_thread_id: Any) -> str:
    return _clean(platform_thread_id) or ""


def _route_policy_allows_command(
    route_policy: Mapping[str, Any],
    *,
    command_type: str | None,
    target_scope: Mapping[str, Any] | None,
) -> bool:
    policy = (
        route_policy.get("command_promotion")
        if isinstance(route_policy.get("command_promotion"), Mapping)
        else {}
    )
    if not policy.get("enabled"):
        return False
    allowed_command_types = _string_set(policy.get("allowed_command_types"))
    command = str(command_type or "").strip()
    if allowed_command_types and command not in allowed_command_types:
        return False
    target_kind = ""
    if isinstance(target_scope, Mapping):
        target_kind = str(target_scope.get("kind") or "").strip().lower()
    allowed_target_kinds = _string_set(policy.get("allowed_target_kinds"))
    if allowed_target_kinds and target_kind not in allowed_target_kinds:
        return False
    return True


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sign_payload(secret: bytes, payload: Mapping[str, Any]) -> str:
    body = _dump(dict(payload)).encode("utf-8")
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return default


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
