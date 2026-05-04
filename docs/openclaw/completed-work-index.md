# Completed Work Index

Milestone 2 Slice 7 adds a local Completed Work Index in the
fumemory-compatible SQLite context store. It is the durable, compact record of
what an Agent Run finished, shaped so a later remote fumemory sync can replay
the same payload safely.

## Entry Shape

Each entry records:

- `work_id`
- `idempotency_key`
- `host_id`
- `repo_id`
- `task_id`
- `run_id`
- `completed_at`
- `files_changed`
- `symbols_affected`
- `approach_taken`
- `approaches_rejected`
- `verification_results`
- `follow_up_pointers`
- `trace_id`
- `source_event_offsets`
- `metadata`

The SQLite tables use JSON columns for the list and object fields, plus
dedicated lookup tables for changed files and affected symbols:

- `completed_work_index`
- `completed_work_files`
- `completed_work_symbols`

`SQLiteContextStore.list_completed_work_by_symbol(symbol)` and
`SQLiteContextStore.list_completed_work_by_file(path)` provide the follow-up
retrieval path for CMA and manifest construction. File lookup normalizes
Windows and POSIX separators.

## Sync Semantics

Use `record_completed_work_index(store, **payload)` when run completion should
continue even if local fumemory storage is unavailable. The helper returns a
`CompletedWorkRecordResult` with `stored=False` and
`degraded_reason="fumemory_unavailable"` instead of raising on store write
failure.

Repeated payloads are deduplicated by `idempotency_key`. If no key is provided,
the helper derives one from the compact completed-work fields, excluding
`completed_at`, so replaying the same run completion payload is safe.

## Transcript Policy

Raw transcript text is not written by default. The entry builder only keeps the
Completed Work Index fields and recursively removes transcript-shaped keys such
as `raw_transcript`, `transcript_text`, `messages`, `events`, and
`terminal_output` from metadata and verification payloads.
