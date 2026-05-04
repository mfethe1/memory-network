# OpenClaw M2 Slice: Cursor And Open Source Provider Adapters

## Goal

Add the Milestone 2 provider registry and adapter support for Cursor SDK sidecar plus OpenCode, Goose, Aider, and OpenHands.

## Scope

Owned paths:

- `code_index/agent_providers.py`
- `code_index/commands/agent_adapter_cmd.py`
- provider adapter docs/tests such as `docs/agent-provider-adapters.md` and `tests/test_agent_adapter_cmd.py`

Do not edit OpenClaw context manager, Fleet Controller MCP, Sandcastle, or host daemon internals in this slice.

## Required Behavior

1. Keep existing Claude, Codex, Kimi, Custom, and OpenCode behavior compatible.
2. Add a Cursor provider that routes through a Node sidecar command rather than making Cursor SDK the only provider path.
3. Add built-in provider presets for Goose, Aider, and OpenHands with conservative capabilities that match their command shapes.
4. Represent provider capabilities explicitly enough for host heartbeats and Fleet Controller eligibility to distinguish `task_run`, `fresh_session`, `mcp_config_file`, `provider_prompt_file`, `task_json_file`, `inline_provider_prompt`, `json_output`, and stream/event parsing.
5. Add output parser coverage for any new provider event shapes you introduce. If a provider has no stable JSON event format, keep a safe generic parser and document the limitation.

## Acceptance Criteria

- `python3 -m code_index agent-adapter --list-providers --json` includes `cursor`, `opencode`, `goose`, `aider`, and `openhands`.
- Existing provider registry tests pass with updated ordering expectations.
- Host daemon heartbeat provider payload includes the new providers without crashing if their CLIs are missing.
- Cursor is optional and sidecar-backed; failure to install Cursor SDK cannot break Claude/Kimi/Codex.

## Verification

Run:

```bash
python3 -m pytest tests/test_agent_adapter_cmd.py tests/openclaw_hostd/test_heartbeat.py -q
python3 -m code_index agent-adapter --list-providers --json
python3 -m pytest tests -q
```

Commit the completed work on your Sandcastle branch and output `<promise>COMPLETE</promise>` when done.
