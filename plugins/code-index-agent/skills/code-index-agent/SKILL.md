---
name: code-index-agent
description: Use code_index for repository navigation, graph supervision, MCP-backed code retrieval, impact analysis, affected tests, and browser-submitted coding-agent tasks.
---

# Code Index Agent

Use this skill when the user asks for repo exploration, graph navigation,
symbol lookup, impact analysis, affected tests, coding-agent oversight, or
browser-driven task submission.

## First Checks

1. Confirm the repo has an index:

```bash
python -m code_index doctor --json
```

If there is no `.code_index/index.db`, run:

```bash
python -m code_index init --json
```

2. For code lookup, prefer the narrowest primitive:

| Need | Command |
| --- | --- |
| Find exact text or paths | `python -m code_index grep "pattern" --json` |
| Find a symbol | `python -m code_index symbol Name --json` |
| Ask a natural-language repo question | `python -m code_index ask "question" --json` |
| Build an agent handoff packet | `python -m code_index context "task" --json` |
| Understand blast radius | `python -m code_index impact Symbol --json` |
| Find affected tests | `python -m code_index tests Symbol --runner pytest` |
| Open the browser graph | `python -m code_index graph-server --port 8767` |

## Live Graph Control Plane

Install repo-local integration files:

```bash
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
# or use Kimi Code CLI for browser-submitted coding tasks
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider kimi --json
```

Start the graph server:

```bash
python -m code_index graph-server --port 8767
```

Open:

```text
http://127.0.0.1:8767/repo-graph.html
```

To let graph-submitted tasks launch a local agent command, set
`CODE_INDEX_AGENT_PROVIDER=claude`, `CODE_INDEX_AGENT_PROVIDER=codex`, or
`CODE_INDEX_AGENT_COMMAND` before starting the server. Useful command
placeholders: `{message}`, `{run_id}`, `{root}`, `{task_json}`,
`{selected_paths}`, and `{selected_nodes}`.

Examples:

```bash
CODE_INDEX_AGENT_PROVIDER=claude python -m code_index graph-server --port 8767
CODE_INDEX_AGENT_PROVIDER=codex python -m code_index graph-server --port 8767
```

The browser cancel button interrupts the local command process tree and records
the run as `cancelled`. HTTP webhook dispatch still works through
`CODE_INDEX_AGENT_WEBHOOK_URL` when a separate adapter service is preferred.
Submitted task JSON includes `context_packet`; provider output can emit JSON
events or prefixed lines such as `EDIT path/to/file.py message` to update the
graph with structured activity. Inspect long runs with:

```bash
python -m code_index agent transcript --run-id <run-id> --json
```

Completed or failed runs include post-run suggestions in the transcript:
touched-file diagnostics, affected pytest node ids when indexed, and a broader
test warning when no affected-test edge is available.

## MCP

The plugin ships `.mcp.json` for agent-neutral access:

```bash
python -m code_index mcp-serve --root .
```

Default MCP mode is read-only. Use `--allow-writes` only when the user
explicitly wants an MCP client to mutate index state.

## Safety Defaults

- Keep the graph server bound to `127.0.0.1` unless the user explicitly asks for
  remote access.
- Use `CODE_INDEX_GRAPH_TOKEN` when browser POSTs or adapter callbacks need a
  bearer token. Do not put graph tokens in URLs; browser sessions are created
  through the local auth prompt and same-origin cookie.
- Do not run broad reindexes during active coding unless `doctor` reports drift
  or the user asks for a full refresh.
