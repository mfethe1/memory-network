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
`ask`, `mcp-serve`, `import-scip`, and `scip-python-index` are live. See
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

## Architecture (this slice)

```
          ┌──────────────────────────────────────────────────────┐
          │                     cli.py (argparse)                │
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
                  Reserved       →  embeddings · test_edges · repo_map_snapshots · commits · file_versions

          Search fan-out:
              grep    → search/lexical.py  (ripgrep ▸ Python re fallback)
              symbol  → search/symbol_search.py (symbols/occurrences join)
              query   → search/fts.py  (weighted BM25 over chunks_fts)
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
- **No `watch` command.** The shared `pipeline.reindex()` entrypoint is
  already in place; `watch` just needs a debounced dispatcher.
- **No `impact` / `tests` commands.** The `relations` and `test_edges` tables
  exist in the schema but only `contains` is populated so far.
- **No `mcp-serve` command.** Tools/resources/prompts surface is designed in
  `plans/code-index-repo-plan.md` §12.
- **No git hook installer yet.** Hooks directory is reserved; installation
  via `core.hooksPath` lands next.
- **No embeddings retrieval.** Table is reserved; v1 explicitly keeps
  embeddings off the hot path.
- **Call graph / override / implements relations not yet extracted.**
- **Inner-block chunks are de-scoped in v1** per the spec; oversized
  functions are not split.

## Testing

```bash
python -m pytest tests/
```

19 tests covering hashing stability, ignore rules, Python AST extraction,
pipeline upsert/tombstone/rewrite semantics, and CLI smoke. Tests run with
the Python stdlib only.

## Repo layout

```
code_index/           core package
  cli.py              argparse entrypoint
  pipeline.py         shared init/update/watch pipeline
  db.py               sqlite + schema application
  schema.sql          full schema (spine + projection + reserved)
  ignore.py           gitignore-ish matcher
  scanner.py          file discovery
  symbols.py          symbol_uid / chunk_uid
  hashing.py          raw + normalized content hashes
  parsers/            per-language adapters + registry
  search/             lexical, FTS, symbol search
  commands/           one file per subcommand
tests/                pytest suite
plans/                implementation plan
docs/                 authoritative spec + long context
```

## License

MIT.
