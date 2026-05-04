from __future__ import annotations

import json
import re
from typing import Any

from code_index.openclaw_hostd.logging import REDACTED
from code_index.openclaw_hostd.telemetry import provider_run_span
from code_index.openclaw_hostd.telemetry import trace_event_payload
from code_index.openclaw_hostd.telemetry import trace_lease_payload
from code_index.openclaw_hostd.telemetry import trace_memory_sync_payload
from code_index.openclaw_hostd.telemetry import trace_run_payload
from code_index.openclaw_hostd.telemetry import trace_task_payload


TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class CapturingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def info(self, message: str, *args: object, **kwargs: Any) -> None:
        del args
        self.records.append((message, dict(kwargs.get("extra") or {})))


def test_trace_id_propagates_across_task_lease_run_event_and_memory_sync_payloads() -> None:
    raw_task = {
        "kind": "openclaw.task.assigned",
        "task_id": "task-123",
        "host_id": "host-a",
    }

    task = trace_task_payload(raw_task)
    lease = trace_lease_payload(
        {
            "kind": "openclaw.lease.acquired",
            "scope": "task",
            "resource_id": "task-123",
        },
        source=task,
    )
    run = trace_run_payload(
        {"kind": "openclaw.run.started", "task_id": "task-123", "run_id": "run-123"},
        source=lease,
    )
    event = trace_event_payload(
        {
            "kind": "openclaw.run_event",
            "task_id": "task-123",
            "run_id": "run-123",
            "event_type": "provider_output",
        },
        source=run,
    )
    memory = trace_memory_sync_payload(
        {
            "kind": "openclaw.memory_sync",
            "task_id": "task-123",
            "run_id": "run-123",
            "summary_ref": "fumemory://summary/task-123",
        },
        source=event,
    )

    assert TRACE_ID_RE.match(task["trace_id"])
    assert lease["trace_id"] == task["trace_id"]
    assert run["trace_id"] == task["trace_id"]
    assert event["trace_id"] == task["trace_id"]
    assert memory["trace_id"] == task["trace_id"]
    assert "trace_id" not in raw_task


def test_provider_run_span_falls_back_to_structured_redacted_log_records() -> None:
    logger = CapturingLogger()

    with provider_run_span(
        {
            "trace_id": "trace-provider-1",
            "task_id": "task-123",
            "run_id": "run-123",
            "provider": "claude",
            "provider_api_key": "sk-provider-secret",
            "headers": {
                "authorization": "Bearer provider-secret",
                "x-api-key": "sk-provider-secret-2",
            },
            "callback_url": (
                "https://user:provider-secret@example.invalid/run"
                "?token=provider-secret"
            ),
        },
        logger=logger,
        prefer_otel=False,
    ):
        pass

    assert [message for message, _extra in logger.records] == [
        "openclaw.telemetry.span.start",
        "openclaw.telemetry.span.end",
    ]
    start = logger.records[0][1]["openclaw"]
    assert start["kind"] == "openclaw.telemetry.span"
    assert start["span_name"] == "openclaw.provider_run"
    assert start["phase"] == "start"
    assert start["trace_id"] == "trace-provider-1"
    assert start["attributes"]["provider"] == "claude"
    assert start["payload"]["provider_api_key"] == REDACTED
    assert start["payload"]["headers"]["authorization"] == REDACTED
    assert start["payload"]["headers"]["x-api-key"] == REDACTED
    assert start["payload"]["callback_url"] == "https://example.invalid"

    rendered = json.dumps([extra for _message, extra in logger.records])
    assert "sk-provider-secret" not in rendered
    assert "sk-provider-secret-2" not in rendered
    assert "provider-secret" not in rendered


def test_controller_telemetry_reuses_trace_and_span_public_api() -> None:
    from code_index.openclaw_controller import telemetry as controller_telemetry

    logger = CapturingLogger()
    task = controller_telemetry.trace_task_payload(
        {"kind": "openclaw.task.assigned", "trace_id": "trace-controller-1"}
    )
    event = controller_telemetry.trace_event_payload(
        {"kind": "openclaw.controller.event"},
        source=task,
    )

    with controller_telemetry.assignment_span(
        event,
        logger=logger,
        prefer_otel=False,
    ):
        pass

    assert event["trace_id"] == "trace-controller-1"
    assert logger.records[0][1]["openclaw"]["span_name"] == "openclaw.assignment"
    for helper_name in (
        "assignment_span",
        "local_dispatch_span",
        "provider_run_span",
        "verification_span",
        "memory_sync_span",
    ):
        assert callable(getattr(controller_telemetry, helper_name))
