# Pressure Test: FastAPI Benchmark Repo

Date: 2026-04-24

Target repo: local disposable clone of `benchmarks/fastapi` at
`.benchmarks/pressure-fastapi-20260424-153248`.

Repo size:

- Files reported by `rg --files`: 2,949
- Python files: 1,119
- Files indexed by `code_index`: 2,765

## Command Timings

| Command | Result | Time |
|---|---:|---:|
| `init --json` cold index | pass | 240.267s |
| `update --all --json` no-op warm scan | pass | 31.394s |
| `query "dependency injection" --limit 10 --json` | pass | 0.208s |
| `symbol FastAPI --limit 10 --references --json` | pass | 0.208s |
| `impact fastapi.applications.FastAPI --max-depth 2 --json` | pass | 0.268s |
| `tests fastapi.applications.FastAPI --max-depth 3 --json` | pass | 0.228s |
| `doctor --json` | pass | 14.971s |
| scoped add of one probe file | pass | 26.047s |
| scoped delete of probe file after fix | pass | 25.251s |
| `rebuild-fts --json` after delete drift | pass | 1.084s |

## Index Shape

Cold init produced:

- `files_parsed`: 2,583
- `files_skipped`: 182
- `files_failed`: 0
- `chunks_created`: 7,751
- `symbols_upserted`: 6,106
- `relations_inserted`: 4,828
- `relations_unresolved`: 16,446
- `test_edges_inserted`: 1,998

Doctor after cleanup:

- Live chunks: 7,751
- Live symbols: 6,106
- Relations: `calls=2444`, `contains=5168`, `imports=2280`, `inherits=104`
- Test edges: 1,998
- FTS drift after `rebuild-fts`: 0

## Findings

1. Read paths held up under a medium Python repo. FTS query, symbol lookup with
   references, impact analysis, and affected-test lookup all returned in under
   300ms once the index existed.
2. Fixed during this pressure test: deleted files retired chunks but left their
   symbols live. The pipeline now tombstones file-derived symbols/occurrences
   for both full scans and targeted `update --files <deleted-path>`, and it
   repairs legacy DBs already in the bad state.
3. Remaining performance issue: `test_edges` rebuilds fully whenever topology
   changes, and `update --all` does a full test-edge rebuild even when every
   file is unchanged. On this repo, that makes a no-op full scan about 31s and
   a one-file symbol add/delete about 25-26s.
4. Remaining precision issue: unresolved calls are much larger than resolved
   calls (`16,446` unresolved vs `2,444` resolved). This is the expected ceiling
   of the stdlib AST resolver and is a strong reason to keep pushing the SCIP
   sidecar integration.
5. JSON stdout stayed parseable, but `doctor --json` emitted a Torch warning on
   stderr while checking embeddings availability. That is not corrupting JSON,
   but it is noisy for agent automation.

## Next Slices

1. Suppress or lazy-load noisy embedding backend imports in `doctor`.
2. Run this same pressure profile after installing `scip` + `scip-python` to
   compare unresolved-call reduction.
3. Improve scoped affected-test precision for changed intermediate helper
   paths. The current scoped path handles touched test files, existing target
   files, deleted target/test symbols, and backfilled source files.

## Follow-Up: Backfill/Test-Edge Bottleneck

Implemented after the first pressure run:

- No-op full scans now skip global unresolved-call backfill and global
  `test_edges` rebuild when no files changed.
- Topology-changing updates rebuild affected tests from the touched/backfilled
  scope instead of dropping and rebuilding the full `test_edges` table.
- Backfill is filtered to newly-available symbol names when possible, avoiding
  a full scan of all unresolved calls for unrelated new symbols.

FastAPI clone before/after:

| Scenario | Before | After |
|---|---:|---:|
| no-op `update --all` | 31.394s | 3.285s |
| one-file new-symbol add | 26.047s | 0.410s |
| one-file delete | 25.251s | 0.262s |

The remaining 3.3s no-op full scan is mostly filesystem/stat + SQLite lookup
over 2,765 indexed files.
