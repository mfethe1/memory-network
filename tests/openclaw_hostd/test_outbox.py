from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_index.openclaw_hostd.nats_client import NatsClient
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
