"""Controller-facing OpenClaw telemetry helpers.

The controller and host daemon share trace-id and span contracts so fleet
events can be followed without making dispatch depend on an observability
backend.
"""

from __future__ import annotations

from code_index.openclaw_hostd.telemetry import TRACE_ID_FIELD
from code_index.openclaw_hostd.telemetry import assignment_span
from code_index.openclaw_hostd.telemetry import configure_local_telemetry_logging
from code_index.openclaw_hostd.telemetry import ensure_trace_id
from code_index.openclaw_hostd.telemetry import extract_trace_id
from code_index.openclaw_hostd.telemetry import generate_trace_id
from code_index.openclaw_hostd.telemetry import local_dispatch_span
from code_index.openclaw_hostd.telemetry import memory_sync_span
from code_index.openclaw_hostd.telemetry import provider_run_span
from code_index.openclaw_hostd.telemetry import redact_payload
from code_index.openclaw_hostd.telemetry import telemetry_span
from code_index.openclaw_hostd.telemetry import trace_event_payload
from code_index.openclaw_hostd.telemetry import trace_lease_payload
from code_index.openclaw_hostd.telemetry import trace_memory_sync_payload
from code_index.openclaw_hostd.telemetry import trace_run_payload
from code_index.openclaw_hostd.telemetry import trace_task_payload
from code_index.openclaw_hostd.telemetry import verification_span


__all__ = [
    "TRACE_ID_FIELD",
    "assignment_span",
    "configure_local_telemetry_logging",
    "ensure_trace_id",
    "extract_trace_id",
    "generate_trace_id",
    "local_dispatch_span",
    "memory_sync_span",
    "provider_run_span",
    "redact_payload",
    "telemetry_span",
    "trace_event_payload",
    "trace_lease_payload",
    "trace_memory_sync_payload",
    "trace_run_payload",
    "trace_task_payload",
    "verification_span",
]
