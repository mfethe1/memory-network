"""Persistent local event outbox for OpenClaw hostd."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator


@dataclass(frozen=True)
class OutboxEvent:
    sequence: int
    subject: str
    payload: dict[str, Any]
    created_at: str
    published_at: str | None


@dataclass(frozen=True)
class OutboxDrainResult:
    published_count: int
    failed_sequence: int | None = None
    error: str | None = None


class EventOutbox:
    """SQLite outbox with replay-safe monotonically increasing sequences."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.apply_schema()

    def close(self) -> None:
        self.conn.close()

    def apply_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS openclaw_event_outbox (
                  event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  subject TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  published_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_openclaw_event_outbox_pending
                  ON openclaw_event_outbox(published_at, event_sequence);
                """
            )
            self.conn.commit()

    def enqueue(self, subject: str, payload: Mapping[str, Any]) -> OutboxEvent:
        subject = _required_text(subject, "subject")
        if not isinstance(payload, Mapping):
            raise ValueError("outbox payload must be an object")
        created_at = _now()
        body = dict(payload)
        with self._transaction():
            cursor = self.conn.execute(
                """
                INSERT INTO openclaw_event_outbox (
                  subject, payload_json, created_at, published_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (subject, _dump(body), created_at),
            )
            sequence = int(cursor.lastrowid)
            body["event_sequence"] = sequence
            self.conn.execute(
                """
                UPDATE openclaw_event_outbox
                   SET payload_json = ?
                 WHERE event_sequence = ?
                """,
                (_dump(body), sequence),
            )
        return self.get_event(sequence)

    def get_event(self, sequence: int) -> OutboxEvent:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT * FROM openclaw_event_outbox
                 WHERE event_sequence = ?
                """,
                (int(sequence),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown outbox event_sequence: {sequence}")
        return _event(row)

    def pending_events(self, *, limit: int | None = None) -> list[OutboxEvent]:
        sql = """
            SELECT * FROM openclaw_event_outbox
             WHERE published_at IS NULL
             ORDER BY event_sequence
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        with self._lock:
            return [_event(row) for row in self.conn.execute(sql, params)]

    def drain(self, nats_client: Any, *, limit: int | None = None) -> OutboxDrainResult:
        published_count = 0
        for event in self.pending_events(limit=limit):
            try:
                nats_client.publish(event.subject, event.payload)
            except Exception as exc:
                return OutboxDrainResult(
                    published_count=published_count,
                    failed_sequence=event.sequence,
                    error=str(exc),
                )
            self.mark_published(event.sequence)
            published_count += 1
        return OutboxDrainResult(published_count=published_count)

    def mark_published(self, sequence: int) -> None:
        with self._transaction():
            self.conn.execute(
                """
                UPDATE openclaw_event_outbox
                   SET published_at = COALESCE(published_at, ?)
                 WHERE event_sequence = ?
                """,
                (_now(), int(sequence)),
            )

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


def _event(row: sqlite3.Row) -> OutboxEvent:
    return OutboxEvent(
        sequence=int(row["event_sequence"]),
        subject=str(row["subject"]),
        payload=_load(str(row["payload_json"])),
        created_at=str(row["created_at"]),
        published_at=row["published_at"],
    )


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _load(raw: str) -> dict[str, Any]:
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("outbox payload JSON must be an object")
    return payload


def _required_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
