# code_index — project memory

This repository ships `code_index`, a local-first hybrid code-memory system.
Use it as the default repo-intelligence layer. Treat symbols/relations as the
semantic spine and chunks as retrieval projections. `symbol_uid` is a
deterministic declaration key derived from `language`, `kind`,
`canonical_name`, `signature_norm`, and `container_uid` — stable across
re-parses of the same declaration, but NOT refactor-durable. Migrate
identity across explicit refactors with `code_index update --rename-map
file.json` (preserves `symbol_pk` + all FK edges).

## Always-on defaults

- Prefer `code_index` over raw file exploration for:
  - symbol lookup → `code_index symbol NAME`
  - structural patterns → `code_index query --ast class|function|method|call|import`
  - literal / regex / path search → `code_index grep PATTERN [--path GLOB]`
  - ranked token retrieval → `code_index query "phrase"`
  - **semantic similarity** ("find code like this") → `code_index similar "phrase"` (requires `code_index embed` first)
  - blast radius of a change → `code_index impact SYMBOL`
  - what tests touch a symbol → `code_index tests SYMBOL`
  - ready-to-run pytest node ids → `code_index tests SYMBOL --runner pytest`
  - **compact repo overview for LLM priming** → `code_index repo-map --format text --limit 50`
  - index health and drift → `code_index doctor` (now reports `git`, `embeddings`, and `jedi` blocks)
  - agent-neutral access over the index → `code_index mcp-serve` (stdio MCP)
  - one-time git hook wiring → `code_index install-hooks`
- Every subcommand accepts `--json`. Use it for anything you plan to parse.
- After material file edits, run `code_index update --files <path...>`. Targeted
  updates backfill previously unresolved cross-file calls automatically. The
  PostToolUse hook handles Edit, Write, and MultiEdit payloads when the index
  already exists.
- When the FTS index accumulates drift (`doctor` flags it), run
  `code_index rebuild-fts`.

## When raw exploration is still correct

- One-off config files or data (JSON/YAML/CSV) the indexer intentionally leaves
  as heuristic chunks.
- Investigating a brand-new file before it is indexed.
- Anything outside this repo.

## Verification before claiming done

- Do not claim completion without evidence. Run the smallest command that
  proves the claim (e.g. `code_index tests`, `pytest -q`, `impact`) and cite
  it.
- If a capability is missing or degraded, say so. `impact` and `tests` both
  return a `limitations` list — surface it if it matters.

## Architecture in one paragraph

Python stdlib `ast` is the semantic source of truth for Python. Tree-sitter is
structural support only (`query --ast`). SQLite + FTS5 is the local control
plane; ripgrep is the lexical fast path with a robust resolver. `symbol_uid`
is the primary identity; `chunk_uid` is secondary. One shared pipeline drives
init/update/watch. Unresolved cross-file calls persist in `unresolved_calls`
and backfill on later reindex runs. Dead edges (live src → tombstoned dst)
are also moved into `unresolved_calls` so they heal when a same-named symbol
reappears — no global rename inference. Relative imports
(`from . import X`, `from ..pkg import Y`) resolve against the parsing file's
package path. `@pytest.mark.parametrize` cases are captured on the test
chunk and surfaced by `code_index tests`; `--runner pytest` expands captured
literal cases into node ids. Resolved call relations also emit
`occurrences(role="reference", syntax_kind="call")`, exposed through
`code_index symbol NAME --references --json`.

## Deep-dive references

- Full playbook: `/code-index` skill (`.claude/skills/code-index/SKILL.md`).
- Authoritative spec: `docs/code-index-spec.md`.
- Implementation plan: `plans/code-index-repo-plan.md`.
- Claude Code setup walkthrough: `docs/claude-code.md`.
