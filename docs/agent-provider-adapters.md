# Agent Provider Adapters

`code_index` exposes one graph/task adapter contract and keeps provider
differences in a small registry. Codex is the default target, but the same
task JSON, MCP config, provider prompt, callback URL, and event normalization
path work for Claude, Kimi, OpenCode, and custom local commands.

## Inspect Providers

```bash
python -m code_index agent-adapter --list-providers --json
```

The JSON payload contains each provider id, display name, command preset, and
capabilities. Built-ins:

| Provider | Command shape | Output parsed |
| --- | --- | --- |
| `codex` | `codex exec -C <root> -s workspace-write --json ...` | Codex JSONL |
| `claude` | `claude -p --output-format stream-json --mcp-config ...` | Claude stream JSON |
| `kimi` | `kimi --print --output-format stream-json --mcp-config-file ...` | Kimi stream JSON |
| `opencode` | `opencode run --dir <root> --format json ...` | OpenCode JSONL |

All provider output is normalized into graph events:
`read`, `edit`, `test`, `tool`, `navigate`, `note`, `decision`, and `status`.

## Install Into A Target Repo

```bash
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
python -m code_index agent-plugin start --root . --provider codex
```

Use another preset by changing `--provider`:

```bash
python -m code_index agent-plugin start --root . --provider claude
python -m code_index agent-plugin start --root . --provider kimi
python -m code_index agent-plugin start --root . --provider opencode
```

Use `--agent-command` for any tool that can accept a prompt and run in the
workspace:

```bash
python -m code_index agent-plugin start --root . \
  --agent-command "my-agent --cwd {root} --task {task_json}"
```

Supported placeholders include `{message}`, `{provider_prompt}`,
`{provider_prompt_file}`, `{last_message}`, `{mcp_config_file}`, `{run_id}`,
`{root}`, `{task_json}`, `{selected_paths}`, and `{selected_nodes}`.

## Add A Provider Without Code Changes

Set `CODE_INDEX_AGENT_PROVIDER_SPECS` to one or more JSON files separated by
the platform path separator. Each file can contain either one provider object
or a `providers` list:

```json
{
  "providers": [
    {
      "id": "example",
      "display_name": "Example Agent",
      "command_preset": "example-agent --root {root} {provider_prompt}",
      "capabilities": ["inline_provider_prompt", "json_output"]
    }
  ]
}
```

The registry appends these providers after the built-ins. If an id matches a
built-in provider, the later spec overrides that provider while preserving its
position in the picker.

## MCP Posture

Every command adapter writes a per-run MCP config at
`.code_index/agent-runs/<run-id>/mcp.json` that exposes:

```bash
python -m code_index mcp-serve --root <repo>
```

The MCP surface is read-only by default. Use `--allow-writes` only when the
host system explicitly wants MCP clients to mutate index state.
