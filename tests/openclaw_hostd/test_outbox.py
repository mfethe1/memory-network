from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from code_index.openclaw_hostd.nats_client import NatsClient
from code_index.openclaw_hostd.nats_client import NatsPyTransport
from code_index.openclaw_hostd.nats_client import NatsUnavailableError
from code_index.openclaw_hostd.outbox import EventOutbox


class FakeNatsTransport:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.fail_publish = False
        self.published: list[tuple[str, dict[str, Any]]] = []

    def connect(self) -> None:
        self.connected = True

    def publish(self, subject: str, payload: bytes) -> None:
        if self.fail_publish:
            raise RuntimeError("nats unavailable")
        self.published.append((subject, json.loads(payload.decode("utf-8"))))

    def close(self) -> None:
        self.closed = True
        self.connected = False


class KvTransportWithoutTtlSupport(FakeNatsTransport):
    def kv_put(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int | float | None = None,
    ) -> None:
        return None


class InspectingKvTransport(FakeNatsTransport):
    def __init__(self, *, bucket_ttl_seconds: int | float | None) -> None:
        super().__init__()
        self.bucket_ttl_seconds = bucket_ttl_seconds
        self.ensure_calls: list[tuple[str, int | float]] = []
        self.kv_entries: list[tuple[str, str, dict[str, Any], int | float | None]] = []

    def ensure_kv_bucket_ttl(
        self,
        bucket: str,
        *,
        ttl_seconds: int | float,
    ) -> None:
        self.ensure_calls.append((bucket, ttl_seconds))
        if self.bucket_ttl_seconds != ttl_seconds:
            raise NatsUnavailableError(
                f"KV bucket {bucket} TTL is {self.bucket_ttl_seconds}; "
                f"expected {ttl_seconds}"
            )

    def kv_put(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int | float | None = None,
    ) -> None:
        self.kv_entries.append(
            (bucket, key, json.loads(payload.decode("utf-8")), ttl_seconds)
        )


def test_outbox_persists_failed_events_and_drains_after_reconnect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outbox.db"
    outbox = EventOutbox(db_path)
    first = outbox.enqueue(
        "openclaw.run.host-a.run-1.events",
        {"kind": "openclaw.run_event", "run_id": "run-1"},
    )
    second = outbox.enqueue(
        "openclaw.run.host-a.run-1.status",
        {"kind": "openclaw.run_status", "run_id": "run-1"},
    )
    transport = FakeNatsTransport()
    client = NatsClient(transport=transport)
    client.connect()

    transport.fail_publish = True
    failed = outbox.drain(client)

    assert failed.published_count == 0
    assert failed.failed_sequence == first.sequence
    assert [event.sequence for event in outbox.pending_events()] == [
        first.sequence,
        second.sequence,
    ]

    outbox.close()
    reopened = EventOutbox(db_path)
    assert [event.sequence for event in reopened.pending_events()] == [
        first.sequence,
        second.sequence,
    ]

    transport.fail_publish = False
    drained = reopened.drain(client)

    assert drained.published_count == 2
    assert reopened.pending_events() == []
    assert [payload["event_sequence"] for _, payload in transport.published] == [
        first.sequence,
        second.sequence,
    ]
    assert first.sequence < second.sequence


def test_nats_client_requires_injected_transport_for_connect() -> None:
    with pytest.raises(NatsUnavailableError, match="inject a transport"):
        NatsClient().connect()


def test_nats_client_rejects_ttl_kv_write_without_explicit_ttl_support() -> None:
    client = NatsClient(transport=KvTransportWithoutTtlSupport())
    client.connect()

    with pytest.raises(NatsUnavailableError, match="TTL"):
        client.kv_put(
            "openclaw_agent_states",
            "host.run",
            {"run_id": "run"},
            ttl_seconds=30,
        )


def test_nats_client_accepts_ttl_kv_write_when_bucket_ttl_is_verified() -> None:
    transport = InspectingKvTransport(bucket_ttl_seconds=30)
    client = NatsClient(transport=transport)
    client.connect()

    client.kv_put(
        "openclaw_agent_states",
        "host.run",
        {"run_id": "run"},
        ttl_seconds=30,
    )

    assert transport.ensure_calls == [("openclaw_agent_states", 30)]
    assert transport.kv_entries == [
        ("openclaw_agent_states", "host.run", {"run_id": "run"}, 30)
    ]


def test_nats_client_rejects_ttl_kv_write_when_bucket_ttl_is_wrong() -> None:
    transport = InspectingKvTransport(bucket_ttl_seconds=None)
    client = NatsClient(transport=transport)
    client.connect()

    with pytest.raises(NatsUnavailableError, match="TTL"):
        client.kv_put(
            "openclaw_agent_states",
            "host.run",
            {"run_id": "run"},
            ttl_seconds=30,
        )

    assert transport.kv_entries == []


def test_nats_py_transport_does_not_advertise_per_entry_kv_ttl(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "nats", object())

    transport = NatsPyTransport("nats://127.0.0.1:4222")

    assert getattr(transport, "supports_kv_ttl", False) is False
    assert callable(getattr(transport, "ensure_kv_bucket_ttl"))
