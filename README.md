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
`ask`, `graph`, `graph-server`, `agent`, `mcp-serve`, `import-scip`, and
`scip-python-index` are live. See [Known Limitations](#known-limitations).

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
python -m code_index agent recent

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
embedded source where allowed, and a notes tab that exports agent-task JSON.

`code_index graph-server` is the interactive local mode. It serves
`/repo-graph.html`, `/repo-graph.json`, `/notes.json`, and `/events`. The
browser receives Server-Sent Events when agent activity or notes change, then
refreshes the graph data without a page reload. Saved node notes are durable in
`.code_index/graph-notes.json`, and note saves are also recorded as
`agent_events` so the recent activity panel can show user guidance next to
agent work.

Agents can write activity through the CLI, MCP mutating tools when explicitly
enabled, or `POST /api/agent-events` on the graph server. The current graph
uses this to highlight active files and the last edited files before the index
has been refreshed.

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
              mcp     → code_graph/agent_activity tools + codeindex://graph resources
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

255 tests cover hashing stability, ignore rules, Python AST extraction,
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
