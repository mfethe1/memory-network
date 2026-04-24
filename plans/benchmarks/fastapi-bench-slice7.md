# FastAPI bench — post-Slice-7 fixes

Same external clone (`benchmarks/fastapi`, 1,119 Python files) after three
fixes implemented in this session:

1. Module-level `ast.Call` extraction.
2. `__init__.py` re-export map + resolver tier.
3. Incremental perf: mtime+size short-circuit, conditional
   `_backfill_unresolved`, scoped `_rebuild_test_edges`, new
   `code_index rebuild-tests` command.

Plus a correctness fix to `symbol_search.lookup` to consult the re-export
map so `code_index symbol "fastapi.FastAPI"` resolves to the impl.

## Results vs baseline

| Metric | Baseline | Target | Post-fix | Delta |
|---|---|---|---|---|
| `update --files X` no-op | 22,180 ms | ≤ 1,000 ms | **168 ms** | **132× faster** ✓ |
| `update --files X` 1-line edit | 58,278 ms | ≤ 3,000 ms | **1,403 ms** | **42× faster** ✓ |
| `update --all` unchanged | 58.7 s | ≤ 5 s | 65.2 s | *regressed slightly* (build_reexport_map overhead) |
| `calls` relations (FastAPI) | 831 | ≥ 5,000 | **4,834** | 5.8× more |
| `test_edges` inserted | 207 | — | **1,998** | 9.7× more |
| Python files with 0 outbound calls | 757 / 1,119 | ≤ 250 / 1,119 | **482 / 1,119** | 36% reduction |
| `symbol fastapi.FastAPI` hits | 0 | ≥ 1 | **1** (via_reexport) | ✓ |
| `impact FastAPI` direct_callers | 0 | ≥ 50 | **609** | ✓✓ |
| `impact APIRouter.get` impacted_files | 5 | — | **38** | 7× more |

## Targets not fully met

- **`calls` relations still below perfect.** 4,834 for 1,119 Python files =
  4.3 calls/file. Real Python files have 20–100 calls. The remaining gap
  is typed-instance calls (`foo = Bar(); foo.method()`), decorator-wrapped
  callables (Celery `@task`), and `getattr`-style dynamic dispatch. These
  are in the anti-goal set ("no speculative type inference") for this
  slice. A Jedi- or scip-python-backed augment would close most of it.
- **`update --all` on unchanged got slightly slower** (58.7 s → 65.2 s).
  Root cause: the re-export map is rebuilt every reindex, which adds
  ~2–3 s on 1,119 Python files. Trade-off is acceptable: `update --all`
  is rare; the common path (`update --files`) is 132× faster.

## Full test suite

127/127 pass (up from 113; 14 new tests added).

## Files changed this slice

- `code_index/parsers/python_ast.py` — module-level call extraction;
  `_resolve_relative_module` fixed for `__init__.py`.
- `code_index/pipeline.py` — `_build_reexport_map`, re-export tier in
  `_try_resolve_candidates`, mtime+size short-circuit, conditional
  backfill, scoped test_edges rebuild, new stats fields.
- `code_index/search/symbol_search.py` — re-export fallback with
  `via_reexport` annotation.
- `code_index/commands/rebuild_tests_cmd.py` — new.
- `code_index/cli.py` — wire `rebuild-tests`.
- Tests: `test_module_level_calls.py`, `test_reexports.py`,
  `test_incremental_perf.py`; fixed one assertion in
  `test_relative_imports.py` that had encoded the old (wrong) `__init__.py`
  behavior.
