# code_index — Repo-Specific Implementation Plan

_Authoritative plan for the first real, usable slice of `code_index` in this repository._

## 1. Repo Reality Check

Before this slice, the repo contained only:

- `CLAUDE.md` (project-memory summary)
- `docs/code-index-spec.md` (the authoritative spec, three nested revisions)
- `docs/long_context.md` (duplicate of the spec)
- `.claude_context_tree` (auto-generated file tree stub)

No lockfiles, no build files, no CI config, no existing source. That means:

- There is no dominant existing language to defer to.
- There is no existing test harness to integrate with.
- There is no existing CLI framework or runtime to align with.
- The repo does not constrain the implementation choice. The choice must be justified by the spec, not by existing tooling.

## 2. Runtime and Language Decision

**Chosen runtime: Python 3.10+**

Reasoning, grounded in the spec's non-negotiables:

| Spec requirement | Python fit |
|---|---|
| SQLite + FTS5 | `sqlite3` is stdlib; FTS5 is built into CPython's bundled SQLite on modern builds (verified: 3.12.7 on this machine). |
| Tree-sitter queries | Mature `tree-sitter` + `tree-sitter-languages` Python bindings exist; optional dep. |
| Native semantic source for at least one language | `ast` stdlib module gives a compiler-backed parse tree for Python with zero deps. That lets the slice prove the symbol-first architecture immediately. |
| Universal Ctags JSON fallback | Subprocess call, language-agnostic; Python is fine. |
| Ripgrep fast path | Subprocess call, language-agnostic. `rg` is on PATH (14.1.1). |
| Git hooks | Shell scripts that invoke the CLI; language-agnostic. |
| MCP server | Python SDK (`mcp`) is first-party. |
| JSON-first CLI | `json` stdlib, `argparse` stdlib. |
| Cross-platform (repo is on Windows) | Python runs on Windows without toolchain. |

Rust/Go would ship a faster binary but slow the first slice dramatically for no spec-required benefit.

## 3. Dependency Posture

**Hard dependencies: none beyond the Python stdlib.**

The first slice must install and run with just `python -m code_index` after cloning. No `pip install` should be required to see `init`, `update`, `grep`, `symbol`, and `doctor` work against Python source.

**Optional dependencies (loaded at runtime, guarded):**

- `tree-sitter` + `tree-sitter-languages` — structural search (`query`) and non-Python language support.
- `watchdog` — `watch` mode.
- `mcp` — `mcp-serve` command.

These are reserved in `pyproject.toml` optional-extras. When absent, the commands that need them fail with a clear install hint; commands that don't need them still work.

## 4. Schema / Migrations

The schema follows the spec's layered model:

**Semantic spine (durable identity):**
- `files` — one row per file; `head_blob_oid`, `worktree_hash`, `is_tracked`, `is_dirty` instead of a single ambiguous `git_oid`.
- `symbols` — `symbol_pk` (integer) + `symbol_uid` (stable text hash); kind, canonical name, container, signature_norm, semantic_source, confidence, first/last_seen_commit, tombstone.
- `occurrences` — symbol occurrences in files (definition, reference, import, etc.).
- `relations` — `src_symbol_pk → dst_symbol_pk` with `relation_kind` (calls, overrides, imports, contains, implements).
- `diagnostics` — parser/lint diagnostics per file.

**Retrieval projection:**
- `chunks` — `chunk_pk` + `chunk_uid`, `primary_symbol_pk` (nullable), content, raw_hash, normalized_hash, tombstone.
- `chunks_fts` — external-content FTS5 over `chunks`, columns weighted so `symbol_name`/`signature` rank above `content`/`file_path`.
- `chunk_edits` — append-only audit of chunk changes.
- `chunk_lineage` — parent/child edges for rename/move/split/merge; populated conservatively in v1.

**Reserved (schema only):**
- `embeddings` — kept out of hot tables.
- `test_edges` — will be populated once test-discovery lands.
- `repo_map_snapshots` — reserved for Aider-style repo maps.
- `commits`, `file_versions` — reserved for deeper history tracking.

Schema lives in a single `code_index/schema.sql` and is applied idempotently via `CREATE TABLE IF NOT EXISTS`. A `schema_meta` table records the schema version so future migrations can detect drift.

SQLite pragmas set on every connection: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`, `temp_store=MEMORY`. `PRAGMA optimize` is invoked on connection close; `rebuild-fts` is an explicit maintenance command.

## 5. Parser / Indexer Strategy

Per the spec's priority order:

1. **Native semantic source (Python):** use stdlib `ast` with full-fidelity symbol extraction. Marked `semantic_source="python-ast"`, `confidence=0.95`. This is the v1 anchor.
2. **Tree-sitter:** adapter exists but is lazy-loaded. When `tree-sitter-languages` is installed, it handles any language with a bundled grammar. Marked `semantic_source="tree-sitter:<lang>"`, `confidence=0.75`.
3. **Universal Ctags:** subprocess wrapper, enabled if `ctags --output-format=json --version` succeeds. Marked `semantic_source="ctags"`, `confidence=0.55`. Provides symbol names/kinds but no full structural lineage.
4. **Heuristic fallback:** one chunk per file, kind `"file"`. No symbols created. Marked `semantic_source="heuristic"`, `confidence=0.10`. Still searchable via FTS5 and grep.

A `ParserRegistry` chooses by extension + available parser. Every file records `parse_status` (`ok`, `empty`, `skipped`, `binary`, `failed`) and `parse_error`.

**v1 lineage scope:** files, classes, functions, methods. No inner-block chunks. `chunk_type` is drawn from: `module`, `class`, `function`, `method`.

## 6. Shared Pipeline

`init`, `update`, `watch` all go through `pipeline.reindex(paths)`:

1. Resolve paths → respect ignore rules.
2. Hash file content. If unchanged since last `files.worktree_hash`, skip.
3. Pick parser, parse, collect `ParsedFile` (symbols + chunks + diagnostics).
4. Inside one DB transaction per file:
   - Upsert `files` row.
   - Diff old vs new symbols by `symbol_uid`; tombstone removed, upsert added/changed.
   - Diff old vs new chunks by `chunk_uid`; tombstone removed, upsert added/changed.
   - Append `chunk_edits` rows for each create/update/delete.
   - Rewrite `occurrences` for the file (cheaper than diffing for v1).
   - Insert diagnostics.
5. Commit.

The FTS index is kept in sync via triggers installed at schema-creation time (SQLite's recommended pattern for external-content FTS5 — `INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')` is exposed as a maintenance command too).

## 7. CLI Surface (v1)

Delivered now:

- `code_index init [--root PATH]` — create `.code_index/`, run full scan.
- `code_index update [--files PATH ...] [--all]` — targeted reindex.
- `code_index grep PATTERN [--path GLOB] [--lang LANG] [--json]` — ripgrep fast path with FTS-backed fallback when `rg` is unavailable.
- `code_index symbol NAME [--kind ...] [--json]` — symbol lookup by name/qualified-name/kind.
- `code_index query PATTERN [--lang LANG]` — structural search via Tree-sitter (returns a clear "tree-sitter not installed" error when optional dep missing).
- `code_index doctor [--json]` — coverage, parse failures, FTS consistency, drift, ignored-file stats, optional-dep status.

Reserved with clean `NotImplementedError` messages and exit code 2:

- `code_index watch`
- `code_index impact`
- `code_index tests`
- `code_index mcp-serve`

Every subcommand supports `--json` for machine output. Human output is stable but unspecified across versions.

## 8. Ignore Rules

- Always skip: `.git/`, `.code_index/`, `__pycache__/`, `.venv/`, `venv/`, `node_modules/`, `dist/`, `build/`, `target/`, `.tox/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.idea/`, `.vscode/`.
- Respect `.gitignore`: a lightweight pattern matcher in `ignore.py` (supports literal, `*`, `**`, leading `/`, trailing `/`, negation `!`).
- Skip binary: any file with a NUL byte in its first 8 KiB.
- Skip > 2 MiB files by default (config override).
- Honor `.code_index/config.json` `extra_ignore` and `include_hidden` keys.

## 9. Verification Strategy

Before declaring the slice done:

- Unit tests (pytest):
  - schema creates cleanly and is idempotent
  - ignore matcher handles gitignore patterns
  - Python AST parser extracts symbols/chunks correctly
  - symbol_uid and chunk_uid are stable under whitespace-only edits (normalized hash)
  - pipeline upserts and tombstones correctly
  - grep returns expected hits (fallback path)
- End-to-end smoke test:
  - Run `code_index init` against a fixture project → assert table populated.
  - Modify one file → `code_index update --files X` → assert new edit row, old chunk tombstoned.
  - `code_index symbol <name>` returns the right row.
  - `code_index doctor` returns a clean JSON report.
- Self-indexing smoke: `code_index init` against this repo itself, verifying the index scans its own source.

## 10. What This Slice Deliberately Does NOT Do Yet

Tracked as TODOs in README and doctored output:

- SCIP ingestion (massive scope; reserved).
- Multi-language tree-sitter coverage beyond the optional adapter scaffold.
- Universal Ctags integration (adapter stub only; the detection path in `doctor` is live).
- Git hook installation (hook scripts reserved under `.code_index/hooks/`).
- MCP server (`mcp-serve` stub; tools/resources/prompts surface is designed in docs).
- Watch mode (stubbed; `pipeline.reindex` is already the single-pipeline entrypoint so adding watchdog is local).
- Call graph / relation extraction for Python beyond `imports` and `contains` (reserved).
- Repo map / Aider-style global overview (reserved table).
- Test-edge discovery (reserved table).
- Embeddings.
- Adaptive pruning, impact analysis.
- Windows-specific long path handling beyond what stdlib gives for free.

## 11. File Layout Committed by This Slice

```
code_index/
  __init__.py
  __main__.py
  cli.py
  config.py
  db.py
  schema.sql
  ignore.py
  hashing.py
  pipeline.py
  scanner.py
  symbols.py
  chunks.py
  parsers/
    __init__.py
    base.py
    registry.py
    python_ast.py
    heuristic.py
    tree_sitter.py   # guarded optional import
    ctags.py         # guarded optional subprocess
  search/
    __init__.py
    lexical.py
    fts.py
    symbol_search.py
  commands/
    __init__.py
    init_cmd.py
    update_cmd.py
    grep_cmd.py
    symbol_cmd.py
    query_cmd.py
    doctor_cmd.py
    stub_cmds.py     # watch, impact, tests, mcp-serve
tests/
  conftest.py
  fixtures/sample/...
  test_hashing.py
  test_ignore.py
  test_python_ast.py
  test_pipeline.py
  test_cli.py
pyproject.toml
README.md
.gitignore
```

## 12. Next Highest-Leverage Step After This Slice

Add Tree-sitter adapter wiring and a structural `query` command backed by a small bundled query set for Python (classes, functions, imports, call sites). That converts the current symbol-first core into a true hybrid system on the retrieval side, and it is a small step from the Python-AST-only baseline because the chunk/symbol model is already correct.

## 13. Slice log

### Slice 2 — Tree-sitter structural + richer relations + impact (landed)
Tree-sitter Python adapter + bundled queries wired into `query --ast`.
Python-AST parser now emits `imports`, `calls`, `inherits` (in addition to
`contains`). Pending relations buffer at end-of-reindex, resolved via
exact + suffix match against the symbols table. `impact` replaced the stub
with a BFS over inbound edges. Robust `rg` discovery resolver with a full
trail in `doctor`.

### Slice 3 — Targeted-update convergence + tests + FTS maintenance (landed)
`unresolved_calls` table added; every reindex runs `_backfill_unresolved` so
`update --files A` resolves B's formerly-unresolved edges when the missing
symbol just landed in A. `test_edges` rebuilt via BFS with `depth` +
`path_json`; `code_index tests` now returns direct + transitive + rationale
chain. `code_index rebuild-fts` drops + recreates the external-content FTS5
table to prune tombstone drift; `doctor` flags rebuild recommendations using
the `chunks_fts_docsize` shadow table (the accurate signal — `COUNT(*)` on
the virtual table routes to chunks). `code_index watch` landed, routed
through `pipeline.reindex()`. `.claude/` native layers wired: `CLAUDE.md`,
path-scoped rules, `code-index` skill, `PostToolUse` reindex hook.

### Slice 4 — Python hardening: choice for this slice

**Repo inspection**: 49 `.py`, 6 `.md`, 1 `.toml`, 1 `.sql`. No TypeScript
or Rust present in any meaningful quantity. Per priority rule #3 in the
slice directive ("Only add second-language support if the repository
actually contains enough of that language to justify it"), this slice is
Python-hardening-only. Second-language work is deferred until a repo that
actually contains JS/TS or Rust consumes this index.

**Delivered**:
- **Relative-import resolution** — `from . import X`, `from .. import Y`,
  `from .sibling import Z` now resolve to the correct package-qualified
  canonical name based on the parsing file's module path.
- **Conservative rename backfill** — when a symbol is renamed (old name
  disappears, new name appears at the same file/line range), the
  resolver emits a `renames` provenance hint so unresolved call sites to
  the old name can repair on the next reindex of either file. Does not
  do global rename inference.
- **Method-call resolution in class bodies** — `cls.method()` and direct
  class-name references (`ClassName.method()`) inside the same file
  resolve without requiring `self`.
- **Watch/ignore hardening** — watch now reuses the pipeline ignore
  matcher, rejects the full binary/build/cache set, and explicitly
  filters `.code_index/` and `.git/` events so the index never indexes
  itself.
- **Parametrized test case identity** — `@pytest.mark.parametrize`
  decorators are parsed and their case count + a compact case
  identifier list are surfaced on `test_edges` and in `code_index tests
  --json`. Per-case dispatch would require pytest collection; we don't
  overbuild, we report the grouped representation.
- **`.claude/` sync** — `CLAUDE.md`, skill, rules, and `docs/claude-code.md`
  updated to reflect new capabilities and to drop claims the index never
  made.

**Not done** (explicit): second-language support (no motivating files in
repo), MCP server, embeddings retrieval, speculative type inference for
call resolution.

### Slice 6 — Critical gaps closed (Claude verification slice)

Research-driven sweep after the Codex rescue. Tavily/Firecrawl weren't
available in this session; I used `WebSearch` to verify best practices
against SCIP 2026, SQLite FTS5 external-content maintenance, Claude Code
hook conventions, and the MCP Python SDK, then closed four gaps:

- **`doctor.fts_consistency.ok` alignment** — now follows the rebuild
  recommendation (`not rebuild_recommended`) instead of `drift == 0`. A
  handful of tombstoned-but-still-indexed rows is benign because the
  retrieval layer filters via SQL. Regression-locked in
  `tests/test_rebuild_fts.py::test_ok_flag_aligns_with_rebuild_recommendation`.
- **`code_index install-hooks`** — writes post-commit, post-checkout,
  post-merge, post-rewrite scripts under `.code_index/hooks` and wires
  `git config core.hooksPath`. Idempotent; `--uninstall` reverses it.
  Each hook derives changed paths, filters the usual generated/binary
  set, and calls the shared pipeline via `python -m code_index update
  --files …`. Full test coverage in `tests/test_install_hooks.py`
  (skips when `git` isn't on PATH).
- **Custom `parametrize(ids=...)` capture** — literal list ids flow
  through `_extract_parametrize` and the pytest runner formatter
  emits them verbatim. Callable `ids=` is flagged (`ids_callable=true`)
  and the formatter falls back to value-based ids with a
  `skipped_tests` note. Tests in `tests/test_parametrize_ids.py`.
- **`code_index mcp-serve`** (stdio MCP server) — implemented against
  the official `mcp` Python SDK. Nine tools
  (`search_text`, `search_query`, `search_ast`, `find_symbol`, `impact`,
  `affected_tests`, `doctor`, `update`, `rebuild_fts`) plus four
  resources (`codeindex://repo-map`, `codeindex://doctor`,
  `codeindex://symbol/<canonical>`, `codeindex://chunk/<chunk_uid>`).
  `--describe` prints the full surface as JSON without starting the
  loop; that's the path the CLI test covers. Stdio loop itself is
  unverified by automated tests (would require an MCP client fixture
  or subprocess with a real JSON-RPC handshake — deferred).

**Research-driven decisions not to act on this slice:**
- `unresolved_calls_open` accumulates stdlib/external calls (~1325 open
  on a reindex of this repo). These are expected. No TTL / cleanup
  policy added; the count is informational, not a fault signal.
- `PRAGMA optimize` on connection close already matches 2026 SQLite
  guidance; no change.
- SCIP export would be interoperable but is a big commitment; deferred.
  Our `symbol_uid` grammar is file-local hash-based, not SCIP-compatible
  — changing that would ripple through every schema and test.
- Second-language expansion (TypeScript/Rust) skipped again; repo is
  still 100% Python.
- Embeddings retrieval: still reserved (schema only).

**Gate**: 113/113 tests green; `doctor.fts_consistency.ok` now correctly
`True` under small drift, `False` only when rebuild is recommended.

### Slice 6.1 — Tavily/Firecrawl research follow-up

Loaded API keys from `~/.openclaw/secrets/tavily.env` + `firecrawl.env`
and re-ran the research with the real services instead of the built-in
WebSearch. Findings that WebSearch had missed or underweighted:

1. **FastMCP is now in-SDK** (`mcp.server.fastmcp.FastMCP`, shipped with
   `mcp` 1.2+; we have 1.26.0). Confirmed via Firecrawl on the official
   `modelcontextprotocol/python-sdk` README. My initial server used the
   low-level `Server` decorators (~450 LOC). Refactored to FastMCP:
   ~320 LOC, URI templates handled natively
   (`codeindex://symbol/{canonical}`, `codeindex://chunk/{chunk_uid}`),
   and added `--transport {stdio,http,streamable-http}` for remote
   agent integrations. Inspection confirms 9 tools + 2 concrete
   resources + 2 resource templates register cleanly.
2. **Scalpel / PyCG** (arXiv 2022 / ICSE 2021) are the academic
   state-of-the-art for Python call-graph resolution. They provide
   flow-sensitive, cross-file, external-library-aware inference. Our
   `ast`-based scope resolver is a deliberate subset per the
   "no speculative type inference" rule. Documented as a deferred
   upgrade path; NOT adopted now (would conflict with our grounding
   rule and add a heavy dependency).
3. **2026 Claude Code hook conventions** (Pixelmojo + DEV). Confirms
   `Edit|Write|MultiEdit` matcher style is current. Two new
   optional patterns (`updatedInput`, `ask`, `once: true`) exist but
   are not load-bearing for our workflow.
4. Tavily also surfaced the Firecrawl blog post "Best Claude Code
   Skills to Try in 2026" — our skill is structured similarly; no
   gap.

**Not adopted** (explicit):
- Scalpel/PyCG integration (grounding rule; heavy dep).
- Streamable HTTP transport has a flag but no server-side auth
  layer; suitable only for localhost/tunneled use. Documented.
- SCIP export (same as before — would ripple through every schema).

### Slice 8 — LLM codebase-understanding improvements

Four items identified in the slice-7 critical review, dispatched across
subagents + main session.

- **Task A (general-purpose subagent)** — `code_index repo-map`:
  compact Aider-style ranked symbol overview. JSON + text output,
  `--limit` and `--budget-tokens`. Ranking: in-degree + test_count +
  kind boost; test-file symbols filtered. 3 new tests.
- **Task B (Codex-rescue blocked → main session)** — `code_index/parsers/jedi_enhanced.py`:
  optional Jedi-augmented resolver. Closes the typed-instance-method
  blind spot (e.g. `foo = Bar(); foo.method()`) without speculative
  typing — Jedi does sound static inference. Gated by
  `config.enable_jedi` (default False). 5 new tests (skipped when
  Jedi isn't installed). Codex rescue bounced off sandbox scope twice;
  I implemented this myself in the main session.
- **Task C (main session)** — git-history integration: schema v3 adds
  `git_blob_oid`, `git_committed_at`, `git_author` to `files`. New
  `code_index/git_meta.py` resolver uses one `git ls-files --stage` +
  on-demand `git log -1`. `doctor --json` surfaces a `git` block
  (`tracked_files`, `untracked_files`, `stale_90d`). 5 new tests.
- **Task D (main session)** — embeddings retrieval: `code_index embed`
  populates the existing `embeddings` table, `code_index similar`
  runs cosine search. Backend abstraction picks `fastembed` if
  available, else `sentence-transformers`. Closes the "semantic
  similarity" gap called out in the slice-7 critical review. 5 new
  tests using a deterministic 16-d mock backend (no model download
  needed for CI).

**Test suite**: 127 → 145 passing.

**CLI surface gained**: `repo-map`, `rebuild-tests`, `embed`, `similar`.

**`doctor --json` new blocks**: `git`, `embeddings`, `optional_deps.jedi`.

**Not done this slice**: a wired-in pipeline hook for Jedi (still
opt-in per reindex); ANN index for embeddings (brute force cosine is
fine under 50k chunks); MCP tools for `similar` / `repo-map` /
`embed` (follow-up — the base commands land in this slice).

### Slice 5 — Runners + MultiEdit + references (Codex rescue)
`code_index tests --runner pytest` now emits command-ready pytest node ids,
with `--runner-json` exposing `{runner, invocation, node_ids, skipped_tests}`.
Captured literal `@pytest.mark.parametrize` cases expand into pytest-style
case ids; non-literal and truncated cases are reported instead of guessed.

The Claude Code PostToolUse hook now matches `Edit|Write|MultiEdit`, extracts
top-level and per-edit file paths, deduplicates them, applies the existing
ignore rules, and batches survivors into one `python -m code_index update
--files ... --json` call.

Resolved `calls` relations now also emit
`occurrences(role="reference", syntax_kind="call")` rows. `code_index symbol
NAME --references --json` exposes up to 50 call-site file/line spans. This is
limited to resolved calls; imports, inherits, and contains are not reference
occurrences in this slice.
