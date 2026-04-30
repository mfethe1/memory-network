# code_index

Local-first hybrid code-memory for coding agents. The durable spine is
**symbols, occurrences, and relations**. Chunks are a retrieval projection on
top, not the primary identity layer.

Design authority: [`docs/code-index-spec.md`](docs/code-index-spec.md).
Repo-specific implementation choices: [`plans/code-index-repo-plan.md`](plans/code-index-repo-plan.md).
Project-memory summary: [`CLAUDE.md`](CLAUDE.md).

## Status

First working vertical slice. `init`, `update`, `grep`, `symbol`, `query`,
`doctor`, `watch`, `impact`, `tests`, `repo-map`, `embed`, `similar`,
`ask`, `context`, `graph`, `graph-server`, `agent`, `agent-adapter`,
`mcp-serve`, `import-scip`, and `scip-python-index` are live. See
[Known Limitations](#known-limitations).

## Quick start

Requires Python **3.10+**. No external Python deps are required for the core
CLI; optional deps unlock additional features (see below).

```bash
# Index the current repo (creates .code_index/ with index.db)
python -m code_index init

# Refresh the index (touched files only)
python -m code_index update --files path/to/changed.py

# Or rescan the whole tree
python -m code_index update --all

# Durable-identity symbol lookup
python -m code_index symbol reindex

# Ranked FTS5 retrieval over chunks
python -m code_index query "chunk upsert" --limit 5

# Task-aware handoff packet for coding agents
python -m code_index context "fix graph navigation" --path code_index/commands/graph_script.py --json

# Interactive file graph with importance and care guidance.
# HTML output also writes .code_index/repo-graph.json as a refresh sidecar.
python -m code_index graph --output .code_index/repo-graph.html

# Keep the graph artifact current while agents work (pair with code_index watch)
python -m code_index graph --watch --output .code_index/repo-graph.html

# Serve the graph with live SSE refreshes and durable node notes.
# Node notes sync to .code_index/graph-notes.json.
python -m code_index graph-server --port 8767

# Record agent activity so the graph can show live movement before reindexing
python -m code_index agent start --agent-name Codex --prompt "refactor graph"
python -m code_index agent event --type edit --file code_index/commands/graph_cmd.py --message "updating graph"
python -m code_index agent decision --message "Keep graph refresh SSE-only for agent events"
python -m code_index agent transcript --run-id <run-id> --json
python -m code_index agent recent
python -m code_index agent claim --run-id <run-id> --file code_index/commands/graph_cmd.py --mode edit
python -m code_index agent verify-claim --run-id <run-id> --file code_index/commands/graph_cmd.py --fence <token>
python -m code_index agent claims --json
python -m code_index agent board --json
python -m code_index agent release --run-id <run-id> --file code_index/commands/graph_cmd.py

# Exercise the graph task callback path without launching a real coding agent
python -m code_index agent-adapter --task-json .code_index/sample-task.json --json

# Run a submitted task through a local agent command and stream stdout/stderr
python -m code_index agent-adapter --mode command \
  --task-json .code_index/sample-task.json \
  --command 'claude -p {provider_prompt}' --json

# Start the repo-local plugin launcher for Claude/Codex/other command adapters
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
python plugins/code-index-agent/scripts/start_graph_server.py --root . --port 8767 \
  --provider codex

# Import a SCIP semantic index exported as JSON
python -m code_index import-scip --json-index index.scip.json

# Or generate a Python SCIP sidecar index, then import the raw index.scip
python -m code_index scip-python-index --project-name my-project
python -m code_index import-scip --from .code_index/external/scip-python/index.scip

# Literal / regex fast path (ripgrep when available, Python re fallback)
python -m code_index grep "BM25" --ignore-case

# Coverage + optional-dep report
python -m code_index doctor
```

Every subcommand accepts `--json` for machine-readable output. The JSON shape
is the stable interface for agents; human output is unstable across versions.

## Live code graph

`code_index graph` writes a standalone HTML/JSON artifact for review. The HTML
shows a tree navigator, graph nodes, recent agent/file edits, node summaries,
embedded source where allowed, a Chat tab for graph-scoped agent tasks, and a
notes tab that exports agent-task JSON.

`code_index graph-server` is the interactive local mode. It serves
`/repo-graph.html`, `/repo-graph.json`, `/notes.json`, `/api/search`, and
`/events`. The browser receives Server-Sent Events when agent activity or notes change. Agent
events update the active/recent files, run list, and inspector in place; full
graph JSON refreshes are reserved for graph or note data changes. Saved node
notes are durable in
`.code_index/graph-notes.json`, and note saves are also recorded as
`agent_events` so the recent activity panel can show user guidance next to
agent work.

Agents can write activity through the CLI, MCP mutating tools when explicitly
enabled, or `POST /api/agent-events` on the graph server. The current graph
uses this to highlight active files and the last edited files before the index
has been refreshed.

When served through `graph-server`, the Chat tab can submit a task for the
selected node and choose the configured adapter, Codex CLI, Claude CLI, or Kimi
Code CLI per message. The Notes tab keeps the same submit/export path for saved
guidance.
Browser task submission now runs as draft -> preflight -> dispatch:
`POST /api/agent-task-preflight` builds the normalized task draft, graph
context, runtime retrieval policy, care warnings, and active file-claim
overlap warnings without creating a run. If the preflight requires
confirmation, the browser asks for a second submit before dispatching.
`POST /api/agent-runs` records a queued run, returns an `/api/agent-events`
callback URL, and dispatches the task when an adapter is configured. Set
`CODE_INDEX_AGENT_WEBHOOK_URL` to send task JSON to an HTTP webhook, or set
`CODE_INDEX_AGENT_COMMAND` to launch a local command adapter directly from the
graph server. You can also set
`CODE_INDEX_AGENT_PROVIDER=claude`, `CODE_INDEX_AGENT_PROVIDER=codex`, or
`CODE_INDEX_AGENT_PROVIDER=kimi` for built-in local presets. The Kimi preset
uses non-interactive stream JSON mode, thinking mode, one Ralph iteration, and a
per-run MCP config that exposes `code_index mcp-serve` to the agent. Each
submitted task includes a bounded
`context_packet` with repo-map, selected files/nodes, matching chunks, graph
notes, and recent agent activity. The command adapter streams stdout/stderr
back as graph events and marks the run completed or failed from the process
exit code.
Useful command placeholders are `{message}`, `{provider_prompt}`, `{run_id}`,
`{root}`, `{task_json}`, `{selected_paths}`, and `{selected_nodes}`.

A webhook or command adapter should post read/edit/test/status/decision events
back to the callback URL so the browser can show active files and run status
without a full page reload. Command adapters also parse structured provider
output: JSON lines with `event_type`/`type`, optional
`CODE_INDEX_EVENT {...}` lines, or prefixed lines like `EDIT path/to/file.py
message`. Set `CODE_INDEX_AGENT_WEBHOOK_TOKEN` to add a bearer token on
outbound HTTP dispatches. Set `CODE_INDEX_GRAPH_TOKEN` when you want graph
server reads, browser POSTs, and adapter callbacks to require bearer auth.
The server prints a clean browser URL; the browser creates an httpOnly
same-origin session after token entry, and API or adapter clients can send
`Authorization: Bearer <token>`. Optional command limits:
`CODE_INDEX_AGENT_COMMAND_TIMEOUT`, `CODE_INDEX_AGENT_MAX_OUTPUT_EVENTS`, and
`CODE_INDEX_AGENT_CONTEXT_BUDGET`.

`POST /api/agent-runs/<run_id>/cancel` records a terminal cancellation status
for queued or working runs. For local command adapters, cancellation also
signals the active adapter and interrupts the spawned process tree before the
adapter posts a final `cancelled` status. The browser uses agent-specific SSE
events for run/activity updates and reserves full graph refreshes for graph or
note data changes. `code_index agent-adapter` also keeps a dry-run mode: give
it a task JSON payload and it posts working/read/test/completed events back to
the task's callback URL. `GET /api/agent-runs/<run_id>` and
`code_index agent transcript --run-id <run_id> --json` return the chronological
run transcript and decision ledger.

`GET /api/debug` returns a compact graph-server health snapshot for humans and
agents: graph build time, payload size, node/edge counts, embedded-code bytes,
index file stats, active run/claim counts, dispatch configuration, and a
sanitized ops snapshot for auth failures, preflight rejections, claim
conflicts, SSE drops, stale runs, search latency, and retrieval-budget
readiness. The browser inspector includes a Debug tab that shows local
render/hydration metrics, renders ops cards, consumes live `perf:tick` SSE
updates, and can fetch this server snapshot.

The top search box filters the visible graph locally and, when served by
`graph-server`, also queries `/api/search` for indexed file/chunk matches and
agent transcript events. `/api/search`, `doctor --eval-retrieval`, and the MCP
`retrieval_broker` tool share the same broker contract for file paths, code
chunks, and transcript events. Search results appear in the navigator and can
jump to a file node or open the matching run transcript.

The graph renderer now draws only the visible subgraph instead of rendering the
whole repository and dimming hidden nodes. The `Layered context` view lays out
the selected node by neighborhood distance, and the hop controls expand or
collapse the selected context up to three hops. This keeps large repos
interactive while giving agents a staged view of "current file -> direct
neighbors -> broader impact" rather than an undifferentiated graph.

File coordination is first-class. Read/edit/test events automatically create
short-lived file claims, browser-submitted tasks claim selected paths in review
mode, and terminal run status releases active claims. `GET /api/file-claims`
lists active claims; `POST /api/file-claims` can claim or release paths for a
run. Claims are included in SSE activity snapshots, graph context, collaboration
packets, context packets, and the browser sidebar so overlapping agents can see
coordination risk before editing.

Hooks and command adapters can enforce leases before supervised writes with
`python -m code_index agent verify-claim --run-id <run-id> --file <path>
--fence <token>`. The command succeeds only for a current edit or exclusive
claim with a matching fence token and reports missing, stale, expired, or
conflicting claims with hook-friendly error text. Read-only claims remain
non-blocking. The included Claude Code PreToolUse hook
`.claude/hooks/verify-claim-before-edit.sh` is opt-in: set
`CODE_INDEX_AGENT_RUN_ID` plus `CODE_INDEX_AGENT_FENCE` for one file or
`CODE_INDEX_AGENT_FENCES` as a JSON map of repo-relative paths to fence tokens.

Task blockers are first-class too. Graph task payloads can include
`blocked_by_run_ids`; blocked tasks are recorded without dispatching and move
from the graph sidebar's blocked column to ready when their blocker runs
complete. `GET /api/agent-board` and `code_index agent board --json` expose the
same blocked/ready/active/review/done projection for adapters.

Completed or failed runs automatically attach post-run suggestions to the
transcript: parser diagnostics for touched files, affected pytest node ids when
known, and a broader-test warning when no test edges are available.

The repo-local plugin package lives in `plugins/code-index-agent`. It includes
a Codex plugin manifest, MCP server config, a skill playbook, demo assets, an
installer, and a cross-platform graph-server launcher. The installer writes
repo-local `.mcp.json`, `.claude/settings.local.json`, `.code_index` launcher
config, starter scripts, and a demo task:

```bash
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
```

The launcher validates configured provider executables before starting unless
`--skip-provider-check` is set. The current value/readiness assessment is in
`docs/agent-plugin-assessment.md`.

## Architecture (this slice)

```
          ┌──────────────────────────────────────────────────────┐
          │          cli.py dispatch + cli_parser.py (argparse)  │
          └──────────────────────────────────────────────────────┘
                                    │
          ┌──────────────────────────────────────────────────────┐
          │                   pipeline.reindex()                 │  one shared
          │  used by: init, update --files, update --all, watch  │  entrypoint
          └──────────────────────────────────────────────────────┘
            │           │             │            │          │
         scanner    parsers.*   hashing.py   symbols.py   db.py
         (ignore)   (registry)  (raw+norm)   (symbol_uid) (sqlite + FTS5)

                  Durable spine  →  files · symbols · occurrences · relations · diagnostics
                  Projection     →  chunks · chunks_fts · chunk_edits · chunk_lineage
                  Activity       →  agent_runs · agent_events
                  Reserved       →  embeddings · test_edges · repo_map_snapshots · commits · file_versions

          Search fan-out:
              grep    → search/lexical.py  (ripgrep ▸ Python re fallback)
              symbol  → search/symbol_search.py (symbols/occurrences join)
              query   → search/fts.py  (weighted BM25 over chunks_fts)
              graph   → commands/graph_cmd.py + graph_model/html/server helpers
              agent   → agent_activity.py + commands/agent_cmd.py
              mcp     → retrieval_broker/code_graph/graph_context/agent_activity tools + codeindex://graph resources
```

### Parser priority

Per the spec's non-negotiable order:

1. **Native semantic source** — Python stdlib `ast` (confidence 0.95) owns
   Python files.
2. **Tree-sitter** — scaffold present, disabled in v1; becomes the general
   fallback once `pip install code-index[tree-sitter]` is resolved.
3. **Universal Ctags JSON** — detection only in `doctor`; extraction slated
   for the next slice.
4. **Heuristic text chunker** — one `file` chunk per file, no symbols. Still
   indexed by FTS5 and grep.

### Identity

`symbol_uid` is derived from `(language, kind, canonical_name,
signature_norm, container_uid)` and intentionally does not depend on file
path or line numbers. Functions moved across files retain identity.
Whitespace-only edits do not change `normalized_hash`, so unchanged
chunks stay unchanged.

`chunk_uid` is secondary and scoped per file + chunk type + primary symbol.

### SQLite posture

WAL, foreign keys on, busy_timeout=5000, synchronous=NORMAL. `PRAGMA optimize`
runs on every connection close. FTS5 uses external-content over `chunks`;
triggers keep it consistent on insert/update/delete, and a future
`rebuild-fts` command will be exposed for drift.

## Optional dependencies

Install on demand; the core CLI keeps working without them.

| Extra | Command | Unlocks |
|---|---|---|
| `tree-sitter` | `pip install code-index[tree-sitter]` | structural `query --ast`, broader language coverage |
| `watch` | `pip install code-index[watch]` | filesystem watcher for `code_index watch` |
| `mcp` | `pip install code-index[mcp]` | `code_index mcp-serve` |
| `dev` | `pip install code-index[dev]` | pytest, coverage |

`code_index doctor` reports which extras are installed and whether `ripgrep`
and `ctags` are on PATH.

External code-intelligence tools are optional sidecars:

| Tool | Install | Unlocks |
|---|---|---|
| `scip-python` | `npm install -g @sourcegraph/scip-python` | `code_index scip-python-index` writes `.code_index/external/scip-python/index.scip` |
| `scip` | Install from the SCIP releases | `import-scip --from index.scip` and `scip-python-index --import-index` |

## Known limitations

Tracked as TODOs rather than silent gaps:

- **SCIP ingestion is an optional semantic sidecar.** `import-scip --json-index`
  can ingest `scip print --json` output directly. Raw `index.scip` import
  requires the `scip` CLI, and Python SCIP generation requires `scip-python`.
  The stdlib AST parser remains the zero-dependency fallback.
- **No Tree-sitter extraction.** Adapter is a scaffold; `query --ast`
  currently returns a clear error telling you what to install.
- **No Universal Ctags extraction.** Detection is live in `doctor`; the
  adapter is scaffolded only.
- **Graph-server is local-first.** It supports SSE, durable notes, and local
  POST adapters, but it is not yet a hosted multi-user collaboration service.
- **Provider routing is adapter-level.** The graph can export node task JSON
  and record activity, but choosing Codex vs Claude still belongs in a thin
  orchestration/webhook layer above `code_index`.
- **Call graph / override / implements relations not yet extracted.**
- **Inner-block chunks are de-scoped in v1** per the spec; oversized
  functions are not split.

## Testing

```bash
python -m pytest tests/
```

315 tests cover hashing stability, ignore rules, Python AST extraction,
pipeline upsert/tombstone/rewrite semantics, graph notes/activity, MCP auth,
schema repair, and CLI smoke. Tests run with the Python stdlib only.

## Repo layout

```
code_index/           core package
  cli.py              thin CLI dispatch
  cli_parser.py       argparse command surface
  pipeline.py         shared init/update/watch pipeline
  db.py               sqlite + schema application
  schema.sql          full schema (spine + projection + reserved)
  agent_activity.py   graph-facing agent run/event records
  ignore.py           gitignore-ish matcher
  scanner.py          file discovery
  symbols.py          symbol_uid / chunk_uid
  hashing.py          raw + normalized content hashes
  parsers/            per-language adapters + registry
  search/             lexical, FTS, symbol search
  commands/           subcommands plus graph/MCP helper modules
tests/                pytest suite
plans/                implementation plan
docs/                 authoritative spec + long context
```

## License

MIT.
