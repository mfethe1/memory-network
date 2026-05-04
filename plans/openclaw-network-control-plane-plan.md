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
fleet coordination, add an OpenClaw Messaging Service as the canonical
room/message/delivery layer, add thin external messaging adapters for Telegram,
Slack, Discord, Matrix, email, and webhook surfaces, use NATS JetStream/KV for
task/event/message/context transport and host/repo/task leases, and sync
long-term summaries plus context hot-load pointers into `fumemory`.

**Tech Stack:** Python `code_index`, Windows Service host daemon, Windows
OpenSSH, Tailscale/private networking, NATS JetStream/KV, Model Context
Protocol, OpenTelemetry, OpenClaw web UI, OpenClaw Context Manager, Telegram,
Slack, Discord, Matrix/email/webhook messaging adapters, Claude/Kimi/Codex/
OpenCode/Goose provider adapters, Cursor TypeScript SDK via a Node sidecar,
and `fumemory` on Railway.

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
7. Tavily searches and Firecrawl scrapes for Slack Socket Mode, Discord
   interactions, Telegram Bot API webhook behavior, Matrix application
   services, NATS JetStream consumers, CloudEvents, MCP transports, and
   long-context/context-memory systems.
8. Claude CLI and Kimi CLI review of multi-service messaging and context
   management changes.
9. Three Codex subagents:
   - Local repo context-management and schema fact finder.
   - External research brief on context rot, context engineering, MemGPT/Letta,
     LangGraph memory, and restart/handoff alternatives to compaction.
   - Architecture critique for `fumemory` SQL tables, context health,
     hot-load manifests, and session restart policy.

Important local facts:

1. The repo already has provider adapter presets for `claude`, `codex`,
   `kimi`, `opencode`, and `custom`.
2. `claude --version` reported Claude Code `2.1.123`.
3. `kimi --version` reported Kimi CLI `1.41.0`.
4. `code_index doctor --json` reported a healthy index.
5. Direct standalone `agent-adapter --provider ...` execution requires a graph
   callback URL, so external CLI review was run directly through `claude -p`
   and `kimi --print`.
6. The repo already has unified chat context, same-run follow-up messages,
   Agent Swarm parent/child metadata, local file claims, and graph-server
   provider registry plumbing that should be reused for OpenClaw messaging.
7. The repo already builds bounded `context_packet` payloads, layered
   `graph_context`, compact collaboration packets, run transcripts, and
   per-run/global JSONL event feeds; the OpenClaw Context Manager should wrap
   those contracts instead of inventing a second context format.
8. `fumemory` must stay a semantic memory and context pointer system, not the
   process-liveness source of truth and not a raw transcript warehouse.

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
22. Slack Socket Mode:
   https://docs.slack.dev/apis/events-api/using-socket-mode
23. Discord interactions:
   https://docs.discord.com/developers/interactions/receiving-and-responding
24. Telegram Bot API:
   https://core.telegram.org/bots/api
25. Matrix Application Service API:
   https://spec.matrix.org/v1.10/application-service-api/
26. CloudEvents specification:
   https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md
27. Anthropic effective context engineering:
   https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
28. Anthropic Claude Code session management:
   https://claude.com/blog/using-claude-code-session-management-and-1m-context
29. Anthropic contextual retrieval:
   https://www.anthropic.com/engineering/contextual-retrieval
30. MemGPT paper:
   https://arxiv.org/abs/2310.08560
31. Letta memory docs:
   https://docs.letta.com/guides/agents/memory
32. LangGraph memory concepts:
   https://docs.langchain.com/oss/python/concepts/memory
33. Lost in the Middle:
   https://arxiv.org/abs/2307.03172
34. RULER long-context benchmark:
   https://arxiv.org/abs/2404.06654
35. Chroma Context Rot:
   https://www.trychroma.com/research/context-rot
36. Context as a Tool:
   https://arxiv.org/abs/2512.22087

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

OpenClaw messaging review additions accepted:

1. Do not send separate Telegram messages directly to each OpenClaw host or
   Agent Run.
2. Add an OpenClaw Messaging Service as the single room/message/notification
   layer for humans, UI clients, Telegram, scripts, and future API clients.
3. Treat Telegram as an inbound/outbound adapter for high-signal notifications
   and operator replies, not as the transport or source of truth.
4. Route human messages through durable task/run/swarm rooms, then fan out
   signed commands or delivery records to eligible hosts and Agent Runs.
5. Preserve the existing local communication layers: local graph-server owns
   Agent Run transcripts and claims, host daemon bridges local state to fleet
   transport, Fleet Controller owns assignments and leases, and `fumemory`
   stores explicit checkpoints instead of raw chat.

Multi-service messaging additions accepted:

1. Keep one canonical OpenClaw Messaging Service store for rooms, messages,
   delivery records, command refs, identity links, and route policy.
2. Add an adapter registry and a common adapter contract for Telegram, Slack,
   Discord, Matrix, email, webhook, CLI, and future API clients.
3. Treat each external service adapter as a thin parser/renderer/delivery
   worker with no authority to create local runs, mutate leases, or publish
   fleet commands directly.
4. Normalize every external event into the same message envelope and derive
   idempotency from platform event IDs, room IDs, and adapter IDs.
5. Fan-out through delivery records and NATS subjects; never bridge platforms
   directly adapter-to-adapter.
6. External text that looks like a command remains a chat message until the
   Messaging Service maps the sender to a verified OpenClaw identity, validates
   command policy, and creates a signed command reference.

Context-manager and `fumemory` additions accepted:

1. Do not automatically load long "soul", project memory, or contextual files
   into every OpenClaw run.
2. Store memory pointers, source metadata, summaries, decisions, failures,
   context health, and handoff packets in `fumemory`; hot-load source content
   only when the current task proves relevance.
3. Add an OpenClaw Context Manager as a first-class control-plane service plus
   a lightweight host-local context probe in the host daemon.
4. Treat compaction as degraded fallback. The normal continuation path is a
   deliberate checkpoint and fresh-session handoff with signed context
   manifests, source handles, file hashes, event offsets, and explicit omissions.
5. Start context health warnings before the hard limit, plan handoff around
   75k tokens, and prefer a new provider session around 80k tokens, with
   provider-specific thresholds and cooldowns.
6. Reuse existing `code_index` context packets, layered graph context,
   collaboration packets, run transcripts, same-run follow-up metadata, and
   Agent Swarm parent/child metadata instead of duplicating live context state.

## Non-Goals For The First Version

1. Do not replace the local graph-server.
2. Do not make `fumemory` the source of truth for process liveness.
3. Do not make Railway private networking the assumed path to Windows PCs.
4. Do not expose every PC's local MCP tools directly over the network.
5. Do not implement cross-host file-level locks in the first release.
6. Do not depend on Cursor SDK as the only provider path.
7. Do not add Temporal before the basic task/event/lease model is stable.
8. Do not make Telegram the primary task/event transport.
9. Do not duplicate user-facing conversation state independently inside every
   host daemon.
10. Do not let Slack, Discord, Matrix, email, webhooks, or Telegram become
    parallel command paths around the OpenClaw Messaging Service and Fleet
    Controller.
11. Do not auto-load long "soul" files, global memory dumps, raw transcripts,
    or stale project docs into every agent prompt.
12. Do not use compaction as the primary long-running-agent continuation
    strategy.
13. Do not let agents mutate `fumemory` context leases, handoff packets, or
    route policy directly.

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
11. Run a lightweight context probe that reports provider token estimates,
    loaded context handles, file hashes, active claims, tool-output volume,
    duplicate context hints, and provider-visible compaction events.
12. Enforce Context Manager decisions locally by blocking automatic long-file
    loads, applying signed context manifests, and starting fresh provider runs
    only after Fleet Controller authorization.

The daemon is not responsible for:

1. Owning local file claims.
2. Rewriting local terminal run status from fleet state.
3. Exposing unrestricted shell access.
4. Holding provider credentials in plain text.
5. Deciding global context policy or writing `fumemory` context leases directly.

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

### Module: OpenClaw Messaging Service

Runs centrally next to the Fleet Controller or as a separate service,
depending on the chosen implementation scenario.

Responsibilities:

1. Store durable rooms for fleet, repo, task, run, host, and swarm
   conversations.
2. Accept messages from OpenClaw web UI, Telegram, Slack, Discord, Matrix,
   email, CLI, scripts, webhooks, and API clients through one contract.
3. Normalize human chat, operator commands, agent replies, host alerts, and
   controller events into one append-only room timeline.
4. Create delivery records for target hosts, Agent Runs, swarm rooms, external
   platform chats, web clients, and webhook subscribers.
5. Convert approved mutating messages into signed command references for the
   Fleet Controller or host daemon inbox.
6. Project delivery, acknowledgement, and failure state back into the UI.
7. Apply notification rules so Telegram, Slack, Discord, Matrix, email, and
   webhook receivers get high-signal alerts and replies instead of raw
   heartbeats or every run event.
8. Preserve `trace_id`, `correlation_id`, `task_id`, `run_id`, `host_id`, and
   event offsets so messages can be audited and synced into memory summaries.
9. Own adapter registration, route policy, room-to-platform mappings, external
   identity links, and delivery idempotency.
10. Normalize platform-specific event IDs, thread IDs, users, attachments,
    reactions, edits, and deletes into canonical message and delivery events.
11. Fan out messages to adapters through delivery records and NATS subjects,
    not by letting adapters bridge directly to each other.
12. Validate external command promotion before creating signed command refs.
13. Record context-health and handoff notices in rooms as system events without
    changing local terminal `AgentRun` status.

The messaging service is not responsible for:

1. Deciding host eligibility or bypassing Fleet Controller leases.
2. Owning local terminal `AgentRun` status.
3. Storing raw full transcripts in `fumemory`.
4. Letting Telegram, Slack, Discord, Matrix, email, or webhook commands mutate
   execution without central validation and signing.
5. Rendering platform-native message formats itself when an external adapter
   can do that through the adapter contract.

### Module: Messaging Adapter Registry And External Adapters

Adapters are thin services or controller modules. They parse inbound platform
events, render outbound deliveries, and report delivery acknowledgements.

Initial adapters:

1. Web UI adapter.
2. Telegram adapter.
3. Slack adapter.
4. Discord adapter.
5. Matrix adapter.
6. Email adapter.
7. Generic signed webhook adapter.
8. CLI/script adapter.

Adapter responsibilities:

1. Register adapter identity, platform type, capabilities, rate limits,
   supported content features, health, and routing constraints.
2. Normalize inbound platform events to OpenClaw message envelopes with stable
   idempotency keys.
3. Strip any untrusted `command_ref`, signed payload, or execution directive
   from inbound external messages before publishing them.
4. Render outbound OpenClaw deliveries into platform-native text, threads,
   replies, attachments, reactions, edits, or emails.
5. Acknowledge delivery outcomes back to the Messaging Service.
6. Keep only platform offset cursors and retry state; do not own durable room
   history.

Adapter-specific notes:

1. Slack Socket Mode requires per-event acknowledgement by `envelope_id` and
   can run multiple active WebSocket connections, so the Slack adapter needs
   idempotent inbound handling and explicit reconnect behavior.
2. Discord interactions require a fast initial response and follow-up messages
   through interaction tokens, so Discord command surfaces must defer quickly
   and hand execution to the Messaging Service.
3. Telegram webhooks and polling are mutually exclusive for a bot, so the
   adapter must choose one ingestion mode per bot identity and record it in the
   registry.
4. Matrix application services use transaction IDs for retry idempotency and
   explicit namespace registration; map those transaction IDs to OpenClaw
   idempotency keys.
5. Generic webhooks are inbound-only for the first version and cannot promote
   commands.

### Module: OpenClaw Web UI

The OpenClaw Web UI is the primary human command surface. Telegram mirrors
selected alerts and replies, but the web UI should be the most complete view of
fleet state.

Responsibilities:

1. Show one inbox for fleet, repo, task, run, host, and swarm rooms.
2. Default task communication to one task room, with the Swarm Lead and child
   Agent Runs shown as room participants.
3. Provide a target selector for task, swarm, run, host, and fleet messages.
4. Preview delivery recipients before a user sends a message.
5. Render mutating messages as command cards with validation, signature,
   delivery, acknowledgement, and rejection state.
6. Keep a context side panel for leases, selected paths, active claims, context
   health, Run Health, Verification State, and memory checkpoints.
7. Let users filter notification noise by severity, room, repo, task, host,
   provider, and delivery state.

### Module: Fleet Controller

Runs centrally, likely next to or near `fumemory`.

Responsibilities:

1. Maintain host inventory.
2. Accept task requests and command references from the OpenClaw Messaging
   Service, browser UI, scripts, or API clients.
3. Select eligible hosts based on repo, provider capabilities, current leases,
   health, and policy.
4. Assign tasks over NATS.
5. Aggregate fleet run health from host heartbeats and run events.
6. Store fleet-level audit events.
7. Provide dashboard/API views and message-friendly fleet projections.
8. Push durable memory objects to `fumemory` or accept host-pushed summaries.
9. Accept Context Manager handoff proposals and authorize fresh provider runs
   only after leases, host eligibility, and restart cooldowns pass.
10. Surface context health and handoff state in fleet APIs without treating
    context health as terminal local run status.

The controller may mark a host or run as `unknown` or `stale`, but it must not
write terminal local `AgentRun` status unless process liveness is known false
and no active local claims remain.

### Module: OpenClaw Context Manager

Runs centrally near the Fleet Controller and `fumemory`, with a lightweight
context probe inside each host daemon.

Goal:

1. Keep agent prompts small, relevant, auditable, and restartable.
2. Replace automatic long-file prompt stuffing with signed hot-load manifests.
3. Treat context as a managed working set backed by local graph-server context
   and `fumemory` pointers.

Responsibilities:

1. Consume host context metrics for each Agent Run: estimated provider tokens,
   loaded files, loaded pointers, tool-output volume, active claims, source
   hashes, context packet IDs, provider-visible compaction signals, and recent
   failures.
2. Query local graph-server through the host daemon for bounded
   `context_packet`, layered `graph_context`, collaboration packets, run
   transcripts, active claims, blockers, process liveness, and file hashes.
3. Query `fumemory` for decisions, failed approaches, verification states,
   repo/host preferences, prior summaries, source pointers, and handoff
   packets.
4. Query the Messaging Service for task-room summaries and explicit operator
   instructions.
5. Rank context pointers by task relevance, freshness, source authority,
   sensitivity, token cost, and whether the pointer is required, useful,
   inspect-only, avoid, or expired.
6. Produce signed context manifests that contain pointer IDs, source URIs,
   locator JSON, load order, budgets, expiry, file/content hashes, relevance
   reasons, and explicit omissions.
7. Write context health events and handoff packets to `fumemory` with source
   event offsets, not raw full transcripts.
8. Warn at provider-specific soft thresholds, plan handoff near 75k tokens, and
   request a fresh session near 80k tokens or on critical rot signals.
9. Coordinate fresh-session handoff through the Fleet Controller, not directly
   through a provider adapter.
10. Maintain restart cooldowns so context pressure does not create handoff
    loops.

The Context Manager is not responsible for:

1. Owning local `AgentRun` status, transcripts, process liveness, or file
   claims.
2. Loading long "soul" or project-memory files automatically.
3. Silently compacting conversation history and replacing the transcript.
4. Letting an external messaging adapter create execution commands.
5. Writing provider secrets, raw private transcripts, or unrestricted local
   file content into `fumemory`.

### Module: NATS JetStream And KV

Use NATS as the fleet coordination bus, not as the local graph database.

Responsibilities:

1. Durable task assignment streams.
2. Durable run event streams.
3. Replayable audit stream.
4. Host capability key-value records.
5. Host/repo/task-level leases with fencing revisions.
6. Adapter registry and route-policy KV records.
7. Durable message delivery, acknowledgement, and dead-letter streams.
8. Context metrics, manifest, health, and handoff event streams.

Deployment recommendation:

1. Prefer managed NATS/NATS Cloud or a small dedicated VM with persistent disk
   for the first production-like control plane.
2. Use Railway for `fumemory` and control APIs.
3. Use Railway for NATS only after proving JetStream persistence across
   restarts, deploys, and region/network interruptions.

### Module: fumemory

Use `fumemory` as durable semantic memory and context-pointer storage. It
should point agents to the right hot-load locations; it should not auto-hydrate
long context into every run.

Responsibilities:

1. Store run summaries.
2. Store decisions.
3. Store failed approaches and remediation notes.
4. Store verification states.
5. Store repo and host preferences.
6. Store cross-run lessons.
7. Provide search/retrieval for long-term memory.
8. Store context sources and hot-load pointers with source hashes, locator JSON,
   sensitivity, expiry, and relevance metadata.
9. Store context health events and handoff packets for deliberate fresh-session
   continuation.
10. Store compact room/run/task summaries with source offsets and omissions.
11. Store "avoid" pointers for failed approaches and stale or superseded
    decisions.

`fumemory` is not responsible for:

1. Process liveness.
2. Local `AgentRun` terminal status.
3. File-claim fencing.
4. Raw full transcripts by default.
5. Provider secrets or unrestricted local file content.

Required sync properties:

1. Idempotency.
2. Backpressure.
3. Retry with bounded local queue.
4. Traceability back to `host_id`, `task_id`, `run_id`, and event offsets.
5. Rebuildability from local graph-server events and Messaging Service rooms.
6. Sensitivity filtering before cross-host or cross-provider retrieval.

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

### Message Room

Rooms are the user-facing conversation model. The room controls visibility,
notification policy, and default delivery targets; it does not override host
leases or local file claims.

```text
openclaw_rooms:
  room_id
  room_kind              # fleet | repo | task | run | host | swarm
  display_name
  repo_id
  task_id
  run_id
  host_id
  parent_room_id
  notification_policy
  created_at
  archived_at
  metadata_json
```

### Message Envelope

Every inbound human message, agent reply, host alert, command request, and
delivery update uses the same envelope.

```text
openclaw_messages:
  message_id
  room_id
  sender_kind            # human | agent | controller | host | system
  sender_id
  target_scope_json
  message_type           # chat | command | event | summary | alert
  body
  context_handles_json
  adapter_id
  platform_ref_json       # external room/thread/message/user ids when present
  trace_id
  correlation_id
  parent_message_id
  idempotency_key
  created_at
  metadata_json
```

### Message Delivery

Delivery records separate "one user message" from the many hosts, Agent Runs,
web clients, and notification adapters that may need to receive or acknowledge
it.

```text
openclaw_message_deliveries:
  delivery_id
  message_id
  recipient_kind         # host | run | agent | adapter | web | controller
  recipient_id
  delivery_status        # queued | delivered | acked | failed | expired
  nats_sequence
  delivered_at
  acked_at
  error
  metadata_json
```

### Messaging Adapter

External messaging services register capabilities and policy in one registry.
The adapter record is not room history; it is routing and operational state.

```text
openclaw_messaging_adapters:
  adapter_id
  adapter_type            # web | telegram | slack | discord | matrix | email | webhook | cli
  display_name
  status                  # active | paused | degraded | disabled
  capabilities_json       # threads, edits, reactions, attachments, rich_text
  rate_limits_json
  auth_key_id
  last_seen_at
  created_at
  updated_at
  metadata_json
```

### Platform Room Mapping

Rooms can be mirrored into multiple messaging services. The mapping is explicit
so a new external channel cannot silently create a command surface.

```text
openclaw_platform_room_mappings:
  mapping_id
  adapter_id
  platform_room_id
  platform_thread_id
  room_id
  sync_mode               # bidirectional | inbound_only | outbound_only | notify_only
  route_policy_json
  created_at
  archived_at
  metadata_json
```

### External Identity Link

External users must be linked to OpenClaw identities before a chat message can
be promoted into a command.

```text
openclaw_external_identities:
  identity_link_id
  adapter_id
  platform_user_id
  openclaw_identity_id
  display_name
  scopes_json             # message:write, command:propose, command:write
  verified_at
  revoked_at
  metadata_json
```

### Signed Command Reference

Mutating messages become command references after validation. This keeps chat
auditable while preserving the Fleet Controller as the authority for
assignment, leases, cancellation, and force operations.

```text
openclaw_command_refs:
  command_id
  message_id
  command_type           # assign_task | run_message | cancel | retry | checkpoint
  target_host_id
  task_id
  run_id
  lease_id
  signed_payload
  signature_key_id
  expires_at
  status
  created_at
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

### Context Source

Context sources live in `fumemory`. They describe where relevant memory can be
hot-loaded from, not what must be injected into every prompt.

```text
context_sources:
  source_id
  source_kind             # repo_file | graph_symbol | graph_chunk | room | run_summary | decision | doc | external
  repo_id
  host_id
  uri                     # file path, codeindex://..., room://..., fumemory://...
  title
  content_hash
  version_ref             # git sha, graph index version, message offset
  sensitivity             # public | normal | private | secret
  created_at
  updated_at
  metadata_json
```

### Context Pointer

```text
context_pointers:
  pointer_id
  source_id
  pointer_kind            # required | hot_load | cite | inspect | avoid
  locator_json            # line ranges, symbol_uid, room offsets, summary section
  summary
  token_estimate
  freshness_at
  expires_at
  metadata_json
```

### Context Relevance Score

```text
context_relevance_scores:
  score_id
  pointer_id
  task_id
  run_id
  agent_id
  query_hash
  score
  reason
  model
  computed_at
```

### Agent Context Lease

```text
agent_context_leases:
  lease_id
  agent_id
  run_id
  task_id
  provider
  budget_tokens
  soft_limit_tokens
  hard_limit_tokens
  estimated_used_tokens
  status                  # active | warning | handoff_pending | restarted | expired | released
  context_manifest_hash
  expires_at
  created_at
  updated_at
```

### Context Manifest

Context manifests are signed, expiring hot-load instructions. They are the
normal mechanism for starting or restarting agent sessions without prompt
bloat.

```text
context_manifest:
  manifest_id
  lease_id
  task_id
  run_id
  repo_id
  host_id
  provider
  pointer_ids
  required_pointer_ids
  load_order_json
  omitted_json
  token_budget_json
  signature_key_id
  signed_payload
  expires_at
```

### Handoff Packet

```text
handoff_packets:
  handoff_id
  from_run_id
  to_run_id
  task_id
  trigger_kind            # token_pressure | context_rot | provider_error | manual
  status                  # proposed | approved | consumed | failed | superseded
  packet_json             # goal, state, decisions, claims, verification, pointers
  packet_hash
  created_at
  consumed_at
```

### Context Health Event

```text
context_health_events:
  event_id
  run_id
  agent_id
  task_id
  event_kind              # token_pressure | stale_context | contradiction | drift | duplicate_context | missing_required
  severity                # info | warning | critical
  observed_tokens
  budget_tokens
  details_json
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
openclaw.message.inbound
openclaw.message.inbound.<adapter_type>.<adapter_id>
openclaw.room.<room_id>.events
openclaw.host.<host_id>.inbox
openclaw.host.<host_id>.messages.ack
openclaw.notification.telegram.outbound
openclaw.adapter.<adapter_type>.<adapter_id>.inbound
openclaw.adapter.<adapter_type>.<adapter_id>.outbound
openclaw.adapter.<adapter_type>.<adapter_id>.ack
openclaw.adapter.<adapter_type>.<adapter_id>.health
openclaw.context.<host_id>.<run_id>.metrics
openclaw.context.<host_id>.<run_id>.health
openclaw.context.<host_id>.<run_id>.manifest.request
openclaw.context.<host_id>.<run_id>.manifest.response
openclaw.context.<host_id>.<run_id>.handoff.proposed
openclaw.context.<host_id>.<run_id>.handoff.ack
openclaw.context.audit
openclaw.audit.<host_id>
openclaw.deadletter
```

Initial KV buckets:

```text
openclaw_hosts
openclaw_leases
openclaw_provider_caps
openclaw_controller_config
openclaw_message_routes
openclaw_messaging_adapters
openclaw_platform_room_mappings
openclaw_context_policy
openclaw_context_leases
```

Subject ACL baseline:

1. Host may publish only its own heartbeat, capabilities, run events, status,
   verification, and audit subjects.
2. Host may consume only its own task assignment subject.
3. Controller may publish task assignments.
4. Controller may consume all host events.
5. No host may consume another host's task stream.
6. No host may publish controller config.
7. Messaging Service may publish room events, host inbox messages, adapter
   outbound deliveries, and message delivery state.
8. External adapters may publish only their own inbound, acknowledgement, and
   health subjects.
9. Hosts may consume only their own inbox and publish only acknowledgements for
   messages addressed to that host.
10. No external adapter may publish `openclaw.command.*`,
    `openclaw.task.*`, `openclaw.host.*`, or context manifest subjects.
11. Context Manager may consume host context metrics and publish only context
    health, manifests, handoff proposals, and context audit records.
12. Only Fleet Controller may authorize a fresh provider run from a handoff
    proposal.

## Implementation Scenarios

These scenarios are ordered from fastest to most scalable. The recommended
path is Scenario A for the first milestone, with interfaces kept narrow enough
to split into Scenario B later.

### Scenario A - Controller-Embedded Messaging

Implement rooms, messages, delivery records, adapter registry, route policy,
Telegram adapter endpoints, and passive Context Manager endpoints in the Fleet
Controller package.

Best when:

1. The first milestone needs one deployable service.
2. The team wants fewer moving pieces while task assignment, leases, and host
   daemon behavior are still being proven.
3. Telegram and other adapters are high-signal alerts or small chat workloads,
   not a heavy multi-platform bridge.

Tradeoffs:

1. Fastest path to an end-to-end demo.
2. Simplest auth and deployment story.
3. Messaging, context, and scheduling code must keep clear module interfaces to
   avoid a future split becoming painful.

### Scenario B - Standalone Messaging Service

Run `openclaw_messaging` as a separate service with its own storage and API.
The Fleet Controller accepts signed command references from it.

Best when:

1. Multiple UI clients, Telegram, and API clients become active at the same
   time.
2. Message retention, adapter routing, notification rules, or moderation/audit
   needs start growing faster than scheduling.
3. The controller should stay focused on host eligibility, leases, assignment,
   and fleet health.

Tradeoffs:

1. Cleaner long-term locality for conversation behavior.
2. Easier to scale notification adapters independently.
3. Requires service-to-service auth, separate migrations, and more operational
   surface in the first release.

### Scenario C - Broker-First Messaging

Make NATS JetStream the primary append-only room event log and build projections
for UI/API reads.

Best when:

1. Replayability and event sourcing are more important than simple relational
   queries.
2. The system expects many hosts and high-volume run/event mirroring.
3. The team is already operating JetStream confidently with durable storage and
   backup.

Tradeoffs:

1. Strong replay story and natural fan-out.
2. More complicated read models, migrations, and user-facing history queries.
3. Riskier before broker persistence and retention behavior are proven.

### Scenario D - Local-First Messaging With Fleet Projection

Keep per-host graph-server conversations as local authority and project them
centrally for the OpenClaw UI.

Best when:

1. Hosts are often offline or disconnected for long periods.
2. Local operator workflows must work even when the central service is down.
3. Cross-host messaging can tolerate delayed convergence.

Tradeoffs:

1. Strong local resilience.
2. Harder to present one clean task room across all hosts.
3. Duplicate delivery and conflict handling become more complex than Scenario
   A or B.

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

### Slice 3 - OpenClaw Messaging Service And UI Rooms

**Files:**

1. Create: `code_index/openclaw_messaging/__init__.py`
2. Create: `code_index/openclaw_messaging/models.py`
3. Create: `code_index/openclaw_messaging/store.py`
4. Create: `code_index/openclaw_messaging/routes.py`
5. Create: `code_index/openclaw_messaging/telegram.py`
6. Create: `code_index/openclaw_messaging/adapters.py`
7. Create: `code_index/openclaw_messaging/adapter_registry.py`
8. Create: `code_index/openclaw_messaging/notifications.py`
9. Create: `docs/openclaw/messaging-service.md`
10. Create: `docs/openclaw/messaging-adapters.md`
11. Create: `docs/openclaw/openclaw-ui-command-center.md`
12. Create: `tests/openclaw_messaging/test_rooms.py`
13. Create: `tests/openclaw_messaging/test_message_delivery.py`
14. Create: `tests/openclaw_messaging/test_adapter_registry.py`
15. Create: `tests/openclaw_messaging/test_telegram_adapter.py`
16. Modify: `code_index/openclaw_controller/app.py`
17. Modify: `pyproject.toml`

Tasks:

- [ ] Implement `openclaw_rooms`, `openclaw_messages`,
      `openclaw_message_deliveries`, `openclaw_command_refs`,
      `openclaw_messaging_adapters`, `openclaw_platform_room_mappings`, and
      `openclaw_external_identities` storage.
- [ ] Define the base adapter contract for normalize inbound, render outbound,
      acknowledge delivery, report health, and expose capabilities.
- [ ] Add room kinds for fleet, repo, task, run, host, and swarm.
- [ ] Add APIs for `GET /rooms`, `GET /rooms/{room_id}/messages`,
      `POST /messages`, `POST /messages/{message_id}/ack`, and
      `GET /messages/stream`.
- [ ] Add a task room projection that shows Swarm Lead and child Agent Runs as
      participants.
- [ ] Add target preview: task, swarm, run, host, or fleet, including expected
      delivery recipients before a user sends.
- [ ] Define the OpenClaw Web UI command center layout: inbox, room timeline,
      target selector, command cards, delivery state, and context side panel.
- [ ] Convert mutating messages into signed command references instead of
      directly publishing host tasks from Telegram, Slack, Discord, Matrix,
      email, webhook, or UI code.
- [ ] Add Telegram inbound webhook handling and outbound notification delivery.
- [ ] Add stub adapter registrations for Slack, Discord, Matrix, email, and
      generic webhook with no command-promotion permissions by default.
- [ ] Add idempotency keys based on adapter ID, platform room/thread ID, and
      platform event/message ID.
- [ ] Add notification rules for `needs_attention`, `blocked`, `failed`,
      `completed`, `lease_conflict`, and `verification_blocked`.
- [ ] Add duplicate delivery tests and idempotency tests for repeated Telegram
      webhook updates and replayed adapter events.

Verification:

1. One human message can be stored once and delivered to multiple host/run
   recipients with separate acknowledgement records.
2. A Telegram reply creates the same message envelope as a web UI message.
3. A Telegram update replay does not create duplicate commands or deliveries.
4. Mutating messages require a signed command reference before host delivery.
5. A task room can show all Agent Swarm child runs without sending separate
   Telegram messages to each one.
6. A Slack or Discord-looking inbound payload cannot create a command until the
   sender is linked to an OpenClaw identity and policy allows promotion.
7. A generic webhook can create an inbound message but cannot create a command.

### Slice 4 - NATS Event Outbox And Task Inbox

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
- [ ] Add host inbox message validation for signed command references and
      non-mutating room deliveries.
- [ ] Publish message delivery ACKs back to the Messaging Service.
- [ ] Add replay-safe event sequence numbers.
- [ ] Add tests for idempotent handling of duplicate task messages.

Verification:

1. Duplicate task assignment with the same `task_id` does not create two local
   runs.
2. Outbox keeps events when publish fails.
3. Outbox drains after reconnect.
4. Duplicate message delivery with the same `message_id` and `delivery_id` is
   acknowledged once.

### Slice 5 - Fleet Leases And Fencing

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

### Slice 6 - Fleet Controller API

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
- [ ] Accept signed command references from the Messaging Service.
- [ ] Add host eligibility filtering by repo root, provider capability, health,
      and lease availability.
- [ ] Add NATS task publish.
- [ ] Return assignment and rejection results in a shape the Messaging Service
      can attach to the originating room message.
- [ ] Add run health aggregation from heartbeats and events.
- [ ] Accept Context Manager handoff proposals and authorize fresh provider
      runs only after host eligibility, leases, and restart cooldown checks.
- [ ] Expose context health and handoff state in fleet API projections.
- [ ] Add API tests for host selection and rejected assignments.

Verification:

1. Controller assigns a task only to an eligible host.
2. Controller refuses assignment when the repo lease is held elsewhere.
3. Controller marks host health `unknown` or `stale` without mutating local
   terminal run status.
4. Controller rejects unsigned or expired command references.
5. Controller rejects a context handoff restart when the repo/task lease is not
   valid or the restart cooldown is active.

### Slice 7A - Context Manager And fumemory Pointer Store

**Files:**

1. Create: `code_index/openclaw_context/__init__.py`
2. Create: `code_index/openclaw_context/models.py`
3. Create: `code_index/openclaw_context/store.py`
4. Create: `code_index/openclaw_context/policy.py`
5. Create: `code_index/openclaw_context/manifest.py`
6. Create: `code_index/openclaw_context/health.py`
7. Create: `code_index/openclaw_context/handoff.py`
8. Create: `code_index/openclaw_hostd/context_probe.py`
9. Create: `docs/openclaw/context-manager.md`
10. Create: `docs/openclaw/context-hot-load-manifest.md`
11. Create: `tests/openclaw_context/test_pointer_store.py`
12. Create: `tests/openclaw_context/test_context_health.py`
13. Create: `tests/openclaw_context/test_manifest.py`
14. Create: `tests/openclaw_context/test_handoff.py`
15. Modify: `code_index/openclaw_hostd/service.py`
16. Modify: `pyproject.toml`

Tasks:

- [ ] Add `context_sources`, `context_pointers`,
      `context_relevance_scores`, `agent_context_leases`,
      `handoff_packets`, and `context_health_events` storage in `fumemory` or
      a `fumemory`-compatible schema migration.
- [ ] Add context pointer dedupe by source URI, content hash, and locator JSON.
- [ ] Add sensitivity filters for cross-host, cross-provider, and external
      messaging retrieval.
- [ ] Add host context metrics collection for estimated tokens, loaded files,
      loaded pointer IDs, file hashes, active claims, recent failures, tool
      output volume, and provider-visible compaction signals.
- [ ] Generate signed context manifests containing pointer IDs, load order,
      required pointers, omissions, token budget, expiry, and source hashes.
- [ ] Reuse existing local `context_packet`, layered `graph_context`,
      collaboration packet, transcript, run metadata, and claim data as context
      sources.
- [ ] Block automatic loading of long "soul", global memory, raw transcript,
      or stale project context files unless a manifest explicitly selects a
      section or pointer.
- [ ] Add context-health heuristics for token pressure, stale context,
      duplicate context, contradiction, drift, missing required instructions,
      source hash mismatch, repeated failed approach, and pending edit under
      pressure.
- [ ] Warn around 65k to 70k tokens, prepare handoff around 75k, and propose a
      fresh session around 80k or critical context health.
- [ ] Add handoff packet generation with current goal, latest state,
      accepted/rejected decisions, active claims, verification state, unresolved
      questions, required pointers, omitted context, and source offsets.
- [ ] Treat compaction as degraded fallback and record a critical
      `context_health_event` when provider compaction happens without a
      Context Manager handoff.

Verification:

1. Context manifest generation fits the configured budget and cannot drop
   required pointers.
2. A long "soul" file is not auto-loaded; only a pointer or selected section is
   returned.
3. A fake run at 70k tokens produces a warning health event.
4. A fake run at 80k tokens produces one idempotent handoff proposal.
5. A source hash mismatch creates a stale-context health event.
6. Duplicate replay of a manifest request returns the same manifest or a safe
   idempotent replacement.
7. `fumemory` outage does not block local run completion; host daemon degrades
   to local context packets and reports degraded context health.

### Slice 7 - fumemory Sync

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
- [ ] Sync compact context sources, pointers, decisions, failed approaches,
      verification states, and handoff packets without syncing raw full
      transcripts.
- [ ] Mark stale or superseded memory as `avoid` or expired pointers instead
      of deleting provenance.
- [ ] Queue failed syncs locally.
- [ ] Add backpressure limits.
- [ ] Add tests for duplicate sync payloads and retry behavior.

Verification:

1. `fumemory` outage does not block local run completion.
2. Duplicate sync payload is safe.
3. Memory payload can be traced back to the original run.
4. Raw transcript text is not written to `fumemory` by default.

### Slice 8 - Cursor SDK Provider Adapter

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

### Slice 9 - Open Source Provider Adapters

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

### Slice 10 - Observability

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

### Slice 11 - Windows Installation And Operations

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

### Messaging And Notifications

1. OpenClaw Messaging Service owns central room/message/delivery state.
2. Telegram, Slack, Discord, Matrix, email, and webhooks are notification,
   reply, or chat adapters, not host transports.
3. User-visible task communication should default to one task room, with run
   and host threads available for focused follow-up.
4. A message can create multiple delivery records, but it must remain one
   durable user message.
5. Mutating messages require validation and a signed command reference before
   host delivery.
6. Notification rules should suppress routine heartbeats and raw event spam.
7. Delivery and ACK failures should appear in the room timeline without
   changing local terminal `AgentRun` status.
8. External adapters cannot publish fleet tasks, host commands, or context
   manifests directly.
9. Platform-specific retries must be idempotent against adapter ID, platform
   event ID, and platform room/thread ID.
10. Generic webhooks are inbound-only until an explicit signed webhook command
    policy exists.

### Context Management

1. Always-loaded context must be a small kernel: safety rules, current task
   contract, active constraints, and pointers to retrieval systems.
2. Long "soul", global memory, raw transcript, and broad domain docs are
   source material for hot-loading, not default prompt material.
3. Context manifests must be signed, expiring, scoped to `host_id`, `repo_id`,
   `task_id`, `run_id`, and provider, and include source hashes or offsets.
4. Required pointers outrank optional context, but required pointers should be
   short handles whenever possible.
5. Warning threshold: 65k to 70k provider-estimated tokens.
6. Handoff planning threshold: around 75k provider-estimated tokens.
7. Fresh-session proposal threshold: around 80k provider-estimated tokens or
   earlier for critical context rot.
8. Compaction is degraded mode. If a provider compacts without a Context
   Manager handoff, record a critical context health event and create a
   reviewable checkpoint.
9. Fresh-session handoff should inject only task goal, current state,
   decisions, constraints, active claims, verification state, unresolved
   questions, explicit omissions, and hot-load pointers.
10. Do not restart repeatedly: enforce cooldowns and require materially changed
    health evidence before proposing another handoff.

Context bloat signals:

1. Stale room chatter or old transcript is more than 30% of loaded context.
2. Same source appears multiple times with different wording.
3. Loaded context has no matching read/edit/test/tool use after several turns.
4. Required pointers cannot fit inside the manifest budget.
5. Large pasted docs displace selected files, claims, or verification output.

Context rot signals:

1. Loaded source hashes differ from current local graph-server file hashes.
2. Run references claims, tests, symbols, or paths that no longer exist.
3. Agent cites a decision superseded by a newer source offset or pointer.
4. A failed approach repeats after an `avoid` pointer exists.
5. Conversation goal drifts from the Agent Task acceptance criteria.
6. Required project/system instructions or active fencing context is missing.

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

11. Telegram becomes a noisy parallel control path.
    - Mitigation: route Telegram through the Messaging Service, require the
      same message envelope and command signing as the web UI, and notify only
      high-signal events by default.

12. Message fan-out creates duplicate work.
    - Mitigation: store one durable message with per-recipient delivery
      records, require idempotency keys, and keep Fleet Controller leases as
      the gate before task assignment.

13. Room history drifts from local run transcripts.
    - Mitigation: store message references and summarized events centrally,
      keep local graph-server as transcript authority, and sync only explicit
      checkpoints to `fumemory`.

14. Additional messaging adapters create unsafe command paths.
    - Mitigation: one adapter contract, per-adapter NATS credentials, external
      identity links, command-promotion policy, and signed command refs only
      from the Messaging Service.

15. Adapter retry behavior creates duplicate messages.
    - Mitigation: idempotency keys from adapter ID, platform room/thread ID,
      and platform message/event ID; delivery records remain one-to-many.

16. Context Manager becomes hidden prompt stuffing.
    - Mitigation: manifests are pointer-first, signed, auditable, expiring, and
      include explicit load reasons and omissions.

17. Context health false positives create restart loops.
    - Mitigation: alert-only rollout first, cooldowns, required materially
      changed health evidence, and human override for repeated handoffs.

18. Summary drift or compaction loses critical nuance.
    - Mitigation: compaction is degraded mode; handoff packets include source
      offsets, hashes, decisions, rejected approaches, and "must verify" flags.

19. Cross-host context leakage exposes private paths or transcripts.
    - Mitigation: `context_sources.sensitivity`, route scopes, host/repo
      filters, and no raw full transcript sync by default.

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
14. One task room can coordinate a Swarm Lead and child Agent Runs without
    separate Telegram messages to each OpenClaw instance.
15. Telegram replies and web UI messages create the same message envelope and
    delivery records.
16. A user can see whether a message is queued, delivered, acknowledged,
    failed, or expired per host/run recipient.
17. Slack, Discord, Matrix, email, and webhook adapters can register
    capabilities and route messages through the same room/delivery contract
    without direct host command access.
18. External command-like messages cannot mutate execution until sender
    identity, room policy, and signed command-reference validation pass.
19. A host reports context metrics for an Agent Run without exposing secrets or
    raw transcript dumps.
20. A Context Manager manifest can hot-load relevant pointers without
    auto-loading long "soul" or global memory files.
21. A run near 80k provider-estimated tokens can create an idempotent handoff
    packet and start a fresh provider session through Fleet Controller policy.
22. Context health events and handoff packets are traceable to source hashes,
    event offsets, `task_id`, `run_id`, and `host_id`.
23. System can enable multiple instances of openclaw to communicate and allow for a single message service to relay messages to each openclaw instance.
24. Messages should be marked as recieved by each of the systems in fumemory, and upon recieving a gateway can claim and write a task to a shared task management system. If another system deems the task partial or does not meet the users needs, they can update this and claim follow up action.
25. The system should allow for critical review of context rot and prevent this from happening.
26. There is always a context management and control system in place and this can traverse levels of agents and subagents. This should be a heuristic system managed by an agent and visible in the control system.
27. The memory-network system should be used to improve context level at the system level, so each instance should be able to run this.
28. There should be a ssh capability or other cross network, capability to maintain ssh connectivity to the other PCs in the openclaw network that can enhance and has a similar but higher level system of the memory-network, but it would be more of an agent-memory-network.
29. Agent memory network can be used to monitor all openclaw instances, agents and subagents running in those systems by ssh or other connection methods.
30. This system is to coordinate multiple instances of openclaw, claude code, kimi cli, opencode and other cli agentic frameworks across a network and allow for easy monitoring of the running systems and is able to recover those systems.
31. This system should improve the current cross system communication and allow for more powerful integration of multiple systems.

## First Milestone Definition

The first milestone should stop before Cursor SDK work.

Milestone 1 scope:

1. Slice 0: broker, identity, security docs and config.
2. Slice 1: host daemon skeleton.
3. Slice 2: local graph-server adapter.
4. Slice 3: embedded Messaging Service with rooms, message storage, delivery
   records, adapter registry, platform room mappings, external identities, and
   Telegram adapter stubs.
5. Slice 4: task inbox, host inbox, message ACKs, and event outbox.
6. Slice 5: minimal host/repo/task leases and fencing before adding a second
   host.
7. Slice 6: minimal controller task assignment from signed command references.
8. Slice 7A passive mode: Context Manager pointer schema, host context metrics,
   signed manifest stubs, and alert-only health events. No automatic restart
   until metrics and false positives are understood.

Milestone 1 demo:

1. Start NATS with persistence.
2. Start the controller.
3. Use Scenario A embedded Messaging Service mode in the controller.
4. Start one Windows host daemon.
5. Submit one task room message from the OpenClaw UI or API.
6. Messaging Service stores the message once, creates delivery records, and
   asks the controller to sign and assign the task.
7. Host receives task, dispatches local adapter, publishes events, and reports
   final local status.
8. Telegram receives only the high-signal task status notification.
9. Host context probe reports estimated tokens, loaded pointers, active claims,
   and source hashes to the Context Manager.
10. Context Manager returns a signed pointer-first manifest and does not
    auto-load long "soul" or global memory files.
11. Stop the network connection during a run and verify local status remains
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

For the first messaging and context-manager slices, the expected initial
failures are also missing test directories:

```powershell
python -m pytest tests/openclaw_messaging -q
python -m pytest tests/openclaw_context -q
```
