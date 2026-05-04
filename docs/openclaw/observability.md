# OpenClaw Observability

OpenClaw observability is dependency-light by default. Dispatch must work when
no collector, exporter, or tracing package is installed.

## Trace Contract

Every fleet payload should carry `trace_id` once it enters the OpenClaw
controller or host daemon path. The helper modules are:

- `code_index.openclaw_controller.telemetry`
- `code_index.openclaw_hostd.telemetry`

Use the typed helpers for the payload category being emitted:

- `trace_task_payload(...)`
- `trace_lease_payload(...)`
- `trace_run_payload(...)`
- `trace_event_payload(...)`
- `trace_memory_sync_payload(...)`

The helpers preserve an existing non-empty `trace_id`, copy it from a source
payload when one is provided, or generate a new 128-bit lowercase hex trace id.
They return a new dict and do not mutate the input payload.

## Span Helpers

The public span helpers are:

- `assignment_span(...)`
- `local_dispatch_span(...)`
- `provider_run_span(...)`
- `verification_span(...)`
- `memory_sync_span(...)`

When `opentelemetry` is importable, the helpers start spans named
`openclaw.assignment`, `openclaw.local_dispatch`, `openclaw.provider_run`,
`openclaw.verification`, and `openclaw.memory_sync`. The package is optional.
The helpers also emit local structured log records, so a missing package or
collector does not block assignment, dispatch, provider execution,
verification, or memory sync.

For host-local fallback files, use `configure_local_telemetry_logging(log_dir)`.
It configures a rotating JSONL logger suitable for collector outages.

## Redaction

Telemetry records redact secret-bearing fields before logging. Field names
containing markers such as `api_key`, `auth`, `credential`, `password`,
`secret`, or `token` are replaced with `[REDACTED]`. URL fields are reduced to
scheme and host, dropping user info, path, query, and fragment.

Do not log provider environment dumps, raw provider config, NKey seeds,
controller tokens, bearer tokens, or enrollment codes. When a provider payload
must be referenced, emit stable ids, provider name, host id, task id, run id,
event type, and the shared `trace_id`.

## Later Integrations

Langfuse and Phoenix can be added later as observability sinks through an
OpenTelemetry Collector, exporter, or offline ingestion job. They are not
dispatch dependencies and should not be required to assign tasks, start local
provider runs, verify work, or sync memory summaries.
