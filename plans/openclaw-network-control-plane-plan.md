# OpenClaw Network Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend Graph Agent Companion into an OpenClaw network control plane
for Windows PCs reachable over SSH/private networking, while syncing durable
agent memory into `github.com/mfethe1/fumemory` on Railway.

**Architecture:** Keep each PC's local `code_index` graph-server and SQLite
store authoritative for local run status, transcripts, process state, file
claims, and graph context. Add a thin OpenClaw host daemon per PC for outbound
fleet coordination, use NATS JetStream/KV for task/event transport and
host/repo/task leases, and sync long-term summaries into `fumemory`.

**Tech Stack:** Python `code_index`, Windows Service host daemon, Windows
OpenSSH, Tailscale/private networking, NATS JetStream/KV, Model Context
Protocol, OpenTelemetry, Claude/Kimi/Codex/OpenCode/Goose provider adapters,
Cursor TypeScript SDK via a Node sidecar, and `fumemory` on Railway.

---

## Status

Research-backed planning document created on 2026-05-03.

Research inputs used:

1. Tavily searches for Cursor SDK, Cursor CLI/API, Open Source agent control
   planes, Windows SSH, NATS JetStream/KV, and distributed lease patterns.
2. Brave searches for Cursor public beta details, self-hosted Cursor agents,
   OpenCode/Goose/OpenHands/Aider, and Tailscale/OpenSSH Windows posture.
3. Two subagents:
   - Cursor SDK research and integration plan.
   - Open source tooling and distributed control-plane research.
4. Claude CLI architecture critique.
5. Kimi CLI architecture critique.
6. Local repo inspection with `code_index doctor --json` and
   `code_index agent-adapter --list-providers --json`.

Important local facts:

1. The repo already has provider adapter presets for `claude`, `codex`,
   `kimi`, `opencode`, and `custom`.
2. `claude --version` reported Claude Code `2.1.123`.
3. `kimi --version` reported Kimi CLI `1.41.0`.
4. `code_index doctor --json` reported a healthy index.
5. Direct standalone `agent-adapter --provider ...` execution requires a graph
   callback URL, so external CLI review was run directly through `claude -p`
   and `kimi --print`.

## Research Sources

Primary and high-signal sources:

1. Cursor SDK release:
   https://cursor.com/changelog/sdk-release
2. Cursor TypeScript SDK announcement:
   https://cursor.com/blog/typescript-sdk
3. Cursor cookbook:
   https://github.com/cursor/cookbook
4. Cursor self-hosted agents:
   https://cursor.com/blog/self-hosted-cloud-agents
5. Cursor CLI docs:
   https://cursor.com/docs/cli/overview
6. NATS JetStream:
   https://docs.nats.io/nats-concepts/jetstream
7. NATS KV:
   https://docs.nats.io/nats-concepts/jetstream/key-value-store
8. Railway private networking:
   https://docs.railway.com/private-networking
9. Railway TCP proxy:
   https://docs.railway.com/reference/tcp-proxy
10. Microsoft OpenSSH on Windows:
   https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse
11. Tailscale SSH:
   https://tailscale.com/docs/features/tailscale-ssh
12. MCP specification:
   https://modelcontextprotocol.io/specification/latest
13. MCP transports:
   https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
14. OpenTelemetry Collector:
   https://opentelemetry.io/docs/collector/
15. OpenTelemetry GenAI semantic conventions:
   https://opentelemetry.io/docs/specs/semconv/gen-ai/
16. Distributed locks and fencing background:
   https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
17. fumemory repository:
   https://github.com/mfethe1/fumemory
18. OpenCode:
   https://github.com/opencode-ai/opencode
19. Goose:
   https://github.com/aaif-goose/goose
20. OpenHands:
   https://github.com/OpenHands/OpenHands
21. Aider:
   https://github.com/Aider-AI/aider

## Accepted Claude And Kimi Review Corrections

Claude CLI review changes accepted:

1. NATS authentication and subject ACLs are Phase 0 requirements, not later
   hardening.
2. JetStream persistence must be explicit. Railway is acceptable for
   `fumemory`, but risky as the hot event broker unless persistent volume,
   retention, backup, and restart behavior are proven.
3. Leases and fencing must exist before cross-host remote dispatch.
4. The Windows daemon needs an explicit service, firewall, secrets, and path
   model.
5. Cursor SDK must be treated as beta and optional until local/headless/cloud
   constraints are tested.

Kimi CLI review changes accepted:

1. Keep local `graph-server` and SQLite authoritative on each PC.
2. Do not distribute file-level claims through NATS in the first version.
3. Coordinate host/repo/task-level leases first.
4. The host daemon should be a thin wrapper around local `graph-server`, not a
   replacement.
5. Cursor SDK should run behind a worker-process adapter so the existing
   process registry can still observe a real child process.
6. MCP should stay local stdio for `code_index` tools; expose central memory
   and fleet tools over authenticated HTTP only where needed.

## Non-Goals For The First Version

1. Do not replace the local graph-server.
2. Do not make `fumemory` the source of truth for process liveness.
3. Do not make Railway private networking the assumed path to Windows PCs.
4. Do not expose every PC's local MCP tools directly over the network.
5. Do not implement cross-host file-level locks in the first release.
6. Do not depend on Cursor SDK as the only provider path.
7. Do not add Temporal before the basic task/event/lease model is stable.

## Target Architecture

### Module: OpenClaw Host Daemon

Runs on each Windows PC.

Responsibilities:

1. Register host identity and capabilities.
2. Start or verify local `code_index graph-server`.
3. Connect outbound to fleet NATS and HTTPS endpoints.
4. Publish heartbeats and capability changes.
5. Consume assigned task messages.
6. Forward tasks to the local graph-server/provider adapter.
7. Publish run events, transcript chunks, status changes, and verification
   results.
8. Sync durable memory summaries to `fumemory`.
9. Store secrets through Windows DPAPI or Credential Manager.
10. Keep local rotating logs when the network or collector is unavailable.

The daemon is not responsible for:

1. Owning local file claims.
2. Rewriting local terminal run status from fleet state.
3. Exposing unrestricted shell access.
4. Holding provider credentials in plain text.

### Module: Local Graph Server

The existing graph-server remains authoritative per host.

Responsibilities:

1. Local `AgentRun` status.
2. Local run transcript and event history.
3. Local process registry and cancellation.
4. Local file claims and fencing.
5. Retrieval over symbols, occurrences, relations, chunks, tests, and
   transcripts.
6. Local MCP stdio tools for agents.

### Module: Fleet Controller

Runs centrally, likely next to or near `fumemory`.

Responsibilities:

1. Maintain host inventory.
2. Accept task requests from humans, browser UI, scripts, or API clients.
3. Select eligible hosts based on repo, provider capabilities, current leases,
   health, and policy.
4. Assign tasks over NATS.
5. Aggregate fleet run health from host heartbeats and run events.
6. Store fleet-level audit events.
7. Provide dashboard/API views.
8. Push durable memory objects to `fumemory` or accept host-pushed summaries.

The controller may mark a host or run as `unknown` or `stale`, but it must not
write terminal local `AgentRun` status unless process liveness is known false
and no active local claims remain.

### Module: NATS JetStream And KV

Use NATS as the fleet coordination bus, not as the local graph database.

Responsibilities:

1. Durable task assignment streams.
2. Durable run event streams.
3. Replayable audit stream.
4. Host capability key-value records.
5. Host/repo/task-level leases with fencing revisions.

Deployment recommendation:

1. Prefer managed NATS/NATS Cloud or a small dedicated VM with persistent disk
   for the first production-like control plane.
2. Use Railway for `fumemory` and control APIs.
3. Use Railway for NATS only after proving JetStream persistence across
   restarts, deploys, and region/network interruptions.

### Module: fumemory

Use `fumemory` as durable semantic memory.

Responsibilities:

1. Store run summaries.
2. Store decisions.
3. Store failed approaches and remediation notes.
4. Store verification states.
5. Store repo and host preferences.
6. Store cross-run lessons.
7. Provide search/retrieval for long-term memory.

Required sync properties:

1. Idempotency.
2. Backpressure.
3. Retry with bounded local queue.
4. Traceability back to `host_id`, `task_id`, `run_id`, and event offsets.

### Module: Provider Adapters

Provider adapters normalize each coding agent runtime into the same local event
model.

Adapters to keep or add:

1. Claude CLI through existing command adapter.
2. Kimi CLI through existing command adapter.
3. Codex CLI through existing command adapter.
4. OpenCode as a high-priority open source terminal coding agent.
5. Goose as a high-priority MCP-native local operator.
6. Aider as a lightweight patch worker.
7. OpenHands as an optional heavier sandbox worker.
8. Cursor SDK as an experimental provider through a Node sidecar.

Cursor adapter rule:

1. Cursor is an execution provider, not the control plane.
2. The adapter must map local `run_id` to Cursor agent/run IDs.
3. The adapter must stream Cursor events into local transcripts.
4. The adapter must support cancellation and archive/delete lifecycle calls
   only through local run controls.
5. Cursor SDK version must be pinned and upgrade-tested because the SDK is new.

## Data Contracts

### Host Inventory

Suggested central table or document:

```text
openclaw_hosts:
  host_id
  display_name
  machine_id
  os_name
  os_version
  daemon_version
  graph_server_url
  ssh_host
  tailnet_name
  repo_roots_json
  capabilities_json
  provider_caps_json
  last_heartbeat_at
  status
  created_at
  updated_at
```

### Task Envelope

```text
agent_tasks:
  task_id
  source
  prompt
  repo_id
  target_host_id
  selected_paths_json
  context_handles_json
  required_provider
  priority
  status
  created_at
  assigned_at
  completed_at
```

### Provider Run References

```text
provider_run_refs:
  run_id
  provider
  provider_agent_id
  provider_run_id
  runtime
  host_id
  status
  raw_ref_json
  created_at
  updated_at
```

### Lease Payload

Use NATS KV for host/repo/task-level leases.

```text
lease:
  lease_id
  host_id
  run_id
  task_id
  repo_id
  scope
  mode
  fencing_revision
  expires_at
  heartbeat_at
```

### fumemory Sync Payload

```text
memory_sync:
  idempotency_key
  trace_id
  host_id
  repo_id
  task_id
  run_id
  source_event_offsets
  summary
  decisions
  failures
  verification_state
  created_at
```

## NATS Subject Layout

Initial subjects:

```text
openclaw.host.<host_id>.heartbeat
openclaw.host.<host_id>.capabilities
openclaw.task.<host_id>.assigned
openclaw.task.<host_id>.ack
openclaw.run.<host_id>.<run_id>.events
openclaw.run.<host_id>.<run_id>.status
openclaw.run.<host_id>.<run_id>.verification
openclaw.audit.<host_id>
```

Initial KV buckets:

```text
openclaw_hosts
openclaw_leases
openclaw_provider_caps
openclaw_controller_config
```

Subject ACL baseline:

1. Host may publish only its own heartbeat, capabilities, run events, status,
   verification, and audit subjects.
2. Host may consume only its own task assignment subject.
3. Controller may publish task assignments.
4. Controller may consume all host events.
5. No host may consume another host's task stream.
6. No host may publish controller config.

## Implementation Slices

### Slice 0 - Broker, Identity, And Security

**Files:**

1. Create: `docs/openclaw/broker-deployment.md`
2. Create: `docs/openclaw/host-identity.md`
3. Create: `docs/openclaw/nats-subject-acls.md`
4. Modify: `README.md`

Tasks:

- [ ] Pick the NATS deployment target for the first real test. Use managed
      NATS or a persistent VM unless Railway persistence has been proven with
      restart tests.
- [ ] Define host identity fields and enrollment flow.
- [ ] Define per-host NKey/account credentials and subject ACLs.
- [ ] Document Windows secret storage using DPAPI or Credential Manager.
- [ ] Document SSH posture: Windows OpenSSH over private networking for admin
      and break-glass only.
- [ ] Add a short README pointer to the OpenClaw fleet docs.

Verification:

1. A new host can receive credentials that cannot read another host's task
   subject.
2. A compromised host credential cannot publish controller config.
3. NATS restart preserves JetStream task/event data in the selected deployment.

### Slice 1 - Host Daemon Skeleton

**Files:**

1. Create: `code_index/openclaw_hostd/__init__.py`
2. Create: `code_index/openclaw_hostd/config.py`
3. Create: `code_index/openclaw_hostd/identity.py`
4. Create: `code_index/openclaw_hostd/service.py`
5. Create: `code_index/openclaw_hostd/heartbeat.py`
6. Create: `code_index/openclaw_hostd/logging.py`
7. Create: `tests/openclaw_hostd/test_identity.py`
8. Create: `tests/openclaw_hostd/test_heartbeat.py`
9. Modify: `pyproject.toml`

Tasks:

- [ ] Add a small Python package for the host daemon.
- [ ] Add config loading from environment plus a local config file path.
- [ ] Add deterministic `host_id` loading or creation.
- [ ] Add capability detection for OS, repo roots, providers, graph-server
      availability, and SSH host name.
- [ ] Add heartbeat payload generation without requiring network access.
- [ ] Add tests for stable host identity and heartbeat shape.
- [ ] Add CLI entrypoint `code-index-openclaw-hostd`.

Verification:

1. `pytest tests/openclaw_hostd -q` passes.
2. Running the daemon with `--once --json` prints a heartbeat payload.
3. No secret values appear in logs or JSON output.

### Slice 2 - Local Graph Server Adapter

**Files:**

1. Create: `code_index/openclaw_hostd/graph_client.py`
2. Create: `tests/openclaw_hostd/test_graph_client.py`
3. Modify: `code_index/openclaw_hostd/service.py`

Tasks:

- [ ] Implement a local graph-server health check.
- [ ] Implement task submission to local graph-server through the existing
      task/run API.
- [ ] Implement local run status polling.
- [ ] Implement cancellation forwarding.
- [ ] Add tests with a fake local graph-server HTTP handler.

Verification:

1. Fake graph-server receives task JSON with `task_id`, `host_id`, selected
   paths, and provider.
2. Host daemon reports graph-server unavailable without crashing.
3. Cancellation request maps to the local graph-server cancel route.

### Slice 3 - NATS Event Outbox And Task Inbox

**Files:**

1. Create: `code_index/openclaw_hostd/nats_client.py`
2. Create: `code_index/openclaw_hostd/outbox.py`
3. Create: `code_index/openclaw_hostd/inbox.py`
4. Create: `tests/openclaw_hostd/test_outbox.py`
5. Create: `tests/openclaw_hostd/test_inbox.py`
6. Modify: `code_index/openclaw_hostd/service.py`

Tasks:

- [ ] Add NATS client wrapper with explicit connect, publish, subscribe, and
      close lifecycle.
- [ ] Add an outbox that can persist unsent events locally.
- [ ] Add task inbox message validation.
- [ ] Add task ACK publishing.
- [ ] Add replay-safe event sequence numbers.
- [ ] Add tests for idempotent handling of duplicate task messages.

Verification:

1. Duplicate task assignment with the same `task_id` does not create two local
   runs.
2. Outbox keeps events when publish fails.
3. Outbox drains after reconnect.

### Slice 4 - Fleet Leases And Fencing

**Files:**

1. Create: `code_index/openclaw_hostd/leases.py`
2. Create: `tests/openclaw_hostd/test_leases.py`
3. Create: `docs/openclaw/lease-model.md`
4. Modify: `code_index/openclaw_hostd/inbox.py`

Tasks:

- [ ] Add host/repo/task-level lease acquisition.
- [ ] Add fencing revision checks.
- [ ] Add lease renewal.
- [ ] Add lease release on terminal local run status.
- [ ] Fail closed for new cross-host assignments when a conflicting lease
      exists.
- [ ] Keep file-level claims local to graph-server SQLite.

Verification:

1. Two hosts cannot acquire the same exclusive task lease.
2. A stale lower fencing revision cannot release or overwrite a newer lease.
3. Local file claims continue to work without NATS.

### Slice 5 - Fleet Controller API

**Files:**

1. Create: `code_index/openclaw_controller/__init__.py`
2. Create: `code_index/openclaw_controller/app.py`
3. Create: `code_index/openclaw_controller/scheduler.py`
4. Create: `code_index/openclaw_controller/models.py`
5. Create: `tests/openclaw_controller/test_scheduler.py`
6. Create: `tests/openclaw_controller/test_api.py`
7. Modify: `pyproject.toml`

Tasks:

- [ ] Add host inventory model.
- [ ] Add task creation endpoint.
- [ ] Add host eligibility filtering by repo root, provider capability, health,
      and lease availability.
- [ ] Add NATS task publish.
- [ ] Add run health aggregation from heartbeats and events.
- [ ] Add API tests for host selection and rejected assignments.

Verification:

1. Controller assigns a task only to an eligible host.
2. Controller refuses assignment when the repo lease is held elsewhere.
3. Controller marks host health `unknown` or `stale` without mutating local
   terminal run status.

### Slice 6 - fumemory Sync

**Files:**

1. Create: `code_index/openclaw_hostd/fumemory_client.py`
2. Create: `code_index/openclaw_hostd/memory_sync.py`
3. Create: `tests/openclaw_hostd/test_memory_sync.py`
4. Create: `docs/openclaw/fumemory-sync.md`
5. Modify: `code_index/openclaw_hostd/service.py`

Tasks:

- [ ] Add `fumemory` client with timeout and retry policy.
- [ ] Build memory sync payloads from completed or failed runs.
- [ ] Include `idempotency_key`, `trace_id`, `host_id`, `repo_id`, `task_id`,
      `run_id`, and source event offsets.
- [ ] Queue failed syncs locally.
- [ ] Add backpressure limits.
- [ ] Add tests for duplicate sync payloads and retry behavior.

Verification:

1. `fumemory` outage does not block local run completion.
2. Duplicate sync payload is safe.
3. Memory payload can be traced back to the original run.

### Slice 7 - Cursor SDK Provider Adapter

**Files:**

1. Create: `plugins/cursor-agent-sidecar/package.json`
2. Create: `plugins/cursor-agent-sidecar/src/index.ts`
3. Create: `plugins/cursor-agent-sidecar/src/run.ts`
4. Create: `plugins/cursor-agent-sidecar/src/events.ts`
5. Create: `code_index/agent_adapters/cursor.py`
6. Create: `tests/agent_adapters/test_cursor_adapter.py`
7. Create: `docs/openclaw/cursor-sdk-adapter.md`
8. Modify: `pyproject.toml`

Tasks:

- [ ] Pin `@cursor/sdk` in the sidecar package.
- [ ] Implement sidecar commands for create, prompt, stream, wait, cancel,
      archive, and delete where supported by the SDK.
- [ ] Normalize Cursor stream events into local adapter events.
- [ ] Store Cursor agent/run IDs in `provider_run_refs`.
- [ ] Implement cancellation through local run controls.
- [ ] Add fallback behavior when Cursor local runtime is unavailable.
- [ ] Add tests using recorded sidecar JSON instead of live Cursor calls.

Verification:

1. Cursor adapter can run in dry-run mode without Cursor credentials.
2. Recorded Cursor events become local transcript/status events.
3. Cancellation emits a local terminal status exactly once.
4. SDK version is pinned and documented.

### Slice 8 - Open Source Provider Adapters

**Files:**

1. Modify: `docs/agent-provider-adapters.md`
2. Modify: existing provider adapter preset files discovered during
   implementation.
3. Create: focused tests for each changed preset under `tests/`.

Tasks:

- [ ] Add or harden OpenCode preset.
- [ ] Add or harden Goose preset.
- [ ] Document Aider as a lightweight patch worker option.
- [ ] Document OpenHands as a heavier sandbox worker option.
- [ ] Keep provider output normalization shared with existing Claude/Kimi/Codex
      adapter behavior.

Verification:

1. `code_index agent-adapter --list-providers --json` lists provider
   capabilities clearly.
2. Each provider preset can be smoke-tested or gracefully reports missing CLI.
3. Missing provider CLI never crashes the host daemon.

### Slice 9 - Observability

**Files:**

1. Create: `code_index/openclaw_hostd/telemetry.py`
2. Create: `code_index/openclaw_controller/telemetry.py`
3. Create: `docs/openclaw/observability.md`
4. Create: `tests/openclaw_hostd/test_telemetry.py`

Tasks:

- [ ] Add `trace_id` propagation across task, lease, run, event, and memory
      sync payloads.
- [ ] Add OpenTelemetry spans for assignment, local dispatch, provider run,
      verification, and memory sync.
- [ ] Add local rotating logs when the collector is down.
- [ ] Document optional Langfuse/Phoenix integration as later observability,
      not a dispatch dependency.

Verification:

1. One task can be followed by `trace_id` across controller, host daemon,
   local run, and memory sync.
2. Collector outage does not fail task execution.
3. Logs do not contain provider secrets.

### Slice 10 - Windows Installation And Operations

**Files:**

1. Create: `scripts/install-openclaw-hostd.ps1`
2. Create: `scripts/uninstall-openclaw-hostd.ps1`
3. Create: `docs/openclaw/windows-host-setup.md`
4. Create: `docs/openclaw/operations-runbook.md`

Tasks:

- [ ] Add PowerShell installer for the Windows Service.
- [ ] Add firewall guidance for outbound NATS/HTTPS and inbound OpenSSH.
- [ ] Add Credential Manager or DPAPI secret installation steps.
- [ ] Add health check commands.
- [ ] Add restart, upgrade, and rollback steps.
- [ ] Add break-glass SSH runbook.

Verification:

1. Service starts after reboot.
2. Host appears in controller inventory.
3. SSH access works for admin users over private networking.
4. Removing the service does not delete repo worktrees or local graph data.

## Runtime Policies

### Run Status

1. Local graph-server owns terminal local `AgentRun` status.
2. Fleet controller owns fleet `Run Health`.
3. Heartbeat absence may produce `unknown` or `stale`.
4. Heartbeat absence must not produce `completed`, `failed`, or `cancelled`.
5. Force-cancel of a live process requires explicit human confirmation.

### Health Timing

Initial timing:

1. Heartbeat interval: 10 seconds.
2. Unknown threshold: 30 seconds.
3. Stale threshold: 120 seconds.
4. Dead host classification requires operator policy and must not mutate local
   terminal run status by itself.

### SSH

1. Use Windows OpenSSH for Windows hosts.
2. Use Tailscale/private networking to avoid public inbound SSH exposure.
3. Treat SSH as bootstrap/admin/break-glass.
4. Do not use SSH as the primary task/event transport.

### MCP

1. Keep local `code_index` MCP tools as stdio tools on each host.
2. Expose central memory/fleet tools through authenticated HTTP only when
   needed.
3. Do not expose local filesystem MCP tools broadly over the network.
4. Use least-privilege tool registration per provider.

## Risks And Mitigations

1. Railway-hosted JetStream loses or replays data unexpectedly.
   - Mitigation: prefer managed NATS or persistent VM; if Railway is used,
     run restart and deploy persistence tests before dispatch.

2. NATS credential compromise allows task poisoning.
   - Mitigation: per-host NKey/account credentials and strict subject ACLs in
     Slice 0.

3. Network partition creates stale central state.
   - Mitigation: local graph-server remains authoritative; fleet marks health
     unknown/stale only.

4. Cursor SDK changes after public beta.
   - Mitigation: pin `@cursor/sdk`, wrap it behind a sidecar adapter, and use
     recorded event tests.

5. Cursor local runtime requires GUI/user session.
   - Mitigation: detect capability at host startup and use CLI/cloud/self-
     hosted fallback where available.

6. Distributed file locks add complexity too early.
   - Mitigation: keep file claims local; coordinate repo/task-level work first.

7. `fumemory` backpressure blocks agent execution.
   - Mitigation: local memory sync queue with retry and bounded size.

8. Secrets leak into logs or events.
   - Mitigation: redaction tests and no raw env dumps in heartbeat, logs, or
     audit payloads.

9. Provider CLIs differ in output and cancellation behavior.
   - Mitigation: adapter normalization tests and process-tree cancellation
     tests per provider.

10. Windows path and shell behavior differs across PowerShell, WSL, and Git
    Bash.
    - Mitigation: Windows-native service first; WSL support only through an
      explicit adapter later.

## Success Criteria

1. A Windows PC can enroll as an OpenClaw host with a stable `host_id`.
2. The host daemon starts on boot as a Windows Service.
3. The controller can see host heartbeat, repo roots, and provider
   capabilities.
4. The controller can assign a task to an eligible host over NATS.
5. The host daemon forwards the task into local graph-server and starts a local
   provider run.
6. Run events stream back to the fleet controller with replayable offsets.
7. A network outage does not corrupt local run status.
8. A `fumemory` outage does not block local run completion.
9. Task/repo-level leases prevent duplicate cross-host assignment.
10. Local file claims remain enforced by the existing graph-server mechanisms.
11. Claude, Kimi, and Codex adapters continue to work.
12. Cursor SDK can be tested as an optional provider without becoming a fleet
    dependency.
13. The same `trace_id` follows a task from controller to host daemon to local
    run to memory sync.

## First Milestone Definition

The first milestone should stop before Cursor SDK work.

Milestone 1 scope:

1. Slice 0: broker, identity, security docs and config.
2. Slice 1: host daemon skeleton.
3. Slice 2: local graph-server adapter.
4. Slice 3: task inbox and event outbox.
5. Minimal controller task assignment.

Milestone 1 demo:

1. Start NATS with persistence.
2. Start the controller.
3. Start one Windows host daemon.
4. Submit one task to the controller.
5. Host receives task, dispatches local adapter, publishes events, and reports
   final local status.
6. Stop the network connection during a run and verify local status remains
   authoritative.

## Recommended Next Command Sequence

Before implementation:

```powershell
python -m code_index doctor --json
python -m code_index agent-adapter --list-providers --json
python -m pytest tests -q
```

For the first implementation task, start with Slice 1 tests:

```powershell
python -m pytest tests/openclaw_hostd -q
```

Expected initial result before implementation:

```text
ERROR: file or directory not found: tests/openclaw_hostd
```

That failure is acceptable before Slice 1 creates the first tests.
