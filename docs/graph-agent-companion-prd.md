# Graph Agent Companion PRD

## Purpose

Turn `code_index graph-server` into a reliable local control plane for coding
agents. The graph should help agents understand code organization, coordinate
work, retrieve bounded context, and expose operational failures before they
turn into broken edits.

This document is the new-session handoff. Start a fresh session with `/new`,
read this PRD, then inspect current git status before editing. Do not assume
the slices below are all complete; use the status section as the source of
truth.

## Current Status

Completed:

1. Retrieval eval scaffold:
   - `python -m code_index doctor --eval-retrieval`
   - Bundled eval fixture under `code_index/evals/`
   - Reports recall, precision, latency, and byte budget metrics.
2. Server-enforced preflight receipts:
   - `agent_task_preflights` table.
   - Canonical JSON hashes and opaque HMAC-style `preflight_id`.
   - Graph-scoped `/api/agent-runs` requires a current preflight.
   - Preflights expire and are consumed once.
3. Minimal perf counters:
   - `/api/debug/perf`
   - Counters for preflight rejection, auth failure, SSE drops, and claim
     conflicts.
4. Claim fencing:
   - `agent_file_claims.fence_token`.
   - Conflicting edit/exclusive claims are rejected.
   - Stale fence verification helper exists.
5. Browser auth off URL:
   - `graph-server` prints a clean `/repo-graph.html` URL.
   - Query-string tokens are rejected for protected JSON/API routes.
   - Browser auth uses `/api/auth/browser-session` to set an httpOnly
     same-origin cookie for graph page requests and SSE.
6. Kanban-style task blockers:
   - `agent_run_blockers` table records run-to-run blocker edges.
   - Graph tasks accept `blocked_by_run_ids` and stay `blocked` until blocker
     runs complete.
   - `/api/agent-board`, `code_index agent board`, and the graph sidebar expose
     blocked, ready, active, review, and done columns.
7. Hook-level lease enforcement:
   - `python -m code_index agent verify-claim --run-id ... --file ... --fence ...`
     verifies current edit/exclusive claims for supervised writes.
   - Missing, stale, expired, and conflicting claims return distinct failure
     messages.
   - `.claude/hooks/verify-claim-before-edit.sh` provides an opt-in
     `PreToolUse` guard when `CODE_INDEX_AGENT_RUN_ID` and fence env vars are
     set.
8. MCP graph context:
   - Read-only `graph_context` MCP tool and `codeindex://graph-context`
     resource.
   - Layered graph context includes stable IDs, layer, distance, relation path,
     risk, byte cost, and closed-enum `why_included` values.
   - MCP and HTTP preflight graph context share stable handles and budget
     enforcement.
9. Sanitized ops snapshot backend tracer bullet:
   - `/api/debug` includes an `ops` snapshot for auth failures, preflight
     rejections, claim conflicts, SSE drops, stale runs, search latency, and
     retrieval-budget readiness.
   - Debug payloads recursively scrub secret-like env values and omit lease or
     fence token fields.
10. Retrieval broker core:
    - `code_index.retrieval` owns the shared broker contract for file paths,
      code chunks, and transcript events.
    - `retrieval_broker` MCP tool and `codeindex://retrieval-broker/{query}`
      resource expose the broker as a read-only agent surface.
    - `/api/search` and `doctor --eval-retrieval` now use the same broker
      contract instead of parallel mini-retrievers.
11. Browser ops panel:
    - The Debug tab renders ops cards for auth failures, preflight rejections,
      claim conflicts, SSE drops, stale runs, search latency, and retrieval
      budget readiness.
    - `/events` emits `perf:tick` SSE updates from the same perf counters.

Latest verified commands:

```bash
python -m pytest -q
python -m code_index doctor --eval-retrieval
```

Latest result: `315 passed, 3 warnings in 77.38s`.
Retrieval eval result: `cases=3 recall=1.00 precision=1.00 bytes_p95=41`.

## Product Goals

1. Agents see the graph as structured, ranked, budgeted context.
2. Browser-submitted tasks cannot bypass preflight, auth, or lease checks.
3. Multiple agents can coordinate file ownership in near real time.
4. Debug panels reveal operational failure modes without leaking secrets.
5. The same context model works across browser, CLI adapter, Claude Code, Codex,
   and MCP-capable systems.

## Non-Goals

1. Do not make embeddings the default retrieval path until evals prove lift.
2. Do not use query-string tokens for new browser auth.
3. Do not make HTTP the primary agent context surface when MCP can expose the
   same data with scoped tools/resources.
4. Do not add child-agent orchestration until preflight, leases, retrieval
   evals, and checkpoint context handles are solid.
5. Do not add external telemetry; keep diagnostics local.

## Slice 1 - Browser Auth Off URL

Status: completed.

Problem: `?token=` URLs leak through browser history, logs, screenshots, and
referrers. EventSource cannot send an `Authorization` header, so browser SSE
needs a same-origin cookie rather than query params.

Requirements:

1. `graph-server` prints `/repo-graph.html` without a token in the URL.
2. If `CODE_INDEX_GRAPH_TOKEN` is set and the browser opens the graph without
   auth, serve a minimal auth page, not graph payload.
3. Browser token submission uses `Authorization: Bearer <token>` to create an
   httpOnly same-origin session cookie.
4. `/events` authenticates by cookie so SSE works without query tokens.
5. API and adapter clients may still use `Authorization: Bearer`.
6. Query-string token auth is rejected for new sessions.

Acceptance:

1. `/repo-graph.json?token=...` returns unauthorized.
2. `/repo-graph.html?token=...` does not serve graph payload unless a valid
   cookie already exists.
3. `/api/auth/browser-session` sets a session cookie only after valid bearer
   auth.
4. Browser tests still submit and stream tasks.

## Slice 2 - Hook-Level Lease Enforcement

Status: completed.

Problem: fence tokens exist, but external tools can still write files unless
they cooperate.

Requirements:

1. Add a local hook or wrapper that verifies edit/exclusive claim and fence
   token before supervised writes.
2. Expose a small verification command for hooks:
   `python -m code_index agent verify-claim --run-id ... --file ... --fence ...`
3. Add clear failure messages for missing claim, stale fence, expired claim,
   and conflicting claim.
4. Keep read-only claims non-blocking.

Acceptance:

1. Stale fence write attempts fail before mutation.
2. Terminal run status releases active claims.
3. The hook path is documented for Claude Code and adapter command mode.

## Slice 3 - MCP Graph Context

Status: completed.

Problem: agent-facing graph context should not be a browser-only HTTP API.

Requirements:

1. Add MCP tool/resource for `graph_context`.
2. Return layered graph context with stable IDs, layer, distance, relation path,
   risk, byte cost, and closed-enum `why_included`.
3. Keep HTTP routes for browser use, but MCP is the primary agent surface.
4. Add tests proving MCP and HTTP return equivalent stable handles.

Acceptance:

1. `code_index mcp-serve --describe` lists `graph_context`.
2. MCP graph context respects byte and node budgets.
3. No free-text `why_included` values.

## Slice 4 - Retrieval Broker

Status: in progress. Core broker, MCP exposure, HTTP search mirroring, and eval
wiring are complete. Graph-context, diagnostic, and affected-test collectors
remain.

Problem: `/api/search` is useful for humans, but agents need ranked context
bundles with budgets, provenance, and eval tracking.

Requirements:

1. Add retrieval broker over code chunks, file paths, transcript events, graph
   context, diagnostics, and affected tests.
2. Expose through MCP first; HTTP route can mirror for browser/local adapter.
3. Every result includes stable handle, source kind, byte cost, provenance, and
   truncation reason.
4. Eval harness must run before and after ranking changes.

Acceptance:

1. Eval metrics are reported in CI or targeted test output.
2. Retrieval never exceeds request byte budget.
3. Transcript plus file search works under one broker contract.

## Slice 5 - Full Ops Panel

Status: completed.

Problem: minimal counters exist, but the browser debug panel needs actionable
operations views.

Requirements:

1. Add UI for preflight rejections, auth failures, claim conflicts, SSE drops,
   stale runs, search latency, and retrieval budget.
2. Keep secrets out of debug payloads.
3. Add `perf:tick` SSE after the counter model is stable.

Acceptance:

1. Debug payload has no bearer tokens, session cookies, lease tokens, or
   webhook secrets.
2. Browser renders useful empty and active states.

## Slice 6 - Agent-Managed Child Tasks

Status: deferred.

Problem: agents should eventually break large work into child tasks, but only
after context retrieval and safety gates are measurable.

Requirements:

1. Child tasks inherit parent budget, auth posture, selected root, and lease
   constraints.
2. Child dispatch requires preflight and lease checks.
3. Checkpoints store stable handles and byte-capped summaries, not transcript
   dumps.
4. Max depth, max active children, and max total budget are enforced.

Acceptance:

1. Agent cannot dispatch child task without preflight.
2. Child task cannot exceed inherited budget.
3. Resume reconstructs context through handles.
