# Benchmark — FastAPI (external complex repo)

Clone: `git clone --depth=1 https://github.com/fastapi/fastapi.git benchmarks/fastapi`
Size: **1,119 Python files**, 54 MB, 2,765 total files incl. docs.
Ran against schema v2 `code_index` at commit-equivalent of the 113-test-green
state after Slice 6.1.

## 1. Cold init

| Metric | Value |
|---|---|
| Wall time | **132.75 s** |
| Files parsed | 2,583 |
| Files skipped (empty) | 182 |
| Files failed | 0 |
| Rate | 19.5 files/s |
| Symbols upserted | 6,106 |
| Chunks created | 7,751 |
| `relations_inserted` | 2,606 |
| `relations_unresolved` | **15,040 (85% of attempted)** |
| `test_edges_inserted` | 207 |

FTS integrity ok, drift=0 after cold init. **85% of call/import/inherits
edges fail to resolve**, vs ~75% on our own repo. The jump is concentrated
in one place — see §3.

## 2. Incremental update latency

| Operation | Wall time | Notes |
|---|---|---|
| `update --files X` (no change) | **22.2 s** | should be ~100 ms |
| `update --files X` (1-line edit) | **58.3 s** | should be ≤1 s |
| `update --all` (nothing changed) | **58.7 s** | hash-short-circuits 2,764 files, still slow |
| `symbol FastAPI --json` | 163 ms | ~100 ms Python startup + ~60 ms work |
| `query "dependency injection" --json` | 159 ms | ditto |
| `impact Depends --json` | 152 ms | ditto |
| `tests FastAPI --json` | 154 ms | ditto |

**Watch mode is effectively unusable at this scale.** Every save would
trigger a 22+ second reindex; the PostToolUse hook would block behind it.

### Latency root causes (traced to code)

1. **`_backfill_unresolved` re-scans every open `unresolved_calls` row on
   every reindex, regardless of scope.** At FastAPI scale that's 15,040
   rows × multiple canonical_name LIKE lookups per row. This is
   O(unresolved × symbols) per update.
2. **`_rebuild_test_edges` deletes ALL edges and rebuilds via BFS over
   every test symbol on every reindex,** even for a single-file update.
3. **Hash-check for unchanged files reads the full file contents** — we
   don't trust mtime as a cheap short-circuit.

## 3. Correctness — the call graph is 99% missing

831 `calls` relations across 1,119 Python files = **0.74 calls per file**.
Real Python files have 20–100 calls each. We're capturing roughly 1% of
the true call graph.

Smoke probes (see terminal output for full details):

| Probe | Expected | Actual |
|---|---|---|
| `FastAPI` symbol lookup | found in `fastapi.applications` | ✓ found |
| `impact FastAPI` direct callers | dozens (every tutorial file instantiates) | **0** |
| `impact APIRouter.get` impacted | thousands of route definitions | 7 symbols, 5 files |
| `symbol fastapi.FastAPI` (the re-export) | returns the same class | **0 hits** |
| Python files with 0 outbound call edges | small minority | **757 / 1,119 (68%)** |

### Root cause of missing calls

The call-site walker in `code_index/parsers/python_ast.py::_walk` only
emits `calls` relations *from inside function/method bodies*:

```python
else:  # function or method
    for call_node in _iter_calls_shallow(node):
        ...
        ctx.pending_relations.append(PendingRelation(
            src_symbol_uid=sym.symbol_uid,
            relation_kind="calls",
            ...
        ))
```

**Module-level calls are never emitted.** That means:
- `app = FastAPI()` at the top of every tutorial file — invisible.
- `@app.get("/")` decorator invocations — invisible.
- `router = APIRouter()` — invisible.
- `logger = logging.getLogger(__name__)` — invisible.
- `settings = Settings()` — invisible.

Entire file classes (tutorial scripts, Django settings, Flask route
modules, `__main__` blocks) have 100% module-level code and produce zero
call edges in our index. This is a pure oversight in the parser — not a
documented limit.

### Root cause of missing re-exports

`fastapi/__init__.py` re-exports via `from .applications import FastAPI`
etc. Our scope builder captures the alias inside that file's scope, but
we never propagate outward: another file doing `from fastapi import
FastAPI` resolves the candidate `fastapi.FastAPI`, which doesn't exist
as a symbol, and the suffix-match `%.FastAPI` finds the concrete
definition at `fastapi.applications.FastAPI`. That match is fine —
except `fastapi.FastAPI` was meant to resolve to a *new* re-export
entry, and external callers of `symbol "fastapi.FastAPI"` silently miss.

## 4. Test edges underrepresented

207 test_edges on a repo with a large pytest suite. FastAPI's tests live
under `tests/` which matches our heuristic, so discovery is fine — the
gap is on the **outbound** side: test functions that do `response =
client.get("/")` never emit a call edge to `client.get` (module-level
decorator invocation on `app.get` produced the client), so the test
never "reaches" the subject under test in our graph.

Fixing the module-level-call bug would likely also fix this.

## 5. What this benchmark confirmed

The blind-spots essay I wrote before this bench listed ten categories.
The bench **validated four of them as concrete, reproducible bugs**:

- ✓ Decorator-wrapped callables are silently missing.
- ✓ `__init__.py` re-exports are not propagated.
- ✓ Dispatch-table and module-level instantiation patterns produce no
  edges.
- ✓ Watch/hook-driven incremental update does not scale beyond ~200
  files because two O(n) passes (`_backfill_unresolved` and
  `_rebuild_test_edges`) run in full on every update.

The bench also surfaced **new issues not on the original list**:

- ✗ Hash-check of unchanged files is too slow (no mtime short-circuit).
- ✗ Markdown-heavy repos produce huge `heuristic` chunk counts that
  inflate FTS without contributing to the semantic graph.
- ✗ `empty` parse_status files: 182 — investigate whether these are
  false negatives or legitimately empty.

## 6. Minimum set of fixes to make this scale

Ranked by impact ÷ effort:

1. **Emit `calls` for module-level code.** One-line change to the
   walker's entry point: run `_iter_calls_shallow` at module scope too,
   attributing to the module symbol. Expected to flip call-graph
   completeness from ~1% to an order of magnitude higher.
2. **Skip `_backfill_unresolved` on targeted `update --files` when no
   new symbols were added.** Track "did any new `symbol_uid` appear this
   run" in the stats; short-circuit the backfill when false.
3. **Incremental `test_edges` rebuild.** Only rebuild edges whose test
   symbol or reachable symbols changed this update. Keep the full
   rebuild as a `rebuild-tests` maintenance command.
4. **mtime short-circuit in `_resolve_paths`.** If stored
   `mtime_ns == current mtime_ns`, skip the hash read entirely.
5. **Propagate `__init__.py` re-exports.** Build a `re_export_map` at
   end-of-reindex, mapping `pkg.Name` → `pkg.sub.Name`. Use it in the
   candidate-resolution step.
6. **Document, then gate**, markdown/yaml/html ingestion — most of the
   `heuristic` 1,646 chunks are docs bloat that never answer a code
   query.

The first two alone would turn a 22-second no-op update into a sub-second
one. The first would turn the call graph from "~1% captured" into
"materially useful for impact analysis."

## 7. What I'd NOT change based on this bench

- Symbol identity model (`symbol_uid` grammar).
- Schema.
- FTS5 + SQLite choice.
- The MCP server surface.
- The `.claude/` skill + hook wiring.

Those are orthogonal to the bugs surfaced here.
