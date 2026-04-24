---
description: Prefer code_index tests and impact when touching test files or choosing what to re-run.
paths:
  - "tests/**/*.py"
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/conftest.py"
---

# Test-editing rules

Use `code_index tests <target>` instead of grep when:

- Deciding which tests cover a function or class before changing it.
- Selecting which subset to re-run after a targeted edit.
- Explaining why a specific test fell out of a refactor.

Output to prefer:

- `--json` for machine parsing; each affected test carries `edge_type`
  (direct | transitive), `depth`, `confidence`, a `path` showing the call
  chain from test → target, and a `parametrize` field
  (`{argnames, case_count, cases, truncated}` or `null`).
- `--runner pytest` when you need stdout to be a ready-to-run pytest node-id
  list, one node id per line.
- `--runner pytest --runner-json` when you need
  `{runner, invocation, node_ids, skipped_tests}` for automation.
- `--direct-only` when you need high-confidence, immediate callers only.
- The top-level `summary` includes `parametrized_test_count` and
  `parametrized_case_total` — useful for estimating re-run cost.

`code_index tests` is backed by the `test_edges` table, rebuilt every reindex
via BFS over `calls`. Heuristic for test discovery: files under `tests/`, or
`test_*.py` / `*_test.py` / `conftest.py`.

Note: `test_edges` remains grouped — one edge per test symbol, not one per
parameter case. The pytest runner emitter expands captured literal cases into
`test_foo[1-2]`-style node ids. Non-literal cases and uncaptured truncated
cases are reported in `skipped_tests` rather than guessed.

For blast-radius analysis of a production symbol (not just tests), use
`code_index impact` and read the `impacted_symbols` + `impacted_files`.
