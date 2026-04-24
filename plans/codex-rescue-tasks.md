# Codex Rescue Plan — code_index hardening batch

> **Audience.** This file is a handoff prompt for `/codex:rescue` running
> GPT-5.5. It is structured so each task can be claimed, implemented,
> verified, and checked off independently without further clarification.
>
> **Source of truth.** Repository tree, `CLAUDE.md`, `docs/code-index-spec.md`,
> and `.claude/skills/code-index/SKILL.md`. Do not drift from those.
>
> **Runtime constraints.** Python 3.10+; stdlib-first; `tree-sitter` and
> `tree-sitter-python` are already installed; `watchdog` is already
> installed; `ripgrep` may or may not be resolvable at runtime (use the
> existing `code_index.search.rg_discovery` resolver, do not add a second
> path).
>
> **Context budget.** All three tasks are self-contained. Read only the
> files referenced in the **"Files you will touch"** section per task.
> Do not refactor unrelated code. Do not rename public functions.

---

## Ground rules (apply to every task)

1. **Symbol-first remains primary.** `symbol_uid` stays the durable
   identity; `chunk_uid` secondary. Do not invent new identity semantics.
2. **Pipeline is shared.** `code_index.pipeline.reindex()` is the only
   entrypoint for `init`/`update`/`watch`. Do not fork a parallel code
   path.
3. **JSON output is the stable contract.** If you add fields, add them;
   do not rename existing fields. Every subcommand must keep `--json`
   parseable.
4. **Tests must come with code.** Every behaviour change needs at least
   one test under `tests/` that fails before the change and passes after.
5. **No speculative type inference.** When you add any resolution logic,
   ground it in an explicit AST signal (e.g. `a = Cls()`); never guess
   from names.
6. **Honest limits.** If a feature only partly works, document the limit
   in the command's `limitations` JSON list and in
   `.claude/skills/code-index/SKILL.md` §7.
7. **Stop on red.** If `python -m pytest tests/ -q --timeout=30` is not
   green at the end, do not finish the task.

---

## Task 1 — `code_index tests --runner pytest` emitter

### Objective

Produce a ready-to-run pytest node-id list from `code_index tests <target>`.
Re-uses the `parametrize` field captured in Slice 4 (`argnames`,
`case_count`, `cases`, `truncated`). Agents need to be able to pipe the
output directly into `pytest`.

### Command shape

```
code_index tests SYMBOL [--direct-only] [--max-depth N] \
                         [--runner pytest] [--runner-json]
```

- `--runner pytest` prints one pytest node id per line on stdout (no
  JSON). Example lines:
  ```
  tests/test_calc.py::test_add_plain
  tests/test_calc.py::test_add_params[1-2-3]
  tests/test_calc.py::test_add_params[0-0-0]
  tests/test_calc.py::test_add_params[5--2-3]
  ```
- `--runner pytest --runner-json` prints a single JSON object:
  ```json
  {
    "runner": "pytest",
    "invocation": ["pytest", "tests/test_calc.py::test_add_plain", "..."],
    "node_ids": ["tests/test_calc.py::test_add_plain", "..."],
    "skipped_tests": [
      {
        "canonical_name": "tests.test_foo.test_dynamic",
        "reason": "parametrize arguments are not literal"
      }
    ]
  }
  ```
- Without `--runner pytest`, current behaviour is unchanged.
- `--runner` with any non-`pytest` value exits non-zero with a clean
  JSON error (`{"error": "unknown runner", "supported": ["pytest"]}`).

### Node-id formation

For each affected test row:

1. Derive `file_path` from the row's `def_file`.
2. Derive `test_name` from the row's `canonical_name` — it's already
   `<module>.<test_name>`, so `rsplit('.', 1)[-1]`.
3. If `row["parametrize"]` is `None`, emit
   `file_path::test_name`.
4. If `row["parametrize"]` is present:
   - If `truncated == True`, still emit one node id per *captured*
     case. Append a limitation to `skipped_tests` naming the full
     `case_count` and noting that only the first 16 cases are emitted.
   - Each case string is pytest's `-` -joined id. Convert each
     `cases[i]` literal using the rule: strip outer parens/brackets,
     split on commas at depth 0, emit `ast.literal_eval` of each
     component's `repr`, join with `-`. If any component fails to
     parse (non-literal values, variable references), skip this case
     and record the test in `skipped_tests`.
5. For methods inside a class (`canonical_name` ends in
   `Class.method`), emit `file::ClassName::method`.

### Files you will touch

- `code_index/commands/tests_cmd.py` — add runner handling; keep FTS /
  parametrize rendering path intact.
- `code_index/cli.py` — add `--runner` and `--runner-json`.
- New: `code_index/runners/pytest.py` — pure node-id formation. **New
  package** `code_index/runners/__init__.py` with just an `__all__`
  entry.
- New: `tests/test_pytest_runner.py`.

### Acceptance criteria

- `code_index tests NAME --runner pytest` prints node ids only, one per
  line, deterministic order (same as current `affected_tests`).
- `--runner pytest --runner-json` returns the JSON shape described
  above.
- Parametrized tests with literal cases produce expanded node ids.
- Non-literal parametrize cases get reported in `skipped_tests` with a
  reason.
- At least three new tests in `tests/test_pytest_runner.py` covering:
  - plain test → single node id
  - parametrized test with three tuple cases → three node ids
  - parametrized test with a non-literal case (e.g. a variable
    reference) → skipped with reason
- `python -m pytest tests/ -q --timeout=30` is green.

### Out of scope

- Running pytest. This emitter is format-only.
- Other runners (`unittest`, `nose`). Emit the "unsupported runner"
  error cleanly and stop.

---

## Task 2 — Support Claude Code `MultiEdit` in the reindex hook

### Objective

The `PostToolUse` hook at `.claude/hooks/reindex-after-edit.sh` only
reads `tool_input.file_path`. Claude Code's `MultiEdit` tool provides a
different shape (`tool_input.edits[].file_path`, plus the top-level
`tool_input.file_path` in newer versions). Extend the hook so every
changed file is passed to `code_index update --files`.

### Hook input shapes

The hook currently receives JSON on stdin from Claude Code. Expect any
of these shapes (handle each without crashing if fields are missing):

```json
// Edit / Write (existing)
{"tool_input": {"file_path": "code_index/pipeline.py"}}

// MultiEdit variant A (current)
{"tool_input": {
  "file_path": "code_index/pipeline.py",
  "edits": [{"old_string": "...", "new_string": "..."}, ...]
}}

// MultiEdit variant B (multi-file)
{"tool_input": {
  "edits": [
    {"file_path": "a.py", "old_string": "...", "new_string": "..."},
    {"file_path": "b.py", "old_string": "...", "new_string": "..."}
  ]
}}
```

### Required behaviour

1. Collect a deduplicated list of absolute `file_path` values from
   *every* recognised location above. If no path is found, exit 0
   silently (current behaviour).
2. For each collected path: apply the existing ignore rules
   (`.claude/`, `.git/`, `.code_index/`, caches, virtualenvs, binary
   extensions). Skip ignored paths without calling `code_index`.
3. Call `python -m code_index update --files <REL1> <REL2> ... --json`
   exactly once with all surviving paths (not once per file).
4. On failure, emit one stderr line like:
   `code_index reindex failed for <comma-joined rel paths>\n<stderr tail>`.

### Files you will touch

- `.claude/hooks/reindex-after-edit.sh` — the bash implementation.
- `tests/test_claude_hook.py` — **new**. Use `subprocess` to pipe a
  heredoc of each JSON shape through the script and assert the command
  the hook *would* invoke. Capture with a stub shim: before the
  subprocess call, set `PATH` so that `python` points at a tiny
  `echo-args` script the test creates, so the test sees the exact
  argv. (Or: add a `CODE_INDEX_DRY_RUN=1` env var to the hook that
  makes it print the would-be command instead of executing — whichever
  you prefer, but the tests must be deterministic on Windows Git Bash
  and on Linux.)

### Acceptance criteria

- Bash script handles all three JSON shapes and runs on
  Git Bash (Windows) and `/bin/bash` (Linux) without modification.
- Running the hook with zero paths extracted exits 0 silently.
- Three new tests covering Edit, MultiEdit variant A, MultiEdit
  variant B, plus one test that proves ignore rules still apply to
  MultiEdit paths.
- `python -m pytest tests/ -q --timeout=30` is green.

### Out of scope

- Running the hook in parallel for a batch. Sequential is fine.
- Adding a new tool matcher in `.claude/settings.json` — the current
  `Edit|Write` matcher needs to be broadened to
  `Edit|Write|MultiEdit` in the same JSON file; that's the single
  config edit you must make in addition to the hook change.

---

## Task 3 — Call-site occurrences (cross-reference the graph)

### Objective

Today when a pending call resolves, we insert one row into `relations`
but no matching `occurrences(role='reference', ...)`. That means the
semantic spine can answer "who calls X?" (via `relations`) but not
"show me every call site of X with file and line." Fix that.

### Required behaviour

1. When `_resolve_pending` or `_backfill_unresolved` creates a
   `relations` row with `relation_kind='calls'`, **also** insert an
   `occurrences` row:
   - `symbol_pk` = the resolved `dst_symbol_pk`.
   - `file_pk` = the file containing the `src_symbol_pk`
     (use the `definition` occurrence of the src).
   - `role` = `'reference'`.
   - `start_line` / `end_line` = the call site line from
     `rel.site_line` (or the `site_line` column in
     `unresolved_calls`).
   - `syntax_kind` = `'call'`.
2. When a file is reparsed, `occurrences` rows for that file are
   already wiped by `_apply_parsed_file`. That's fine: the resolve
   pass that follows re-emits the `reference` rows.
3. Expose them: `code_index symbol NAME --json` already returns `def_file`
   / `def_line`; **add** a `references` array listing
   `[{file, start_line, end_line}, ...]` up to a hard limit of 50.
   Respect a new `--references` flag; default omits them for
   backward compatibility.

### Files you will touch

- `code_index/pipeline.py` — add reference-occurrence emission in both
  `_resolve_pending` and `_backfill_unresolved`.
- `code_index/search/symbol_search.py` — optionally pull references.
- `code_index/commands/symbol_cmd.py` — add `--references` flag.
- `code_index/cli.py` — plumb `--references` through.
- `tests/test_references.py` — **new**.

### Acceptance criteria

- After a fresh `init`, `code_index symbol "reindex" --json
  --references` returns a `references` array containing at least
  entries for `init_cmd.run` and `update_cmd.run` (both call
  `reindex`).
- References stay in sync on `update --files`: removing a caller's
  call and reparsing that caller causes the reference row to
  disappear; reintroducing the call restores it.
- No duplicate reference rows for the same `(symbol_pk, file_pk,
  start_line, end_line)` tuple — add a guarded insert or an appropriate
  UNIQUE constraint.
- Three new tests: fresh-index reference list, remove-caller drop,
  restore-caller reappearance.
- `python -m pytest tests/ -q --timeout=30` is green.

### Out of scope

- References for `imports`, `inherits`, `contains` — only `calls` in
  this slice.
- Full call graph traversal UI — just the single-symbol reference
  list.

---

## Cross-task verification (run after all three land)

Run, in order, and record the output:

```
python -m pytest tests/ -q --timeout=30           # full suite
python -m code_index init --force --json          # reindex real repo
python -m code_index doctor --json                # snapshot health
python -m code_index tests "reindex" --runner pytest      # task 1 smoke
python -m code_index symbol "reindex" --references --json # task 3 smoke
```

Then confirm:

- No test file was renamed or deleted.
- `.claude/CLAUDE.md`, `.claude/rules/code-index-python.md`,
  `.claude/rules/code-index-tests.md`, and
  `.claude/skills/code-index/SKILL.md` describe the new capabilities
  truthfully (no over-claiming).
- `.claude/settings.json`'s PostToolUse matcher is
  `"Edit|Write|MultiEdit"`.
- `docs/claude-code.md`'s "truthful reality check" section mentions the
  pytest-runner emitter, MultiEdit hook support, and reference
  occurrences.

---

## Completion checklist

- [ ] Task 1: `code_index tests --runner pytest` emits pytest node ids
      (incl. parametrized expansion).
- [ ] Task 2: PostToolUse hook handles `MultiEdit`; settings matcher
      updated.
- [ ] Task 3: Call-site occurrences recorded; `symbol --references`
      available.
- [ ] All tests green (`pytest tests/ -q --timeout=30`).
- [ ] `.claude/` files updated; no stale claims.
- [ ] `docs/claude-code.md` reality check extended.
- [ ] `plans/code-index-repo-plan.md` §13 slice-log entry appended
      (title: "Slice 5 — Runners + MultiEdit + references (Codex
      rescue)").

## When you finish

Write a Markdown block at the top of
`plans/codex-rescue-report.md` (create it if missing) containing:

- Files changed (one per line).
- Tests added (names).
- Commands run and their summarized results.
- Any limitation you had to add or any acceptance criterion you could
  not meet, with reasoning.

Return that file path in your final reply.
