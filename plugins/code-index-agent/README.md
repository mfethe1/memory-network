# Code Index Agent Plugin

Repo-local plugin package for `code_index`.

It gives Claude, Codex, and other MCP-capable systems the same control plane:

- read-only MCP retrieval by default
- graph-server browser UI for repo navigation and task oversight
- browser-submitted tasks through HTTP webhooks or a local command adapter
- task-aware context packets attached to submitted runs
- run transcripts and decision ledger inspection
- structured provider output parsing for read/edit/test/status events
- post-run diagnostic and affected-test suggestions
- repo-local installer for MCP, Claude settings, and launcher config
- process-tree cancellation for local command runs

## Install Repo-Local Config

```bash
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
```

This writes `.mcp.json`, `.claude/settings.local.json`,
`.code_index/agent-plugin.json`, `.code_index/start-code-index-agent.ps1`,
`.code_index/start-code-index-agent.sh`, and a demo task JSON. Use
`--provider claude`, `--provider codex`, or `--agent-command "your command"`.

## Start MCP

```bash
python -m code_index mcp-serve --root .
```

The plugin `.mcp.json` exposes that command as the `code-index` MCP server.

## Start The Live Graph

```bash
python -m code_index agent-plugin start --root . --port 8767
```

Open `http://127.0.0.1:8767/repo-graph.html`.
The launcher initializes `.code_index/index.db` when the target does not have
one yet. It also injects this source tree into `PYTHONPATH`, so the same
script can serve another local codebase without installing `code_index` first:

```bash
python -m code_index agent-plugin start \
  --root E:/Projects/other-repo --scope src/auth --port 8767 --provider codex
```

`--root` owns the repo/index. `--scope` is optional and starts graph focus,
search, and browser task defaults inside that directory without shrinking the
whole-repo index.

Use `--refresh-index` to rescan a target that already has an index. The
launcher checks that configured provider commands are on `PATH`; use
`--check-only` to validate setup without starting the server.

## Launch Agent Tasks From The Browser

Claude example:

```bash
python plugins/code-index-agent/scripts/start_graph_server.py --root . --port 8767 --provider claude
```

Codex example:

```bash
python plugins/code-index-agent/scripts/start_graph_server.py --root . --port 8767 --provider codex
```

Use `--agent-command` when another local agent needs a custom command. Command
templates support `{message}`, `{run_id}`, `{root}`, `{task_json}`,
`{selected_paths}`, and `{selected_nodes}`. The adapter posts process output
back as graph events and marks the run `completed`, `failed`, or `cancelled`.
Submitted task JSON includes a `context_packet` so agents start with the
selected graph node, matching chunks, notes, and recent work history.
Completed or failed runs include post-run suggestions in the transcript:
diagnostics for touched files and pytest node ids derived from affected-test
edges.

## Demo

See `demo/README.md` and `demo/demo-task.json` for a short browser task flow.

## When This Is Useful

This is valuable when a coding project has enough files, symbols, and test
surface area that plain file browsing becomes expensive or lossy. The graph is
not a replacement for an agent. It is the control plane: context selection,
active-run oversight, impact lookup, affected-test lookup, and cancellation.

The highest-value next step is packaging polish: screenshots, provider
availability checks, and a one-command installer for host-specific settings.
