# Cursor SDK Adapter

OpenClaw treats Cursor as an execution provider behind a Node sidecar. The
control plane still owns task assignment, cancellation requests, and local run
state; the sidecar only translates a local task into Cursor SDK calls and emits
JSONL events that the existing command adapter can normalize.

## Package

The sidecar lives in `plugins/cursor-agent-sidecar` and pins `@cursor/sdk` to
`1.0.12`. Keep the pin exact because the SDK is public beta and its command,
streaming, and lifecycle surfaces should be upgrade-tested deliberately.

Build and run locally:

```bash
npm --prefix plugins/cursor-agent-sidecar install
npm --prefix plugins/cursor-agent-sidecar run build
node plugins/cursor-agent-sidecar/dist/index.js run --root . --task-json task.json
```

When installed as a package, it exposes the `cursor-agent-sidecar` binary.

## Commands

Supported commands:

- `create`: create a Cursor agent and emit its Cursor agent ID.
- `run` / `prompt`: read the task and provider prompt, send a Cursor prompt,
  stream events, wait for the terminal result, and emit `command.result`.
- `stream`: attach to an existing run and stream events.
- `wait`: wait for an existing run and emit one terminal status.
- `cancel`: cancel an existing run and emit one `cancelled` terminal status.
- `archive`: archive an agent where the SDK/runtime supports it.
- `delete`: permanently delete an agent where the SDK/runtime supports it.

The command adapter preset uses this shape:

```bash
cursor-agent-sidecar run \
  --root <repo> \
  --task-json <task.json> \
  --provider-prompt-file <provider-prompt.txt> \
  --mcp-config-file <mcp.json>
```

## Dry-Run Fallback

If `CURSOR_API_KEY` is absent, `CODE_INDEX_CURSOR_DRY_RUN` requests dry-run, or
the SDK package cannot be imported, the sidecar does not contact Cursor. It
emits deterministic JSONL:

1. `run.started`
2. one `tool.call` read event for each selected path
3. one `assistant.message`
4. one `run.completed`
5. `command.result`

Lifecycle commands also return deterministic fallback events. `cancel` emits a
single `run.cancelled` terminal status so local cancellation handling remains
idempotent even without Cursor runtime access.

## Event Contract

Sidecar events are JSON objects with `provider: "cursor"` and Cursor run refs:

```json
{
  "provider": "cursor",
  "event": "assistant.message",
  "local_run_id": "local-run-1",
  "cursor_agent_id": "agent-123",
  "cursor_run_id": "run-123",
  "role": "assistant",
  "message": "Updated the adapter."
}
```

`code_index.agent_adapters.cursor` exposes Python helpers for tests and adapter
integration:

- `build_sidecar_command(...)`
- `should_use_dry_run(...)`
- `dry_run_events(...)`
- `parse_sidecar_json_line(...)`
- `normalize_stream_records(...)`

The normalizer maps Cursor SDK/status/tool messages into local event types:
`read`, `edit`, `test`, `tool`, `navigate`, `note`, `decision`, and `status`.
Terminal statuses are deduplicated so cancellation, failure, or completion is
recorded exactly once per normalized stream.
