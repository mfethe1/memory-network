# code_index Codex Guidance

This repository builds `code_index`, a local-first code-memory and graph
control plane for coding agents. Treat symbols, occurrences, and relations as
the semantic spine; chunks are retrieval/context projections.

## Default Workflow

- Prefer `python -m code_index ... --json` for repo intelligence before broad
  manual scanning.
- Use `python -m code_index doctor --json` when the index state is uncertain.
- Use `python -m code_index symbol NAME --json` for symbol lookup.
- Use `python -m code_index grep PATTERN --json` for literal, regex, or path
  search.
- Use `python -m code_index query "phrase" --json` for ranked retrieval.
- Use `python -m code_index impact SYMBOL --json` before shared or risky edits.
- Use `python -m code_index tests SYMBOL --runner pytest` for affected pytest
  node ids when test edges exist.
- After material edits, run `python -m code_index update --files <paths...>`
  unless a host hook already did it.

## Codex Tool Setup

The repo-local plugin package is in `plugins/code-index-agent`. Install it into
a target repo with:

```bash
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
```

Start the browser graph and local command adapter with:

```bash
python -m code_index agent-plugin start --root . --provider codex
```

The same adapter registry supports `claude`, `kimi`, `opencode`, and custom
commands. Inspect the active presets with:

```bash
python -m code_index agent-adapter --list-providers --json
```

## Engineering Rules

- Inspect the actual repo before locking runtime, parser, or dependency
  choices.
- Keep changes scoped and JSON-first for agent-facing interfaces.
- Do not make chunks the primary identity layer.
- Keep `init`, `update`, and `watch` on the shared indexing pipeline.
- Run targeted verification before claiming completion.

## Skill Routing

- Use `setup-matt-pocock-skills` inside project repos before using the Matt
  Pocock engineering skills there.
- Use `tdd` for implementation work where behavior can be driven by tests.
- Use `diagnose` for bugs and regressions, with a concrete repro or pass/fail
  signal before broad changes.
- Use `triage`, `to-prd`, and `to-issues` for issue-tracker work.
- Use `improve-codebase-architecture` and `zoom-out` for architecture mapping
  and refactor planning.
- Use `sandcastle-orchestration` only for repeatable, sandboxed,
  branch-aware AFK agent workflows.

## Sandcastle Setup

This repo includes a cross-platform `.sandcastle/` configuration for
branch-safe AFK agent runs via Docker (Windows WSL2/macOS/Linux) or Podman.

```bash
# Configure
npm install
cp .sandcastle/.env.example .sandcastle/.env
# Edit .sandcastle/.env and add ANTHROPIC_API_KEY

# Run
npm run sandcastle:plan
npm run sandcastle:implement
npm run sandcastle:review
```

See `.sandcastle/README.md` for full platform-specific instructions.
