# OpenClaw Fumemory Sync And Completed Work Durability

This slice keeps Completed Work durability local-first.

## Current Write Path

- `record_completed_work_index(store, **payload)` writes into `SQLiteContextStore`.
- The write is idempotent on `idempotency_key`.
- Raw transcript-shaped keys stay excluded unless a caller opts in explicitly.
- Store failures return `stored=False` with `degraded_reason="fumemory_unavailable"` instead of failing run completion.

That means Milestone 1 does not need a Railway-hosted fumemory API to finish a run safely. The durable path is the local SQLite Completed Work Index, and Fleet MCP can query the same store later by file or symbol.

## Railway Posture

- Mount a Railway volume on every service that needs local context durability.
- Point `OPENCLAW_CONTEXT_STORE_PATH` at a file under `RAILWAY_VOLUME_MOUNT_PATH`.
- Use the same context-store path for the controller service and Fleet MCP service when they should read the same completed-work and pointer data.

Recommended path shape:

```text
${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/context-store.db
```

## Failure Model

- Local SQLite write succeeds: run completion is durable immediately.
- Local SQLite write fails: the caller receives a degraded result and may surface that state to operators, but the run does not crash because fumemory is unavailable.
- Optional remote fumemory sync remains a follow-on concern. This slice does not add a mandatory remote sync dependency or a hard-coded fumemory URL.

## Query Model

Completed Work entries remain queryable after restart through:

- `list_completed_work_by_file(path)`
- `list_completed_work_by_symbol(symbol)`

File-path lookup normalizes Windows and POSIX separators before comparison, so replay and restart behavior stays stable across mixed host environments.
