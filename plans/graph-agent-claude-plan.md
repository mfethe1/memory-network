# Graph Agent Orchestration Plan

Audience: Claude Code or another coding agent picking up the graph-server
task orchestration slice.

Next hardening roadmap:
[`graph-agent-companion-hardening-plan.md`](graph-agent-companion-hardening-plan.md).

New-session PRD:
[`../docs/graph-agent-companion-prd.md`](../docs/graph-agent-companion-prd.md).

## Goal

Make `code_index graph-server` a seamless local control plane for supervising
coding agents from the browser: submit tasks, watch active files, inspect
events, and keep graph context stable without page-level refresh churn.

## Current State

1. `POST /api/agent-runs` records a queued run and returns a callback URL.
2. `POST /api/agent-events` records read/edit/test/status events and updates
   run status.
3. If `CODE_INDEX_AGENT_WEBHOOK_URL` is set, graph-server dispatches task JSON
   to that webhook.
4. If `CODE_INDEX_GRAPH_TOKEN` is set, graph-server POST endpoints require
   `Authorization: Bearer <token>`.
5. The browser can submit a task from the selected graph node, store graph view
   state, pan/zoom/focus the graph, and show active runs in the navigator.

## Next Increments

1. Harden provider-specific command presets.
   - `code_index agent-adapter --mode command` now invokes a configured local
     command, streams stdout/stderr as `tool` events, records selected paths,
     and marks the run completed or failed from the process exit code.
   - `--provider claude`, `--provider codex`,
     `CODE_INDEX_AGENT_PROVIDER=claude`, and `CODE_INDEX_AGENT_PROVIDER=codex`
     now provide built-in presets, while custom commands remain available.
   - `graph-server` can dispatch directly to that adapter when
     `CODE_INDEX_AGENT_COMMAND` or `CODE_INDEX_AGENT_PROVIDER` is set, while
     keeping the existing HTTP webhook path.
   - Next: parse provider output into structured read/edit/test events instead
     of treating every line as a generic tool event.

2. Deepen event streaming for responses.
   - SSE now emits an `agent` event carrying active runs and recent activity,
     and the browser updates run/activity state without fetching full graph
     JSON.
   - Next: render a compact activity timeline or transcript for the selected
     run.

3. Improve cancellation and completion controls.
   - `POST /api/agent-runs/<run_id>/cancel` now records `cancelled`, and the
     browser exposes cancel controls on queued/working run rows.
   - Local command dispatch now registers a cancellation handle, and the
     command adapter interrupts the spawned process tree before posting a final
     `cancelled` status.
   - Next: persist active process metadata so a graph-server restart can mark
     orphaned local runs stale.

4. Add durable task records if run metadata becomes too overloaded.
   - Keep `agent_runs` as the canonical run table for now.
   - Introduce a separate `agent_tasks` table only if task-specific fields
     cannot fit cleanly in `metadata_json`.

5. Improve safety.
   - Keep remote bind opt-in.
   - Reuse the bearer-token posture from MCP if graph-server gains
     `--allow-remote`.
   - Never dispatch secrets in task payloads.

6. Add browser-level verification.
   - Prefer Playwright if the project accepts a dev dependency.
   - Cover graph load, pan/zoom controls, task submission, token prompt flow,
     and quiet live refresh behavior.

7. Package distribution.
   - `plugins/code-index-agent` now contains a Codex plugin manifest, MCP
     config, skill playbook, icon, graph-server launcher, and marketplace
     entry.
   - Next: add screenshots, command availability checks, and public repository
     metadata before publishing outside this repo.

## Risks

1. Agent adapter scope creep.
   - Mitigation: keep graph-server as control plane only; adapter owns process
     execution.

2. Stale queued runs.
   - Mitigation: adapter must post terminal `status` events; add a timeout or
     stale-run indicator later.

3. Browser refresh churn.
   - Mitigation: prefer event-specific SSE updates and full graph fetch only
     when graph signatures change.

4. Auth confusion.
   - Mitigation: document `CODE_INDEX_GRAPH_TOKEN` for inbound browser POSTs
     and `CODE_INDEX_AGENT_WEBHOOK_TOKEN` for outbound adapter dispatch.

## Success Criteria

1. A task submitted from the browser creates a run, dispatches to a real
   local command adapter or webhook, and shows queued/working/completed status
   without a page reload.
2. Agent read/edit/test events appear in the graph within one second on a
   visible page.
3. Active files highlight before reindexing.
4. Manual graph position, filters, selected node, and expanded tree state
   survive reloads and quiet live updates.
5. Full test suite remains green and new HTTP/browser behavior has focused
   coverage.
