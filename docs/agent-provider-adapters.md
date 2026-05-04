# Agent Provider Adapters

`code_index` exposes one graph/task adapter contract and keeps provider
differences in a small registry. Codex is the default target, but the same
task JSON, MCP config, provider prompt, callback URL, and event normalization
path work for Claude, Kimi, OpenCode, Cursor sidecars, OpenHands, Goose,
Aider, and custom local commands.

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
| `cursor` | `cursor-agent-sidecar run --root <root> --task-json ...` | Cursor sidecar JSONL |
| `goose` | `goose run --instructions <provider-prompt-file> --no-session` | Generic line parser |
| `aider` | `aider --yes-always --message-file <provider-prompt-file> ...` | Generic line parser |
| `openhands` | `openhands --headless --json -f <provider-prompt-file>` | OpenHands JSONL |

All provider output is normalized into graph events:
`read`, `edit`, `test`, `tool`, `navigate`, `note`, `decision`, and `status`.

Built-in capabilities are explicit so host heartbeats and Fleet Controller
eligibility can reason about them directly. Common examples:

- `task_run` and `fresh_session`: the preset can execute one task in a fresh local run.
- `mcp_config_file`, `provider_prompt_file`, `task_json_file`, and `inline_provider_prompt`: which adapter placeholders the preset consumes.
- `json_output` and `stream_json_output`: whether the provider emits JSONL/JSON events or stream JSON.
- `provider_event_parser` vs `generic_text_parser`: whether `code_index` has provider-specific event parsing or falls back to safe line-based normalization.

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
python -m code_index agent-plugin start --root . --provider cursor
python -m code_index agent-plugin start --root . --provider goose
python -m code_index agent-plugin start --root . --provider aider
python -m code_index agent-plugin start --root . --provider openhands
```

`cursor` is intentionally sidecar-backed. The built-in preset expects a
`cursor-agent-sidecar` command on `PATH` that wraps the optional Cursor SDK or
Cursor CLI locally; missing Cursor tooling does not affect other providers.

`goose` and `aider` use documented one-shot CLI entrypoints, but the adapter
does not rely on a stable machine-readable event stream from either tool yet.
Their output is still safe to run through the generic line parser, but event
granularity is lower than the JSON-backed providers.

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
