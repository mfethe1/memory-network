# Claude Code setup

This repo wires `code_index` into Claude Code's native `.claude/` layers so
future sessions use it as the default repo-intelligence layer without
re-prompting.

## What is loaded automatically

Every session reads these in order:

1. **`.claude/CLAUDE.md`** — always-on defaults (when to prefer `code_index`,
   verification expectations, architecture in one paragraph).
2. **`.claude/rules/*.md`** — path-scoped rules. Loaded only when Claude
   touches files matching the `paths:` frontmatter.
   - `code-index-python.md` → `**/*.py` and `**/*.pyi`
   - `code-index-tests.md` → `tests/**/*.py`, `test_*.py`, `*_test.py`,
     `conftest.py`
3. **`.claude/settings.json`** — hooks/env for the project.

## The `code-index` skill

`.claude/skills/code-index/SKILL.md` is the detailed playbook. It is
**not** loaded into every session — it activates:

- **Automatically** when Claude classifies a request as "repo exploration,
  symbol lookup, structural query, impact, tests, or retrieval." The
  skill's `description` is what Claude uses to decide.
- **Manually** via `/code-index` from the user or another agent.

The skill teaches the full request-classification table, when to use
`impact` + `tests` before refactors, and how to fall back gracefully when
optional deps (tree-sitter, ripgrep) are missing.

## Automatic reindex on edit

`.claude/settings.json` registers a `PostToolUse` hook for
`Edit|Write|MultiEdit`:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit", "hooks": [
        { "type": "command",
          "command": ".claude/hooks/reindex-after-edit.sh",
          "run_in_background": true } ] } ]
  }
}
```

The script (`.claude/hooks/reindex-after-edit.sh`):

- Reads the tool input's `file_path` and MultiEdit `edits[].file_path` values
  from JSON on stdin.
- Skips `.git/`, `.code_index/`, `.claude/`, caches, virtualenvs, node_modules,
  dist/build/target, and binary artifacts.
- Only runs when `.code_index/index.db` already exists (`init` is a user
  action; the hook doesn't create an index spontaneously).
- Calls `python -m code_index update --files <rel_path...> --json` once with
  all surviving paths.
- Stays silent on success. On failure, emits a single actionable line to
  stderr so Claude can surface it.

## MCP read-only default

`code_index mcp-serve` ships with a **read-only tool surface by default**.
A connected agent sees only the retrieval and analysis tools (`search_text`,
`search_query`, `search_ast`, `find_symbol`, `impact`, `affected_tests`,
`doctor`, `ask`). The mutating tools (`update`, `rebuild_fts`) are NOT
registered and cannot be invoked. (`ask` dispatches to lookup, impact,
tests, grep, FTS, similar, repo-map, and doctor — never to mutating
primitives.)

To expose the mutating tools, start the server with `--allow-writes`:

```bash
code_index mcp-serve --allow-writes
```

When this flag is set the server prints a one-line warning to stderr
(`mcp-serve: mutating tools enabled via --allow-writes`) and the `update`
and `rebuild_fts` tool descriptions are prefixed with `MUTATING —` so the
model sees the warning in its tool list.

Rationale: a coding agent can accidentally trigger an expensive full
reindex, repair FTS while another workflow expects stale state, or mutate
local state during a read-only investigation. Requiring an explicit flag
keeps the default safe.

`describe_surface()` (via `mcp-serve --describe`) reflects the actually
exposed surface — it honours `--allow-writes` too, so `--describe` alone
returns only the read-only tools.

## Verifying the configuration

Run these in Claude Code:

- `/memory` — lists currently loaded memory (`CLAUDE.md` + active rules).
  Confirms the path-scoped rules are included when editing Python files.
- `/code-index` — loads the skill explicitly. Useful for checking the
  playbook or teaching a new teammate.
- `code_index doctor --json` — confirms the index is healthy, which is
  what the hook relies on.
- Make a trivial edit to a `.py` file. Watch for a reindex happening in
  the background (or a single stderr line if it failed).

## If skills do not appear

If `.claude/skills/` did not exist when the current session started, the
skill directory may not be watched for this session. Restart Claude Code
once after adding `.claude/skills/` for the first time.

## User-scope mirroring (optional)

To make this default across every repo on a developer machine (not just
this one):

- Mirror `CLAUDE.md`'s always-on guidance into `~/.claude/CLAUDE.md`
  (generalized — drop references to this repo's files).
- Install a user-scope skill at `~/.claude/skills/code-index/SKILL.md` for
  personal use; or leave it project-scoped.

Team-wide distribution beyond one repo is the Anthropic Claude Code plugin
system's job — not covered here.

## What is NOT configured

On purpose, to keep the configuration minimal:

- No `Stop` hook (too chatty for this workflow).
- No `UserPromptSubmit` hook.
- No `PreToolUse` guards beyond what Claude Code already does.
- No legacy `.claude/commands/*` — skills are the recommended path.
- No embeddings / MCP server wiring (the index itself doesn't ship those
  yet).

## What the indexer currently handles (truthful reality check)

Each of these has test coverage in `tests/`:

- **Relative imports** — `from . import X`, `from ..pkg import Y`,
  `from .sub.mod import Z` resolve against the parsing file's package path.
- **Dead-edge repair** — when a target symbol is tombstoned, the edge is
  moved into `unresolved_calls` and the backfill pass repairs it on a later
  reindex if a same-canonical-name symbol reappears. No global rename
  inference is attempted.
- **Class-qualified calls** — `ClassName.method()` inside that class's own
  body resolves internally without needing `self` / `cls`.
- **Subscripted generic bases** — `class Foo(Mapping[str, int])` emits
  `inherits Mapping` (not a broken edge on the subscript).
- **Targeted-update convergence** — `update --files A.py` resolves B's
  previously-unresolved edges if A introduced the missing symbol, without
  needing `update --all`.
- **Watch filter hardening** — watch and the `PostToolUse` hook reject the
  full binary/build/cache/editor-junk set, including the index DB's own
  WAL/SHM files. The filter is a pure function in
  `code_index.commands.watch_cmd.should_skip_watch_event` and is test
  covered.
- **Pytest runner emitter** — `code_index tests X --runner pytest` prints one
  pytest node id per line and expands captured literal `parametrize` cases.
  `--runner-json` returns `{runner, invocation, node_ids, skipped_tests}`.
- **MultiEdit hook support** — the PostToolUse hook handles Edit, Write,
  current single-file MultiEdit, and multi-file MultiEdit payloads, then calls
  `update --files` once for the deduplicated non-ignored path list.
- **Reference occurrences** — resolved `calls` relations also write
  `occurrences(role="reference", syntax_kind="call")`; `code_index symbol X
  --references --json` returns up to 50 call-site file/line spans.
- **MCP server** — `code_index mcp-serve` speaks stdio MCP. The default
  surface is **read-only**: eight tools are exposed (`search_text`,
  `search_query`, `search_ast`, `find_symbol`, `impact`, `affected_tests`,
  `doctor`, `ask`) plus four resources (`codeindex://repo-map`,
  `codeindex://doctor`, `codeindex://symbol/<canonical>`,
  `codeindex://chunk/<chunk_uid>`). Pass `--allow-writes` to additionally
  expose the mutating tools `update` and `rebuild_fts`; their descriptions
  are prefixed with `MUTATING —` so agents see the warning in the tool
  list. `--describe` prints the surface as JSON without starting the loop
  and honours `--allow-writes` too.
- **Git-hook installer** — `code_index install-hooks` writes post-commit,
  post-checkout, post-merge, and post-rewrite scripts under
  `.code_index/hooks` and wires `git config core.hooksPath`. Idempotent;
  `--uninstall` reverses it.
- **Custom parametrize ids** — `@pytest.mark.parametrize(..., ids=[...])` with
  a literal list is captured and flows through to `code_index tests
  --runner pytest` verbatim. Callable `ids=` is flagged and node ids fall
  back to value-based form with a `skipped_tests` note.
- **FTS `ok` semantics** — `doctor.fts_consistency.ok` follows the
  rebuild recommendation, not raw drift. Small drift below the threshold is
  benign because the retrieval layer filters tombstones via SQL.
