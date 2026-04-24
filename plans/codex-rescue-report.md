# Codex Rescue Report

## Files changed

- `.claude/CLAUDE.md`
- `.claude/hooks/reindex-after-edit.sh`
- `.claude/rules/code-index-python.md`
- `.claude/rules/code-index-tests.md`
- `.claude/settings.json`
- `.claude/skills/code-index/SKILL.md`
- `code_index/cli.py`
- `code_index/commands/symbol_cmd.py`
- `code_index/commands/tests_cmd.py`
- `code_index/pipeline.py`
- `code_index/runners/__init__.py`
- `code_index/runners/pytest.py`
- `code_index/search/symbol_search.py`
- `docs/claude-code.md`
- `plans/code-index-repo-plan.md`
- `plans/codex-rescue-report.md`
- `tests/test_claude_hook.py`
- `tests/test_pytest_runner.py`
- `tests/test_references.py`

## Tests added

- `tests/test_pytest_runner.py::test_pytest_runner_emits_plain_node_id`
- `tests/test_pytest_runner.py::test_pytest_runner_expands_literal_parametrize_cases`
- `tests/test_pytest_runner.py::test_pytest_runner_reports_non_literal_parametrize_case`
- `tests/test_claude_hook.py::test_hook_extracts_edit_file_path`
- `tests/test_claude_hook.py::test_hook_extracts_multiedit_top_level_file_path`
- `tests/test_claude_hook.py::test_hook_extracts_multiedit_per_edit_file_paths_once`
- `tests/test_claude_hook.py::test_hook_ignores_generated_multiedit_paths`
- `tests/test_claude_hook.py::test_hook_exits_silently_when_no_paths_are_present`
- `tests/test_references.py::test_symbol_references_flag_lists_fresh_index_call_sites`
- `tests/test_references.py::test_reference_occurrence_disappears_when_caller_call_is_removed`
- `tests/test_references.py::test_reference_occurrence_reappears_when_caller_call_is_restored`

## Commands run and summarized results

**Codex sandbox failed to launch Python** (`CreateProcessAsUserW failed: 5`), so the in-session verification block could not execute. Verification was therefore completed from the parent Claude Code session after Codex returned:

- `python -m pytest tests/ -q --timeout=30` — initially 2 failures in `tests/test_claude_hook.py` because the hook passed Python's text-mode `print()` output through bash's `read -r` on Windows, leaving a stray `\r` inside path variables and splitting the dry-run output across lines. Fixed by adding `FILE_PATH="${FILE_PATH%$'\r'}"` immediately after `read -r`. Re-run: **101 passed in 4.00s**.
- `python -m code_index init --force --json` — parsed 65 files, inserted 559 relations, materialized 1713 `test_edges`.
- `python -m code_index doctor --json` — schema v2, `relations={calls:411, contains:272, imports:148}`, `test_edges=1713`, `fts_consistency.drift=2` (under threshold; `rebuild_recommended=false`), `unresolved_calls_open=1325` (stdlib / external calls; expected).
- `python -m code_index tests "reindex" --runner pytest` — emits node-id-per-line stdout; verified against real repo tests.
- `python -m code_index symbol "reindex" --references --json` — `code_index.pipeline.reindex` reports 30 references, first three at `init_cmd.py:27`, `update_cmd.py:33`, `watch_cmd.py:274` (i.e. the three live callers, plus test/self references).
- Parametrize expansion smoke in an isolated tmp repo — `@pytest.mark.parametrize("a, b, expected", [(1, 2, 3), (0, 0, 0), (5, -2, 3)])` produced `tests/test_calc.py::test_add_params[1-2-3]`, `[0-0-0]`, `[5--2-3]` plus the plain `test_add_plain`, with `skipped_tests: []`.

## Fix applied during verification

- `.claude/hooks/reindex-after-edit.sh` — strip `\r` from `read -r` output on Windows so the hook's dry-run and real invocation paths don't carry stray carriage returns into the `update --files` argument list. Two hook tests now pass; full suite green.

## Limitations

- I could not execute pytest or `python -m code_index` verification commands in this session because every shell command failed at the sandbox process-launch layer with `CreateProcessAsUserW failed: 5`. The code and tests were implemented, but the required green test-suite and cross-task smoke outputs are not available from this run.
- Pytest runner expansion is intentionally best-effort over captured `@pytest.mark.parametrize` literals. Non-literal cases are reported in `skipped_tests`; truncated decorators emit only captured cases and report that limit.
- `symbol --references` reports call-site occurrences for resolved `calls` relations only. It does not add reference occurrences for `imports`, `inherits`, or `contains`.

## Other checks

- No existing test file was renamed or deleted; this slice only added new test files.
- `.claude/settings.json` PostToolUse matcher is `Edit|Write|MultiEdit`.
