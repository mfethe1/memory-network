# OpenClaw Context Manager

Slice 7A implements the passive Milestone 1 subset of the Context Manager. It
does not invoke an LLM, inject correction pointers, start fresh provider
sessions, sync to remote fumemory, or enforce active control. It stores
reviewable local artifacts and emits passive health/quality signals.

## Responsibilities

- Store context sources and hot-load pointers in a fumemory-compatible SQLite
  schema.
- Dedupe context pointers by source URI, content hash, and canonical locator
  JSON.
- Filter retrieval by sensitivity for local, cross-provider, cross-host, and
  external-message routes.
- Collect host context metrics without secrets or raw transcript dumps.
- Build signed context manifests through the five-step `code_index` pipeline.
- Emit passive context-health and quality-gate events.
- Generate idempotent handoff packets when fresh-session pressure is detected.
- Provide an avoid-pointer hold decision function that a controller can call
  before assignment without invoking a live CMA.

## Storage

`code_index.openclaw_context.store.SQLiteContextStore` creates these local
tables:

```text
context_sources
context_pointers
context_relevance_scores
agent_context_leases
handoff_packets
context_health_events
context_manifests
```

The first six tables mirror the planned fumemory shape for M1. The
`context_manifests` table is local replay storage for signed manifest
idempotency. The SQLite store enables WAL mode, `busy_timeout`, foreign-key
checks, and `synchronous=NORMAL` for local durability and concurrent readers.

Sources represent handles to existing local state, not duplicated state. The
host probe exposes handles for:

```text
code_index://context-packet/<run_id>
code_index://graph-context/<run_id>
code_index://collaboration/<run_id>
code_index://transcript/<run_id>
code_index://run-metadata/<run_id>
code_index://claims/<run_id>
```

## Sensitivity

Pointers can use these sensitivity values:

- `public`: visible to local, cross-host, cross-provider, and external routes.
- `repo`: visible inside OpenClaw repo context, not external messaging.
- `host_private`: visible only on the same host, including cross-provider.
- `provider_private`: visible only to the same host and provider.
- `external_blocked`: visible internally but never to external messaging.

Manifest building uses the same `ContextRetrievalPolicy` as direct pointer
retrieval. Both target-symbol candidates and explicitly requested required
pointer IDs are checked against the request host, provider, and route scope
before they can be signed into a manifest.

Hostd can attach passive context metrics with `--probe-context`. Setting
`OPENCLAW_HOSTD_CONTEXT_STORE_PATH` or `context_store_path` in the hostd JSON
config asks the probe to check that local store. If the store is unavailable,
hostd reports `context_manager_degraded` health data and continues normal
heartbeat/task execution.

## Context Health

The passive health evaluator reports:

- `token_pressure` around 65k to 70k tokens.
- `handoff_prepare` around 75k tokens.
- `fresh_session_recommended` around 80k tokens.
- `stale_context` on source hash mismatch.
- `duplicate_context` when the same pointer is loaded more than once.
- `missing_required_instructions` when required pointers are absent.
- `repeated_failed_approach` on repeated approach history.
- `pending_edit_under_pressure` when edit claims remain active near handoff.
- `provider_compaction_without_handoff` as critical degraded fallback.
- `context_manager_degraded` when local fumemory storage is unavailable.

These are alert-only in Milestone 1.

## Quality Gates

`detect_quality_gate_flags()` emits passive flags for:

- zero test runs on a complex task,
- no `code_index impact` call before symbol edits,
- premature done without verification,
- repeated approaches in `approach_history_json`, and
- goal drift from acceptance criteria.

Each flag records `passive=true` and `invoked_llm=false`.

## Deferred

Milestone 2 owns live CMA model invocation, tier escalation, correction pointer
injection, completed-work index writes, remote fumemory sync, provider SDK work,
observability tracing, installer work, and MCP server exposure.
