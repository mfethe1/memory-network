# Graph Agent Companion Hardening Plan

Audience: coding agents and humans extending `code_index graph-server` from a
live graph viewer into a reliable agent companion system.

Status: research-backed implementation plan. Firecrawl and Tavily were used for
current research. A reusable local Tavily skill now reads the API key from
`TAVILY_API_KEY`, `TAVILY_API`, or `~/.openclaw/secrets/tavily.env` without
hardcoding the secret. Claude CLI was used in `--bare --model sonnet` mode for
the first planning critique and `--bare --model opus` mode for the post-Tavily
architecture review.

## Research Inputs

1. Sourcegraph's context retrieval writeup argues for a context engine, not a
   giant prompt. It combines keyword, embedding, graph-based, local, and other
   retrievers, then ranks candidates under latency, cost, and token budgets:
   https://sourcegraph.com/blog/lessons-from-building-ai-coding-assistants-context-retrieval-and-evaluation
2. Claude Code's best practices emphasize explore-plan-code workflows,
   aggressive context management, deterministic hooks, verification, and
   subagents for bounded investigation:
   https://code.claude.com/docs/en/best-practices
3. MCP security guidance pushes least privilege, explicit consent, no token
   passthrough, and treating session IDs as identifiers rather than
   authorization:
   https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
4. Anthropic's multi-agent research system pattern supports an orchestrator
   that decomposes complex work into bounded subagents, but the overhead is
   only worth it when the task is large enough:
   https://www.anthropic.com/engineering/built-multi-agent-research-system
5. Tavily follow-up research on 2026 context engineering and multi-agent
   systems reinforced the same direction but exposed noisy secondary-source
   results. Treat broad agent trend articles as weak evidence; prefer official
   Claude, MCP, Sourcegraph, and peer-reviewed sources for implementation
   choices.
6. Opus reviewed this plan after the Tavily pass and flagged five must-fix
   design issues: retrieval evals must come before retriever changes,
   preflight receipts must be HMAC-signed over canonical JSON, auth must move
   off URL query strings, file leases need fencing tokens and hook enforcement,
   and agent context should be exposed through MCP instead of a parallel
   HTTP-only agent surface.

## Brutal Diagnosis

The system has crossed from "nice graph" into "control plane," but several
parts are still advisory. Advisory systems fail exactly when an agent is under
pressure: long context, partial memory, stale browser state, overlapping edits,
and weak auth assumptions. The next work should make the graph server the
source of truth for what an agent may do next, while keeping all heavy context
as handles and summaries rather than dumping raw blobs into prompts.

The highest-risk gaps are:

1. Task preflight can be bypassed if dispatch accepts the same payload shape
   without a server-issued preflight receipt.
2. Preflight receipts need HMAC signing and canonical JSON. A bare hash is
   forgeable or brittle when key ordering changes.
3. Query-string graph tokens are not a real auth posture. Scrubbing browser
   history is only cleanup after the token has already crossed logs and
   referers.
4. File claims are visible, but they are not yet strong enough to act as
   real-time leases with fencing tokens, renewal, expiry, and hook-level write
   enforcement.
5. Search mixes user convenience and agent retrieval. Agents need budgeted,
   ranked context bundles with provenance, truncation metadata, and retrieval
   evals before changing ranking.
6. The debug panel is useful locally, but it is not yet an agent operations
   panel. It should expose lag, dropped events, stale leases, retrieval budget,
   and queue health across adapters.
7. Layered graph mode improves human scanning, but agents need the same layers
   as structured JSON with closed-enum "why included" reasons and budget
   controls.
8. The current plan risks building a parallel HTTP agent API when the existing
   MCP server is the better least-privilege surface for agent context tools.

## Target Architecture

Keep the existing graph-server shape, but introduce six internal services:

1. `RetrievalEvalHarness`
   - Owns a small golden set for code, transcript, graph, and test-context
     retrieval.
   - Reports `recall@k`, `precision@k`, `bytes_used`, and latency before any
     new ranking scheme is treated as better.

2. `TaskGate`
   - Owns task draft, preflight receipt, dispatch eligibility, and warning
     acknowledgement.
   - Produces an HMAC-signed `preflight_id` tied to canonical JSON for the
     normalized task draft, selected graph nodes, warning set, active claims,
     auth audience, and expiry.

3. `LeaseManager`
   - Owns read, edit, and exclusive leases plus monotonic per-path fencing
     tokens.
   - Applies compatibility rules before dispatch and before adapter write
     execution through hooks or wrapper enforcement.
   - Emits lease lifecycle events over SSE.

4. `RetrievalBroker`
   - Owns transcript search, file/chunk search, graph neighborhood retrieval,
     repo-map snippets, affected tests, and diagnostics as separate retrievers.
   - Returns ranked candidates with byte sizes, provenance, and truncation
     reasons.
   - Exposes agent-facing operations through MCP tools/resources first; HTTP
     routes are for browser UI and local adapter bridge needs.

5. `GraphContextService`
   - Converts graph neighborhoods into layered, agent-friendly JSON.
   - Adds relation paths, distance, layer, active claims, related tests,
     diagnostic risk, byte budgets, and closed-enum "why included" metadata.

6. `OpsTelemetry`
   - Publishes adapter, SSE, search, graph render, task, and lease metrics.
   - Keeps local-only secrets and tokens out of snapshots and browser history.

## Implementation Slices

### Slice 0 - Retrieval Evaluation Harness

Goal: stop changing retrieval by vibes. Any new retriever, ranking signal, or
graph context expansion must beat the current baseline under a byte budget.

Data:

1. Create a small checked-in fixture of 30-50 evaluation questions covering
   symbol lookup, implementation lookup, transcript lookup, affected tests,
   diagnostics, and graph-neighborhood questions.
2. Store expected stable IDs: chunk UID, symbol UID, file path, event PK, test
   node ID, or graph node ID.
3. Include both easy exact-match cases and adversarial cases where naive FTS
   returns the wrong file.

Metrics:

1. `recall@5`, `recall@10`, `precision@10`.
2. `bytes_used_p50`, `bytes_used_p95`.
3. `latency_ms_p50`, `latency_ms_p95`.
4. Regression summary versus the existing FTS baseline.

Interface:

1. `python -m code_index doctor --eval-retrieval --json`
2. Optional browser/debug display after the CLI is stable.

Tests:

1. The baseline eval runs without network access.
2. Missing expected IDs fail clearly.
3. A retriever that exceeds the byte budget is reported as worse even if recall
   improves.

### Slice 1 - Server-Enforced Task Preflight

Goal: no agent run can be dispatched from the browser or HTTP API unless it is
based on a current server preflight.

Data model:

```sql
CREATE TABLE IF NOT EXISTS agent_task_preflights (
    preflight_pk INTEGER PRIMARY KEY,
    preflight_id TEXT NOT NULL UNIQUE,
    draft_hash TEXT NOT NULL,
    warning_hash TEXT NOT NULL,
    status TEXT NOT NULL, -- active | consumed | expired
    run_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
```

API:

1. `POST /api/agent-task-preflight`
   - Returns `preflight_id`, `draft_hash`, warnings, context summary, and
     expiry.
   - Computes hashes from canonical JSON so field order and incidental spacing
     cannot change the result.
   - Uses HMAC with a server-local secret. Do not accept caller-provided
     signatures.
2. `POST /api/agent-runs`
   - Requires `preflight_id` when the task came from graph UI or includes
     selected nodes.
   - Rejects stale, mismatched, already-consumed, or warning-unacknowledged
     preflights with distinct statuses: `412 Precondition Failed` for stale or
     mismatched, `409 Conflict` for consumed, and `428 Precondition Required`
     for missing.

UI:

1. First submit calls preflight.
2. If warnings exist, second submit sends the same normalized draft plus
   `preflight_id`.
3. Move graph auth off `?token=`. Prefer `Authorization: Bearer` for API calls
   and an httpOnly same-origin cookie for browser/SSE if the server supports
   it. URL scrubbing remains only as cleanup for old links.

Tests:

1. Direct `/api/agent-runs` bypass fails for graph-scoped tasks.
2. Tampered selected nodes after preflight fail.
3. Expired preflight fails.
4. Successful dispatch consumes preflight once.
5. Remote bind without auth token refuses mutating endpoints.
6. Query-string token auth is not accepted for new sessions.

### Slice 2 - Real-Time File Claims As Leases

Goal: make claims enforce coordination, not merely display intent.

Data model changes:

1. Keep `agent_file_claims`, but add compatibility-oriented metadata:
   `lease_token_hash`, `lease_kind`, `owner_agent`, `heartbeat_interval_ms`,
   `conflict_policy`, `last_conflict_json`, and `fence_token`.
2. Add `agent_file_claim_events` for auditability:
   claim created, renewed, released, expired, denied, and overridden.
3. Maintain a monotonic per-path fence counter. A renewed or replaced edit
   lease gets a newer fence token, so stale writers can be rejected even inside
   an expiry race window.

Lease modes:

1. `read`: compatible with read.
2. `edit`: exclusive against edit and exclusive, warning against read.
3. `exclusive`: blocks all other active modes.

Defer `review` and `test` modes until a real workflow needs them. Do not add
coordination states for labels that behave like read-only access.

API:

1. `POST /api/file-claims`
   - Claim paths, return lease tokens and fence tokens only in the response
     body.
2. `POST /api/file-claims/<claim_id>/renew`
   - Renew with holder identity, lease token, and current fence token.
3. `POST /api/file-claims/<claim_id>/release`
   - Release idempotently.
4. `GET /api/file-claims?path=...&include=events`
   - Inspect active leases and recent lifecycle events.

Realtime:

1. Server sweep expires stale leases every 5 seconds.
2. Lease checks also happen lazily on claim/write attempts so a missed sweep
   does not preserve stale locks.
3. SSE emits `claim:update` with path, mode, holder, status, and expiry.

Agent contract:

1. An adapter must claim before writing selected files.
2. An adapter must renew active edit leases while working.
3. Lease tokens must never be embedded in prompts or transcripts.
4. Claude Code hooks, MCP tool wrappers, or adapter write wrappers must reject
   writes without an active edit or exclusive lease and matching fence token
   when the graph server is supervising the run.

Tests:

1. Concurrent exclusive claims cannot both succeed.
2. Read claims can coexist.
3. Expiry releases conflicts within one sweep.
4. Renew after expiry fails.
5. Terminal run status releases active claims.
6. Stale fence token cannot write after a newer edit lease exists.

### Slice 3 - Budgeted Transcript And File Search

Goal: make search useful for agents, not just a browser convenience feature.

Retrievers:

1. FTS over `chunks_fts` for code and docs.
2. FTS over `agent_events` for transcript messages and payload summaries.
3. Symbol lookup for definitions and occurrences.
4. Graph neighborhood retrieval for related files and symbols.
5. Diagnostics and affected tests for validation context.

Data model:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS agent_events_fts USING fts5(
    run_id,
    event_type,
    file_path,
    message,
    payload_text,
    content='',
    tokenize='unicode61 remove_diacritics 2'
);
```

API:

1. `GET /api/search?q=...&kinds=file,chunk,transcript,symbol&limit=20`
   - Human/browser search.
2. `POST /api/context/retrieve`
   - Browser and local adapter retrieval with `task`, `selected_nodes`,
     `budget_bytes`, `retrievers`, and `must_include`.
   - Returns ranked context handles, excerpts, byte counts, and `truncated`.
3. MCP tools/resources expose the same retrieval broker to agents:
   `retrieve_context`, `search_transcript`, `search_code`, `graph_context`,
   and `affected_tests`.
   - Prefer MCP as the primary agent surface because it already has scoped
     tools/resources and least-privilege configuration.

Ranking:

1. Gather candidates from all enabled retrievers.
2. Deduplicate by stable IDs: chunk UID, symbol UID, event PK, file path.
3. Score by exact/path hit, symbol hit, graph distance, recency, active claim
   relevance, and diagnostics/test relevance.
4. Fill the budget with high-value candidates first. Do not ask an LLM to
   count tokens.

Tests:

1. Transcript search finds recent decisions by run.
2. File search still returns code chunks with existing BM25 behavior.
3. Retrieval broker never exceeds `budget_bytes`.
4. Mixed search returns provenance and source kind for every result.
5. Retrieval eval metrics improve or stay neutral against Slice 0 baselines.
6. MCP and HTTP retrieval paths return equivalent stable handles for the same
   request.

### Slice 4 - Cross-System Performance And Debug Panel

Goal: turn the debug panel into an operations dashboard for graph-supervised
agent work.

Metrics:

Start with four actionable counters, then expand:

1. `preflight_rejections`, grouped by reason.
2. `auth_failures`, grouped by endpoint family.
3. `sse_dropped_events` and `sse_queue_depth_max`.
4. `claim_conflicts` and stale-fence rejections.

Full dashboard metrics can add `graph_build_ms`, `graph_payload_bytes`,
`visible_nodes`, `visible_edges`, `active_runs`, `queued_runs`, `stale_runs`,
`adapter_failures`, `active_claims`, `expired_claims_last_minute`,
`search_p50_ms`, `search_p95_ms`, and `retrieval_budget_bytes_p95`.

API:

1. Extend `GET /api/debug`.
2. Add `GET /api/debug/perf?window=60`.
3. Emit `perf:tick` SSE once per second with a compact rolling snapshot.

UI:

1. Debug tab sections: server health, adapter health, leases, search, SSE,
   graph render, auth posture.
2. Show warnings for stale runs, dropped SSE events, high search latency,
   unauthenticated remote bind, and active lease conflicts.
3. Keep this local and compact. No external telemetry.

Tests:

1. Debug endpoints include expected keys without secrets.
2. SSE queue cap drops slow subscribers and reports the drop.
3. Browser debug panel renders with no active runs and with synthetic active
   runs/claims.

### Slice 5 - Layered Graph Context For Agents

Goal: make layered graph/neighborhood expansion improve agent understanding,
not just visual layout.

Layer model:

1. `task`: selected node, active task, current run.
2. `claimed`: files currently claimed by this run or conflicting runs.
3. `local`: symbols/chunks in selected files.
4. `impact`: dependencies, callers, tests, imports, and related diagnostics.
5. `history`: recent edits, decisions, notes, and completed runs.

API:

1. `GET /api/graph/expand?node_id=...&hops=2&layers=task,local,impact&max_nodes=80`
2. `POST /api/context/graph`
   - Takes selected nodes and a byte budget.
   - Returns layered context JSON, not only graph nodes.
3. MCP `graph_context` resource/tool returns the same layered payload for
   agents.

Budget rules:

1. Every request has `max_nodes` and `budget_bytes`.
2. Each layer gets a floor budget so noisy `impact` or `history` data cannot
   starve `task` and `claimed`.
3. Shrink order is deterministic: trim `history`, then low-score `impact`,
   then low-score `local`; never trim selected `task` nodes.

Allowed `why_included` values:

`selected`, `claim_conflict`, `active_claim`, `contains`, `imports`, `calls`,
`called_by`, `test_of`, `diagnostic`, `recent_edit`, `note`, `transcript_hit`,
and `repo_map_anchor`.

Agent JSON shape:

```json
{
  "selected": [{"id": "...", "kind": "file", "path": "..."}],
  "layers": [
    {
      "name": "impact",
      "nodes": [
        {
          "id": "...",
          "kind": "symbol",
          "why_included": "called_by",
          "distance": 2,
          "relation_path": ["contains", "calls"],
          "risk": "has affected tests",
          "byte_cost": 420
        }
      ]
    }
  ],
  "truncated": false
}
```

Why this improves context:

1. It lets an agent ask "what matters near this task?" instead of scanning a
   whole repo.
2. It explains why every piece of context was included, which makes pruning and
   self-correction easier.
3. It separates current coordination risk from code dependency risk.
4. It provides stable handles that survive across checkpointed runs.
5. It gives the UI and agent the same mental model: current file, local
   neighborhood, broader impact, and history.

Tests:

1. `max_nodes` is respected with deterministic layer priority.
2. Every returned node includes a valid closed-enum `why_included`.
3. Conflicting file claims appear in the `claimed` layer.
4. Affected tests appear in the `impact` layer when known.
5. Layer budgets prevent `impact` from starving selected task context.

### Slice 6 - Agent-Managed Task Creation

Goal: allow agents to create and manage child tasks without letting them flood
the system or lose the plot.

Rules:

1. Agents may create child tasks only through `POST /api/agent-tasks`.
2. Child tasks inherit parent budget, auth posture, selected repo root, and
   lease constraints.
3. Server enforces max child depth, max active children, max total budget, and
   allowed adapters.
4. Agents can propose high-risk tasks, but user confirmation is required for
   destructive operations, broad edits, or external network dispatch.

Minimal API:

1. `POST /api/agent-tasks`
   - Create draft child task from a parent run.
2. `POST /api/agent-tasks/<task_id>/preflight`
   - Same gate as browser task preflight.
3. `POST /api/agent-tasks/<task_id>/dispatch`
   - Dispatch only after preflight and lease checks.
4. `POST /api/agent-runs/<run_id>/checkpoint`
   - Store concise progress, current handles, lease state, and remaining
     budget.

Token-budget strategy:

1. Do not pass long task histories to child agents.
2. Pass stable handles: selected nodes, context handle IDs, lease IDs, run IDs,
   and retrieval query IDs.
3. Require checkpoint summaries capped by bytes, not model token estimates.
4. On resume, reconstruct context through `RetrievalBroker` and
   `GraphContextService` instead of transcript stuffing.

Tests:

1. Agent cannot dispatch child task without preflight.
2. Child task cannot exceed inherited budget.
3. Max depth and max active child limits are enforced.
4. Checkpoint/resume reconstructs context handles without raw transcript dump.

## Delivery Order

1. Slice 0: Retrieval evaluation harness.
2. Slice 1: HMAC preflight enforcement, canonical JSON, and auth off URL.
3. Slice 4a: Minimal ops counters for preflight, auth, SSE, and lease conflicts.
4. Slice 2: Lease enforcement with fencing tokens and hook/write-wrapper checks.
5. Slice 5: Agent-facing layered graph context with closed-enum reasons.
6. Slice 3: Retrieval broker with transcript plus file search, validated
   against Slice 0.
7. Slice 4b: Full performance/debug panel.
8. Slice 6: Agent-managed child tasks with budgets and checkpoints.

This order is intentional. Evals come first so retrieval changes can be judged.
Preflight/auth and leases are control-plane safety. Minimal telemetry lands
before the systems it needs to observe. Agent-managed child tasks come last and
should ship only if evals show single-agent retrieval is no longer enough.

## First Tickets

1. Add retrieval eval fixtures and `code_index doctor --eval-retrieval --json`
   reporting `recall@10`, `precision@10`, `bytes_p95`, and latency.
2. Add `agent_task_preflights` with canonical JSON hashing, HMAC
   `preflight_id`, and migration coverage.
3. Require `preflight_id` on graph-scoped `/api/agent-runs` with distinct
   failure statuses for missing, stale/mismatched, and consumed preflights.
4. Move graph auth off `?token=` for new browser sessions and SSE.
5. Add `/api/debug/perf` counters for preflight rejections, auth failures, SSE
   drops, and claim conflicts.
6. Add lease fence tokens to `agent_file_claims` and reject stale-fence writes.
7. Add hook/write-wrapper enforcement for supervised writes.
8. Add `/api/graph/expand` and MCP `graph_context` fields: layer, distance,
   relation path, risk, byte cost, and closed-enum `why_included`.

## Non-Goals For This Pass

1. Do not make embeddings the default retrieval path. Keep them optional until
   there is a relevance benchmark for this workflow.
2. Do not let browser-only checks enforce security. Server endpoints must
   reject invalid states.
3. Do not add external telemetry. Local debug is enough.
4. Do not make all claims exclusive. Over-constraining read/review/test work
   will reduce parallelism.
5. Do not spawn child agents for routine lookup. Use retrieval first, subagents
   only for bounded investigation or parallel implementation.
6. Do not make HTTP the primary agent context API when MCP can expose the same
   broker through scoped tools/resources.
7. Do not build child-task creation until retrieval evals and lease fencing are
   in place.

## Success Criteria

1. Retrieval changes are judged by a local eval harness, not subjective result
   inspection.
2. A graph-scoped task cannot bypass HMAC preflight validation.
3. New graph auth does not depend on query-string tokens.
4. Two agents cannot hold incompatible edit leases on the same file, and stale
   fence tokens cannot write.
5. Agents can retrieve transcript plus file context under a byte budget through
   MCP-scoped tools/resources.
6. The debug panel identifies stale runs, dropped SSE events, lease conflicts,
   auth failures, and search latency without exposing secrets.
7. A selected graph node can expand into layered JSON that explains why each
   node matters with closed-enum reasons.
8. An agent can create a child task only within parent limits and with a
   reconstructable context plan.
