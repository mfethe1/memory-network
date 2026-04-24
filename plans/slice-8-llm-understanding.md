# Slice 8 — LLM codebase-understanding improvements

> Follow-up to `plans/benchmarks/fastapi-bench-slice7.md` critical review.
> Four independent tasks. Two dispatched to subagents in parallel; two done
> in the main session sequentially.

## Ground rules (all tasks)

1. 127 pytest tests must stay green. `python -m pytest tests/ -q --timeout=60`.
2. `symbol_uid` stays primary. No breaking schema changes.
3. Shared `pipeline.reindex()` stays the only entrypoint. Don't fork it.
4. Add new fields to JSON, never rename.
5. Stop on red.

---

## Task A — Aider-style repo-map command  _(parallel subagent)_

### Objective
Give LLMs a compact structured overview of the codebase they can consume at
session start. Aider's repo-map [ships ~2 KB of high-signal symbol info per
repo](https://aider.chat/docs/repomap.html) and measures a real quality
improvement. We already have everything needed — this is a formatter over
`symbols` / `relations` / `test_edges`.

### Required behaviour
- New `code_index repo-map` command.
- Ranks symbols by a composite score: in-degree (callers + importers +
  inheriters) + `test_edges` count + a boost for `class` / `module` kinds.
- Output modes: `--format text` (human) and `--format json` (default).
- `--limit N` caps the number of symbols returned (default 100).
- `--budget-tokens N` trims the output until a rough token count fits
  (1 token ≈ 4 characters as a heuristic).
- Text format, per line: `[kind] canonical_name :: signature_norm  (file:line)`.
- JSON format: `{ "symbols": [ { canonical_name, kind, def_file, def_line,
  signature, in_degree, test_count, score } ] }`.

### Files you will touch
- `code_index/commands/repo_map_cmd.py` — **new**
- `code_index/cli.py` — one `subparsers.add_parser("repo-map", ...)` block;
  copy the existing `rebuild-tests` wiring as a template. Do NOT modify any
  other subparser.
- `tests/test_repo_map.py` — **new**

### DO NOT touch
- `code_index/pipeline.py`
- `code_index/commands/mcp_serve_cmd.py` (MCP integration is a follow-up)
- `code_index/schema.sql`
- Any parser module

### Acceptance tests
Three tests in `tests/test_repo_map.py`:
1. On a tiny fixture repo with 3 files + 1 test, `repo-map --format json
   --limit 10` returns a non-empty `symbols` list ordered by descending
   `score`.
2. `--budget-tokens 200` produces output strictly smaller than unbounded.
3. Symbols from test files are NOT included in the top 10 (they rank low
   by design — tests shouldn't appear in a repo map).

### Verification
- Run `python -m pytest tests/test_repo_map.py -v`.
- Run `python -m code_index repo-map --format json --limit 20` against the
  `code_index/` self-repo; eyeball that top entries are real orchestrators
  (`pipeline.reindex`, `cli.main`, etc.), not obscure helpers.

---

## Task B — Jedi-augmented call resolution  _(parallel subagent / codex rescue)_

### Objective
The FastAPI bench showed we capture ~5-15 % of the real call graph. Most
of the gap is typed-instance method calls (`foo = Bar(); foo.method()`),
decorator-wrapped callables, and `getattr`-style dispatch. [Jedi](https://jedi.readthedocs.io/en/latest/docs/api.html)
handles all three via static inference without speculative typing. Add
it as an **optional** post-pass that upgrades unresolved edges.

### Required behaviour
- New optional extra: `pip install code-index[jedi]` installs `jedi>=0.19`.
- New module `code_index/parsers/jedi_enhanced.py` that exposes ONE public
  function: `resolve_unresolved_calls(config, conn) -> dict`.
  - Iterates `unresolved_calls` rows where `resolved_at IS NULL`.
  - For each row, reconstructs the call site from the source file (using
    `file_pk → files.file_path`, `site_line`, and the row's
    `dst_candidates_json`).
  - Uses `jedi.Script.goto(line=N, column=...)` to resolve.
  - If Jedi returns a definition whose `module_path` and `name` match a
    symbol in our `symbols` table (by `canonical_name`), upgrade the edge:
    insert into `relations` with `provenance='jedi:goto'`, mark the row
    `resolved_at=now`.
  - Return `{"resolved_by_jedi": N, "still_unresolved": M, "jedi_errors": K}`.
- Gate behind a new config key `enable_jedi: bool` (default `False`).
  When False, the module is never imported. When True but Jedi is not
  installed, log once and proceed with the existing resolver only.
- `doctor` JSON output must gain a `jedi.available: bool` and
  `jedi.enabled: bool` field under `optional_deps`.

### Files you will touch
- `code_index/parsers/jedi_enhanced.py` — **new**
- `code_index/config.py` — add `enable_jedi: bool = False` to `Config`.
- `code_index/commands/doctor_cmd.py` — extend `optional_deps` only.
- `pyproject.toml` — add `jedi = ["jedi>=0.19"]` to `optional-dependencies`.
- `tests/test_jedi_enhanced.py` — **new**

### DO NOT touch
- `code_index/pipeline.py` (hooking Jedi INTO the pipeline is a follow-up).
- The existing resolver in `pipeline.py::_try_resolve_candidates`.
- Any other parser.

### Acceptance tests
Tests must **skip** when Jedi is not installed. When Jedi is installed:
1. A fixture repo with `foo = Bar(); foo.method()` produces a
   `calls` edge from the caller to `Bar.method` after
   `resolve_unresolved_calls` runs.
2. A call site that Jedi can't resolve (e.g. `getattr(self, x)()`) leaves
   the `unresolved_calls` row untouched and appears in `still_unresolved`.
3. The stats return value has the three expected keys.

### Verification
- `python -c "import jedi"` must not be required for the full test suite
  to pass — guard imports.
- `python -m code_index doctor --json` must not crash when Jedi isn't
  installed.

---

## Task C — Git history integration  _(main session, foreground)_

### Objective
Enable ownership/hotspot/recency signals by populating git metadata during
reindex. This foundation is required for repo-map ranking (Task A can use
it) and for future doctor/hotspots features.

### Required behaviour
- Schema bump to v3. New columns on `files`:
  `git_blob_oid TEXT`, `git_committed_at INTEGER` (unix ts),
  `git_author TEXT`.
- Migration drops nothing; just `ALTER TABLE files ADD COLUMN …`.
- Pipeline: during each file reindex, if the repo root has a `.git/`,
  run `git log -1 --format=%H%x00%ct%x00%an -- <rel_path>` (NUL-separated
  to survive author names with pipes). Cache the subprocess invocation
  in-process with a simple dict (path → result) so repeated lookups within
  one reindex don't fork a process per file.
- Untracked files: columns stay `NULL`.
- Non-git repos: skip the call entirely after the first failure; don't
  error on every file.
- `doctor --json` gains a `git` block:
  `{ "tracked_files": N, "untracked_files": M, "stale_90d": K }` where
  stale is "tracked files with `git_committed_at` older than 90 days".

### Files I will touch
- `code_index/schema.sql`
- `code_index/db.py` (bump `SCHEMA_VERSION` to "3" + migration hook)
- `code_index/pipeline.py` (populate on reindex)
- `code_index/commands/doctor_cmd.py` (git block)
- `tests/test_git_history.py` — **new**

---

## Task D — Embedding retrieval over chunks  _(main session, after C)_

### Objective
Close the biggest gap identified in the slice-7 critical review: semantic
similarity. Without embeddings, `query "JWT expiry"` only hits files that
literally contain those tokens. Even a tiny local embedding model closes
this gap for most "find code like this" questions.

### Required behaviour
- Optional extra: `pip install code-index[embeddings]` installs
  `fastembed>=0.3` (small, CPU-only, no PyTorch dep).
- New `code_index/embeddings/` package:
  - `store.py` — writes to the existing `embeddings` table (already in
    schema) keyed by `chunk_pk`.
  - `models.py` — default model: `"BAAI/bge-small-en-v1.5"` (384-d,
    ~100 MB, Apache-2.0).
- New commands:
  - `code_index embed [--model NAME] [--batch 32]` — populate/refresh
    embeddings for all live chunks missing a row.
  - `code_index similar QUERY [--limit N]` — semantic retrieval.
- Scoring: cosine similarity. For a repo with < 10k chunks, brute-force
  in-memory scan is fine; no ANN index needed.
- `doctor --json` gains `embeddings: { model, dimension, total_chunks,
  embedded_chunks, coverage_pct }`.
- Degrade gracefully: if `fastembed` isn't installed, `embed` and
  `similar` emit a clean JSON error pointing at the install command.

### Files I will touch
- `code_index/embeddings/__init__.py`, `store.py`, `models.py` — **new**
- `code_index/commands/embed_cmd.py`, `similar_cmd.py` — **new**
- `code_index/cli.py` — two new subparsers
- `code_index/commands/doctor_cmd.py` — embeddings block
- `pyproject.toml` — optional dep
- `tests/test_embeddings.py` — **new**; uses a mocked embedding function
  so the test suite doesn't depend on fastembed.

---

## Order of operations

1. Write this plan (done).
2. Dispatch Task A + Task B as parallel subagents.
3. Implement Task C in main session (schema + pipeline).
4. Implement Task D in main session (embeddings).
5. Wait for subagents; if they produced clean patches, integrate; if not,
   inline-complete the simpler pieces.
6. Run the full test suite; bench on FastAPI to measure impact.
7. Update `.claude/` docs and `plans/code-index-repo-plan.md` §13.
