# code_index project memory

Goal:
Build a local-first hybrid code-memory system for coding agents.
Symbols, occurrences, and relations are the semantic spine.
Chunks are retrieval/context-packing projections only.

Architecture rules:
- `symbol_uid` is the primary semantic identity. It is a *deterministic
  declaration key* derived from `language`, `kind`, `canonical_name`,
  `signature_norm`, and `container_uid`. It is stable across re-parses of
  the same declaration but is NOT refactor-durable — it changes when the
  canonical name, signature, container, or kind changes. Use
  `code_index update --rename-map file.json` to migrate identity across
  explicit refactors (preserves `symbol_pk` and downstream FK references).
- `chunk_uid` is secondary.
- Prefer semantic source order:
  1. native semantic indexer / SCIP emitter / compiler or language-server-backed source
  2. Tree-sitter
  3. Universal Ctags JSON
  4. heuristic text chunker
- SQLite is the local control plane, not the only search path.
- For literal/path/regex search, use a lexical fast path first.
- Structural search is first-class.
- Tree-sitter is for syntax, AST querying, chunking, and fallback, not the highest-authority semantic source when better sources exist.
- Keep v1 lineage focused on files, types, functions, and methods.
- Use one shared pipeline for init/update/watch.
- JSON-first CLI output.

Working rules:
- Inspect the actual repo before locking runtime, parser, or dependency choices.
- Prefer the existing repo's dominant language/tooling unless there is a strong reason not to.
- Implement thin vertical slices, not a big-bang rewrite.
- Run targeted verification before claiming completion.
- Update docs when commands, schema, or workflow change.
- Be explicit about unsupported languages or partial implementations.

Expected CLI surface:
- `code_index init`
- `code_index update [--files ...]`
- `code_index watch`
- `code_index grep`
- `code_index symbol`
- `code_index query`
- `code_index impact`
- `code_index tests`
- `code_index doctor`
- `code_index mcp-serve`
- `code_index branch {list,diff,files,impact,compare}` — git branch comparison with index cross-reference
- `code_index workspace {init,add,remove,list,status,query,graph}` — multi-repo workspace management

Architecture notes:
- Command modules can export `register_parser(subparsers)` so `cli_parser.py` stays
  slim as the surface grows. `branch_cmd` and `workspace_cmd` already use this
  pattern; older commands will migrate over time.
- The graph HTTP server uses a lightweight `Router`/`Route` abstraction
  (`commands/graph_server_router.py`) instead of long if/else chains. Routes match
  method + path with `{param}` extraction and are resolved in registration order.

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `mfethe1/memory-network`. See `docs/agents/issue-tracker.md`.

### Triage labels

The canonical triage label strings are used unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo with `CONTEXT.md` at the root. See `docs/agents/domain.md`.
