---
name: code-index
description: >-
  Use this skill whenever the task involves exploring, understanding, or
  modifying code in this repository. Covers symbol lookup, structural
  (tree-sitter) queries, literal/regex search via ripgrep, ranked retrieval
  (FTS5 with BM25), blast-radius / impact analysis via the call graph,
  affected-tests discovery (direct and transitive), index health, and
  surgical reindexing after edits. Prefer these commands over raw
  Read/Grep/Glob for questions about "where", "who calls", "what tests", or
  "what breaks if I change" a symbol.
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash(code_index *)
  - Bash(python -m code_index *)
---

# code_index operational playbook

This repository ships a local-first hybrid code-memory system. Use it as the
default layer before falling back to ad-hoc grep or file browsing.

## 1. Classify the request first

Map the user's intent to one (or a few) of these verbs — then pick a command.

| Intent | Command |
|---|---|
| "Where is `X`?" | `code_index symbol X --json` |
| "Show call sites of `X`" | `code_index symbol X --references --json` |
| "Who calls `X` / what breaks if I change `X`?" | `code_index impact X --json` |
| "Which tests exercise `X`?" | `code_index tests X --json` |
| "Give me pytest node ids for `X`" | `code_index tests X --runner pytest` |
| "Find every `class` / `function` / `call` / `import`" | `code_index query --ast NAME` |
| "Find this string / regex / path glob" | `code_index grep PATTERN [--path GLOB]` |
| "Explain code similar to `phrase`" | `code_index query "phrase" --limit N` |
| "Is the index healthy?" | `code_index doctor --json` |
| "FTS looks wrong / drift" | `code_index rebuild-fts` |
| "I just edited files" | `code_index update --files PATH...` (the PostToolUse hook does this automatically — only run manually for batch edits, git ops, or reverts) |
| "Watch and reindex live" | `code_index watch [--debounce-ms 250]` |
| "Expose the index to an MCP client" | `code_index mcp-serve` (stdio); `--describe` for a JSON surface |
| "Wire git hooks for this repo" | `code_index install-hooks` (sets `core.hooksPath` to `.code_index/hooks`) |

## 2. Symbol-first and relation-aware workflows

- `symbol_uid` is the durable identity (see `code_index/symbols.py`). Prefer
  it when passing a symbol handle between steps. `code_index tests UID`
  accepts a 20-hex `symbol_uid` directly.
- `impact` traverses inbound `calls`, `inherits`, `contains`, and optionally
  `imports` edges. Depth defaults to 2; increase with `--max-depth`.
- `tests` is backed by a materialized `test_edges` table built via BFS over
  `calls`. Each edge carries `edge_type` (direct | transitive), `depth`,
  `confidence`, a `path` chain of canonical names, and a `parametrize`
  field when the test carries `@pytest.mark.parametrize`.
- `tests --runner pytest` formats affected tests as pytest node ids and
  expands captured literal `parametrize` cases. `--runner-json` also returns
  a `skipped_tests` list for non-literal or truncated cases.
- `symbol --references --json` includes up to 50 resolved call-site
  occurrences (`role="reference"`, `syntax_kind="call"`) with file and line
  spans.
- Relative imports (`from . import X`, `from ..pkg import Y`) resolve
  against the parsing file's package path; no manual linkage needed.
- Dead edges heal: when a target symbol is tombstoned, the edge moves into
  `unresolved_calls` and the backfill step repairs it on a later reindex if
  a same-canonical-name symbol reappears.
- `mcp-serve` exposes the index as an MCP server over stdio. Tools cover
  `search_text`, `search_query`, `search_ast`, `find_symbol`, `impact`,
  `affected_tests`, `doctor`, `update`, `rebuild_fts`. Resources cover
  `codeindex://repo-map`, `codeindex://doctor`,
  `codeindex://symbol/<canonical>`, `codeindex://chunk/<chunk_uid>`.

## 3. Refactor workflow

Before a non-trivial refactor:

1. `code_index impact SYMBOL --json` — read `impacted_symbols`,
   `impacted_files`, and `limitations`.
2. `code_index tests SYMBOL --json` — record the transitive test list so you
   know what must stay green.
   Use `code_index tests SYMBOL --runner pytest` when you want a command-ready
   pytest node-id list.
3. Make the edit. The PostToolUse hook reindexes touched files, which also
   triggers the backfill pass — previously unresolved cross-file calls
   resolve automatically if the missing symbol just landed.
4. Re-run the test list from step 2. Surface any divergence.

## 4. Falling back gracefully

- If an optional dep is missing, prefer the degraded-but-working path and
  say so:
  - `query --ast` requires `tree-sitter` + `tree-sitter-python`. If not
    installed, fall back to `query "phrase"` (FTS5).
  - `grep` works either way — `rg` is preferred; otherwise a `python-re`
    engine kicks in. `doctor` reports which one you'll get.
- If `code_index` is not on PATH, invoke with `python -m code_index`.
- The Claude Code PostToolUse hook handles Edit, Write, and MultiEdit payloads
  and batches surviving paths into one `update --files` call.

## 5. Commands run, evidence found, limitations

When you report results, quote the exact command and the fields that matter
(not the full JSON). Example:

> `code_index impact "apply_schema"` returned `summary.direct_callers=3`,
> impacted files include `code_index/commands/init_cmd.py:14`.

Then include any `limitations` the command returned so the user can judge
precision.

## 6. Schema / architecture pointers

- Durable spine: `files`, `symbols`, `occurrences`, `relations`,
  `diagnostics`, `unresolved_calls`.
- Retrieval projection: `chunks`, `chunks_fts` (external-content FTS5),
  `chunk_edits`, `chunk_lineage`, `test_edges`.
- Shared pipeline: `code_index/pipeline.py::reindex`. `init`, `update --files`,
  and `watch` all flow through it.
- Authoritative spec: `docs/code-index-spec.md`.
- Implementation plan: `plans/code-index-repo-plan.md`.

## 7. Known limitations (surface when relevant)

- Dynamic attribute calls (`getattr`, `__getattr__`) aren't resolved.
- Typed-instance method calls aren't resolved — only `self`/`cls` and
  `ClassName.method()` inside that class's own body.
- Global rename inference is NOT done. If a symbol is renamed, existing
  edges to the old name go unresolved until the caller file is reparsed or
  a file reintroduces the old name.
- Parametrized test cases still produce one `test_edges` row per test symbol.
  `tests --runner pytest` expands only captured literal cases; non-literal
  cases are reported in `skipped_tests`, and truncated decorators only emit
  captured cases.
- Call-site references are emitted for resolved `calls` relations only, not
  `imports`, `inherits`, or `contains`.
- Non-Python languages fall through to a heuristic chunker (FTS/grep still
  work; `symbol`, `impact`, `tests` do not apply).
- Embeddings retrieval is reserved (schema only).
- `parametrize(ids=...)` is captured only when `ids` is a literal list.
  Callable `ids=` is flagged (`ids_callable=true`), and the pytest-runner
  formatter falls back to value-based ids plus a `skipped_tests` note so
  downstream consumers know they may diverge from pytest's collection-time
  values.
