# Codex Rescue Plan — Fix what the FastAPI bench revealed

> **Audience.** `/codex:rescue` running GPT-5.x.
> **Working dir.** `E:\Projects\hackathon\memory-claude`.
> **Baseline.** 113 tests green, schema v2.
> **Ground-truth reference.** `plans/benchmarks/fastapi-bench.md`.
>
> The benchmark against FastAPI (1,119 Python files) proved four real
> bugs. This slice fixes them in a controlled, testable way. Do **not**
> add external dependencies (no Jedi, no SCIP). Phase-2 integrations are
> a separate slice.

---

## Ground rules

1. `pytest tests/ -q --timeout=30` must end green. Stop on red.
2. Every behaviour change needs a unit test **and** a FastAPI-bench delta
   recorded in the report.
3. `symbol_uid` stays primary; no schema breaking changes.
4. Keep the shared `pipeline.reindex()` entrypoint.
5. No speculative type inference (anti-goal, per slice 4 directive).
6. Preserve all JSON field names — add, don't rename.

## Benchmark baseline to beat (from today's run)

| Metric | Baseline | Target |
|---|---|---|
| FastAPI cold `init` wall time | 132.75 s | ≤ 90 s |
| `update --files X` no-op | 22.2 s | ≤ 1.0 s |
| `update --files X` one-line edit | 58.3 s | ≤ 3.0 s |
| `update --all` unchanged | 58.7 s | ≤ 5.0 s |
| `calls` relations (FastAPI) | 831 | ≥ 5,000 |
| Python files with 0 outbound calls | 757 / 1,119 | ≤ 250 / 1,119 |
| `symbol fastapi.FastAPI` hits | 0 | ≥ 1 |
| `impact FastAPI` direct_callers | 0 | ≥ 50 |

Record actual post-fix numbers in `plans/codex-rescue-bench-fixes-report.md`.

---

## Task 1 — Emit `calls` for module-level statements

### Root cause

`code_index/parsers/python_ast.py::_walk` only traverses into
`FunctionDef` / `AsyncFunctionDef` / `ClassDef` nodes, and the call-site
extraction block lives inside the function branch. Top-level statements
(module body) are never scanned for `ast.Call` nodes. This invisibly
drops all decorator invocations, `app = FastAPI()` style instantiations,
`@app.get("/")` route registrations, and module-scoped initialisation.

### Required behaviour

1. Attribute module-level calls to the **module symbol** (not to any
   class or function). The module symbol already exists (`kind='module'`)
   with `canonical_name` equal to the file's dotted module path.
2. Use the existing `_iter_calls_shallow` helper — it already stops at
   nested `FunctionDef`/`ClassDef` boundaries, so running it on the
   module node gives *only* the module-level calls without double-counting.
3. Use the existing `_resolve_callee` helper with `class_qual=None`.
4. Emit `PendingRelation` entries exactly like the function-body branch.
5. Keep the walker deterministic: module-level calls must be attributed
   in source order.

### Files you'll touch

- `code_index/parsers/python_ast.py` (extend `parse()` after `_walk`
  returns to run `_iter_calls_shallow` on `tree` itself, emitting
  pending relations attributed to `module_sym.symbol_uid`).
- `tests/test_module_level_calls.py` — **new**.

### Acceptance tests

- `tests/test_module_level_calls.py`:
  1. A file with only module-level code (`app = FastAPI()`, `router =
     APIRouter()`) produces at least 2 `calls` `PendingRelation`s
     attributed to the module symbol.
  2. A decorator at module scope (`@app.get("/")` above a function)
     produces a `calls` pending relation whose dst candidates include
     `app.get`.
  3. A function body that *also* calls something does NOT double-count:
     `ast.Call` inside the inner function shows up once as a function
     caller, and nothing leaks to the module.

---

## Task 2 — Propagate `__init__.py` re-exports

### Root cause

`fastapi/__init__.py` contains `from .applications import FastAPI`. Our
scope resolver maps `FastAPI` → `fastapi.applications.FastAPI` *inside
the __init__.py scope only*. External callers who do `from fastapi
import FastAPI` resolve candidate `fastapi.FastAPI`, which does not
exist as a symbol. Suffix match `%.FastAPI` silently hides the real
hit, and `code_index symbol "fastapi.FastAPI"` returns zero results.

### Required behaviour

1. After the main reindex loop completes, build a **re-export map**
   scanning every `__init__.py` file's `ImportFrom` statements:
   - For `from .applications import FastAPI` in `pkg/__init__.py`,
     record `pkg.FastAPI → pkg.applications.FastAPI`.
   - For `from .applications import FastAPI as App`, record
     `pkg.App → pkg.applications.FastAPI`.
   - Handle `from . import applications` by recording
     `pkg.applications → pkg.applications` (no-op but harmless).
   - Handle `from ..sibling import Thing` using
     `_resolve_relative_module`, which already exists.
2. Use the re-export map as a third resolution tier (between exact match
   and suffix match) in `_try_resolve_candidates`. If a candidate is in
   the re-export map, look up its resolved target in the symbols table.
3. Expose a read-only helper `_reexport_target(conn, candidate)` so
   `symbol` lookups and MCP `find_symbol` can optionally report that a
   query resolved via a re-export chain (set a `via_reexport=True` flag
   in the returned row).
4. No schema change required — the re-export map lives in memory during
   a single reindex transaction.

### Files you'll touch

- `code_index/pipeline.py`
  — build `_reexport_map(conn)` at end of reindex, pass into
  `_try_resolve_candidates` and `_backfill_unresolved`.
- `code_index/search/symbol_search.py`
  — accept an optional re-export resolver and annotate hits.
- `tests/test_reexports.py` — **new**.

### Acceptance tests

1. A tiny tmp repo with `pkg/__init__.py` doing `from .impl import Foo`
   and another file `caller.py` doing `from pkg import Foo; Foo()`
   produces a `calls` relation from `caller` → `pkg.impl.Foo` after
   `init`.
2. `code_index symbol "pkg.Foo" --json` returns the impl symbol
   with `via_reexport=True` in the result row.
3. Renaming `Foo` to `Bar` in `impl.py` and running `update --files
   pkg/impl.py` causes the re-export edge to re-resolve correctly
   after the caller file is NOT re-parsed (pure backfill case).

---

## Task 3 — Incremental reindex performance

Two O(n) passes run in full on every single-file update:

- `_backfill_unresolved` iterates every open `unresolved_calls` row
  (15 k at FastAPI scale).
- `_rebuild_test_edges` deletes ALL edges and BFS-rebuilds them.

Plus: `_resolve_paths` reads every unchanged file to hash-compare
instead of trusting mtime.

### Required behaviour

1. **mtime short-circuit.** In `_resolve_paths` (or wherever hashing
   happens), if `(mtime_ns, size_bytes)` match the `files` row and
   `deleted_at IS NULL`, skip the byte-level hash read. Full hash
   still runs whenever mtime or size differs. Add a `force_hash`
   config flag (default False) to disable the short-circuit for CI.
2. **Conditional backfill.** Track whether this reindex introduced
   any new `symbol_uid`s. When `paths` is a non-empty explicit list
   AND no new symbols appeared AND no symbols tombstoned, skip
   `_backfill_unresolved` entirely. Record `skipped=true` in stats
   under a new field `relations_backfill_skipped`.
3. **Scoped test-edge rebuild.** When `paths` is a non-empty explicit
   list:
   - Compute `touched_symbols` = every `symbol_pk` defined in any
     touched file (including tombstoned ones).
   - DELETE only `test_edges` where `test_symbol_pk` is in
     `touched_symbols` OR `target_symbol_pk` is in `touched_symbols`.
   - Re-run BFS only for test symbols in `touched_symbols` (when a
     touched file is a test file).
   - A `rebuild-tests` CLI command must still exist for full rebuild.

Record new `ReindexStats` fields:
- `relations_backfill_skipped: bool`
- `test_edges_rebuilt_scope: 'full' | 'scoped'`

### Files you'll touch

- `code_index/pipeline.py` — all three passes.
- `code_index/cli.py` — new `rebuild-tests` subcommand.
- `code_index/commands/rebuild_tests_cmd.py` — **new**, mirrors
  `rebuild_fts_cmd.py`.
- `tests/test_incremental_perf.py` — **new**.

### Acceptance tests

1. `update --files X` on a file whose mtime+size are unchanged returns
   in under 200 ms on our own repo (50 files) and under 1.5 s on the
   FastAPI bench directory. Use `time.perf_counter()`; allow ±20 %
   headroom on CI.
2. Reindexing a non-test file that introduces no new symbols returns
   `relations_backfill_skipped: true` in the JSON stats.
3. Test-edge rebuild in "scoped" mode never touches edges whose
   `test_symbol_pk` is in an untouched file. Prove by snapshotting
   `test_edges` before and after a single-file non-test update on a
   fixture with mixed files.
4. `rebuild-tests` CLI produces identical output to init (i.e.
   forces a full rebuild).

---

## Cross-task verification

Run in this order and record each in the report file:

```
python -m pytest tests/ -q --timeout=30
cd benchmarks/fastapi
PYTHONPATH=E:/Projects/hackathon/memory-claude python -m code_index init --force --json
PYTHONPATH=E:/Projects/hackathon/memory-claude python -m code_index doctor --json
PYTHONPATH=E:/Projects/hackathon/memory-claude python -m code_index symbol "fastapi.FastAPI" --json
PYTHONPATH=E:/Projects/hackathon/memory-claude python -m code_index impact "FastAPI" --json
PYTHONPATH=E:/Projects/hackathon/memory-claude python -m code_index impact "APIRouter.get" --json
```

Then re-run the incremental-update microbench from
`plans/benchmarks/fastapi-bench.md` §2 and record the new numbers.

## Update .claude/ and docs

Only after the bench target numbers are met:

- `.claude/rules/code-index-python.md`: drop the "module-level calls
  not captured" disclaimer; add "re-exports from `__init__.py` resolve
  correctly" to the "knows" list.
- `.claude/skills/code-index/SKILL.md` §7 Known limitations: remove
  the items fixed in this slice; keep dynamic attribute and typed-
  instance limits.
- `docs/claude-code.md` §"What the indexer currently handles": add
  module-level calls + re-export propagation + incremental perf
  entries with test pointers.
- `plans/code-index-repo-plan.md` §13: append "Slice 7 — FastAPI bench
  fixes (Codex rescue)" summarising which blind spots were closed.

## Deliverable

Write `plans/codex-rescue-bench-fixes-report.md` with:

- Files changed (one per line)
- Tests added (names)
- Baseline vs post-fix numbers in a table
- Any acceptance criterion you could not meet, with reason
- Any new limit you had to document

Return the report path in your final reply.

## Anti-goals for this slice

Do NOT in this slice:

- Add Jedi, scip-python, or any external resolver.
- Add embeddings retrieval.
- Broaden to non-Python languages.
- Implement typed-instance (`foo = Bar(); foo.method()`) resolution.
- Change `symbol_uid` grammar or any schema table shape.
- Implement SCIP export.
- Modify the MCP server surface (field names, tool list, resources).

These are all legitimate follow-ups for a later slice. Stay bounded.
