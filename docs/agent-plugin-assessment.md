# Agent Plugin Assessment

## Verdict

This is useful for real coding projects when the repo is large enough that
"open files until it makes sense" burns time or misses dependencies. The value
is strongest in these workflows:

- finding the right files and symbols before editing
- checking impact and affected tests before risky changes
- supervising browser-submitted agent tasks without page refresh churn
- seeing active files before the index catches up
- handing agents a bounded "where we are" packet before they start
- auditing run transcripts and decisions during long tasks
- getting parser diagnostics and affected-test commands when a run completes
- cancelling a runaway local agent process from the UI
- exposing the same context layer to Claude, Codex, and MCP-capable tools

It is less useful for tiny repos, one-file tasks, or teams that only need a
static search index. The graph control plane matters once there is enough task
state to supervise.

## What Is Shippable Now

- `code_index mcp-serve` gives agent-neutral read-only repo intelligence by
  default.
- `code_index graph-server` serves a browser graph with task submission,
  active-run oversight, run transcripts, quiet SSE updates, and cancellation.
- `code_index context` builds bounded handoff packets from repo-map, selected
  graph nodes, FTS matches, notes, and recent activity.
- `code_index agent transcript` and `code_index agent decision` expose an
  append-only audit trail for what the agent did and why.
- Completed/failed runs include post-run suggestions: diagnostics for touched
  files, affected pytest node ids, or a broader-test warning when no edges are
  known.
- `code_index agent-adapter --mode command` can launch a configured local
  Claude/Codex/other command and stream stdout/stderr back to the graph.
  Structured JSON or prefixed output lines become read/edit/test/status events.
- `plugins/code-index-agent/scripts/install_plugin.py` writes repo-local MCP,
  Claude settings, launcher config, starter scripts, and a demo task.
- `plugins/code-index-agent/scripts/start_graph_server.py --check-only`
  validates configured provider commands before the server starts.
- `CODE_INDEX_AGENT_PROVIDER=claude` and `CODE_INDEX_AGENT_PROVIDER=codex`
  provide built-in local command presets; `CODE_INDEX_AGENT_COMMAND` remains
  the escape hatch for other systems.
- `plugins/code-index-agent` packages the MCP config, skill instructions, and
  graph-server launcher for repo-local plugin use.

## Gaps Before A Wider Release

1. Durable process registry.
   In-memory cancellation is correct for the current graph-server process. A
   production daemon should persist active run metadata so server restarts can
   mark orphaned runs stale.

2. Public marketplace metadata.
   The repo-local plugin has install scripts, screenshot assets, and demo
   onboarding. A public plugin still needs final repository/homepage URLs,
   release versioning, and publish credentials.

3. Hosted collaboration model.
   Graph-server is intentionally local-first. Multi-user teams would need
   auth, persistence, and conflict semantics beyond the current local browser
   flow.

## Highest-Value Next Improvements

1. Add event filters and search within browser run transcripts.
2. Add CI/provider health cards in the graph header.
3. Add hosted auth/persistence for teams that want shared oversight.
4. Publish marketplace metadata with real project URLs and release notes.
5. Add a guided first-run wizard in the browser.
