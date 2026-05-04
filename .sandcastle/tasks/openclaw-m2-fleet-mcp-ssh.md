# OpenClaw M2 Slice: Fleet MCP And CMA SSH Recovery

## Goal

Implement the Milestone 2 read-heavy Fleet Controller MCP surface plus the CMA SSH recovery allowlist.

## Scope

Owned paths:

- `code_index/openclaw_controller/**`
- `code_index/commands/mcp_*` only for exposing a separate fleet MCP surface
- `tests/openclaw_controller/**`
- `tests/test_mcp_*` only for fleet MCP coverage
- `docs/openclaw/**` runbook updates

Do not edit provider registry, Sandcastle, or OpenClaw context-manager internals in this slice unless a minimal integration hook is required.

## Required Behavior

1. Expose read-heavy fleet tools: `fleet_task_status`, `fleet_query_agent_states`, `fleet_submit_handoff`, `fleet_query_fumemory`, `fleet_get_context_manifest`, and `fleet_publish_work_summary`.
2. Keep write operations gated through signed command references or explicit controller methods. No unrestricted shell, lease mutation, task assignment, or cancellation tool in v1.
3. Use per-client credentials/scopes for HTTP fleet MCP mode, mirroring the existing MCP bearer-token posture where practical.
4. Add CMA SSH recovery policy with exactly four allowed command kinds: `health-check`, `process-check`, `service-restart`, and `index-update`.
5. Require Fleet Controller confirmation that the target host is stale, has no active local file claims, and has no active leases before producing or executing an SSH recovery command.
6. Record SSH recovery attempts as auditable fleet/controller events; tests may use fakes.

## Acceptance Criteria

- The fleet MCP surface can describe its tools without exposing generic `update`, `assign`, `cancel`, shell, or lease mutation tools.
- `fleet_query_agent_states` returns the Fleet Context Graph projection from existing M1 stores.
- `fleet_submit_handoff` follows the existing handoff authorization constraints.
- SSH recovery rejects unknown commands and rejects active hosts, hosts with active leases, or hosts with active claims.
- The docs contain Windows operations guidance for the four-command allowlist.

## Verification

Run:

```bash
python3 -m pytest tests/openclaw_controller -q
python3 -m pytest tests/test_mcp_readonly_default.py tests/test_mcp_auth.py -q
python3 -m pytest tests -q
```

Commit the completed work on your Sandcastle branch and output `<promise>COMPLETE</promise>` when done.
