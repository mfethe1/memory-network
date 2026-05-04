# OpenClaw M1 Slice: Unified Telegram Controller

## Goal

Unify Telegram chat and control for the Lenny and Rosie OpenClaw hosts through
one canonical Messaging Service plus one Fleet Controller path.

Telegram must be a thin adapter into the central room/message/delivery store.
Neither Lenny nor Rosie should run a separate Telegram command parser, bot
control path, or direct host-to-host Telegram integration. All mutating work
must flow through stored messages, signed command references, Fleet Controller
eligibility checks, central leases, NATS delivery, and persistent ACK state.

## Branch

Suggested Sandcastle branch:

```bash
agent/openclaw-m1-unified-telegram-controller
```

## Scope

Owned paths:

- `code_index/openclaw_messaging/models.py`
- `code_index/openclaw_messaging/store.py`
- `code_index/openclaw_messaging/telegram.py`
- `code_index/openclaw_messaging/routes.py`
- `code_index/openclaw_messaging/adapter_registry.py` only for registration or
  capability wiring
- `code_index/openclaw_controller/app.py`
- `code_index/openclaw_controller/scheduler.py`
- `code_index/openclaw_controller/models.py` only for narrow assignment or
  rejection fields
- `code_index/openclaw_hostd/service.py` only for NATS consumer/ACK integration
  needed by this slice
- `code_index/openclaw_hostd/inbox.py` only for host message delivery or task
  ACK persistence needed by this slice
- `code_index/openclaw_hostd/nats_client.py` only for reusable publish/subscribe
  helpers needed by this slice
- `tests/openclaw_messaging/test_telegram_adapter.py`
- `tests/openclaw_messaging/test_message_delivery.py`
- `tests/openclaw_controller/test_scheduler.py`
- `tests/openclaw_controller/test_api.py`
- `tests/openclaw_hostd/test_inbox.py`
- `tests/openclaw_hostd/test_service.py`
- `tests/openclaw_hostd/test_outbox.py` only if outbox replay behavior changes
- `docs/openclaw/messaging-service.md`
- `docs/openclaw/messaging-adapters.md`
- `docs/openclaw/nats-subject-acls.md`
- `plans/openclaw-network-control-plane-plan.md`

Out-of-scope paths:

- `.sandcastle/**`
- `plugins/**`
- `code_index/agent_adapters/**`
- `code_index/agent_providers.py`
- `code_index/openclaw_context/**`
- `code_index/openclaw_controller/fleet_mcp.py`
- `code_index/openclaw_controller/ssh_recovery.py`
- `code_index/commands/mcp_*`
- Provider adapter, Cursor sidecar, live CMA, Fleet MCP, SSH recovery, and
  `fumemory` implementation files

Do not edit Sandcastle runtime/config files. Keep changes confined to the
central messaging/controller/hostd surfaces needed for this integration.

## Existing Primitives To Reuse

1. `MessagingStore` is the canonical persistent SQLite store for
   `openclaw_rooms`, `openclaw_messages`, `openclaw_message_deliveries`,
   `openclaw_command_refs`, adapters, platform room mappings, and external
   identities.
2. `openclaw_messages.idempotency_key` already deduplicates adapter events via
   `adapter_id`, platform room/thread, and platform event/message ID.
3. `openclaw_message_deliveries` already stores per-recipient delivery state
   and monotonic ACKs through `ack_delivery`.
4. `openclaw_command_refs` already provides a single-use command claim:
   `pending -> active -> assigned/rejected/cancelled`.
5. `MessagingStore.promote_message_to_assign_task_command_ref` already models
   untagged claimable work without adding a second claim table.
6. `TelegramAdapter` already normalizes inbound updates, extracts `@rosie` and
   `@lenny` as routing metadata, preserves the original body, and renders
   outbound `sendMessage` payloads.
7. `handle_telegram_webhook` already validates
   `X-Telegram-Bot-Api-Secret-Token` before ingest and maps Telegram chats to
   OpenClaw rooms.
8. `FleetController.assign_task_from_command_ref` already verifies signed
   command refs, acquires repo/task leases, selects eligible hosts, and
   publishes `openclaw.task.<host_id>.assigned`.
9. `FleetController.claim_message_as_task` already promotes claimable chat
   messages and assigns the claimant host when eligible.
10. `HostInventoryRecord.host_aliases` already supports aliases from heartbeat
    payloads/capabilities.
11. `TaskInbox` already deduplicates task assignments by task ID, persists ACK
    attempts, fails closed on lease conflicts, and publishes
    `openclaw.task.<host_id>.ack`.
12. `HostInbox` already validates host message deliveries and publishes
    idempotent `openclaw.host.<host_id>.messages.ack`.
13. `NatsClient` already provides injectable synchronous `publish`,
    `subscribe`, and JetStream KV helpers for tests and host integration.

Do not add a second Telegram bot, per-host Telegram table, direct Rosie/Lenny
command service, or independent claim table unless a failing test proves the
existing command-ref plus lease model cannot satisfy the behavior.

## Required Behavior

1. Store every Telegram inbound update as one canonical OpenClaw message in
   `MessagingStore`, with delivery rows for host/run/controller/adapter
   recipients as policy requires.
2. Support webhook ingestion and long-poll ingestion through the same
   normalization and persistence path. Long-polling must use an injectable HTTP
   client/transport and persist or explicitly return update offsets so replay
   does not duplicate messages.
3. Do not hard-code bot tokens, webhook secrets, chat IDs, host IDs, or NATS
   credentials. Use function parameters, config objects, or environment-backed
   app setup. Tests must use fake tokens/transports only.
4. `@lenny` and `@rosie` are host aliases. Store aliases as routing metadata
   or target metadata; do not treat them as Telegram users, host IDs,
   authorization keys, or separate bot names.
5. Explicit alias messages are hard routing hints. `@lenny ...` must resolve to
   a host whose heartbeat advertises alias `lenny`; `@rosie ...` must resolve
   to alias `rosie`. Unknown, ambiguous, stale, repo-ineligible, or
   provider-ineligible aliases must reject without silently falling back.
6. `/assign <task_id> @lenny <prompt>` and `/task <task_id> @rosie <prompt>`
   must preserve the original Telegram body, store the alias in metadata, store
   the executable prompt in assignment metadata, create a signed
   `assign_task` command ref only after identity/route policy allows it, and
   route assignment through the Fleet Controller.
7. Untagged actionable Telegram messages in mapped fleet/task rooms must become
   claimable work. They must not immediately start duplicate runs on Lenny and
   Rosie.
8. A claimable message must promote to exactly one pending `assign_task`
   command ref for the original `message_id`. If the human did not supply a
   task ID, derive a deterministic one such as `telegram-msg:<message_id>`.
9. Host task claims must be claimant-aware. A valid claim from Lenny assigns
   Lenny when Lenny is eligible and delivery-visible; it must not select Rosie
   because Rosie sorts first.
10. Claiming must be atomic. Racing claims from Lenny and Rosie must produce
    exactly one `openclaw.task.<winner>.assigned` publish; losers receive a
    deterministic claimed/consumed/lease-conflict rejection and must not
    dispatch a local run.
11. A claimant outside the message delivery scope cannot steal work. For
    untagged fleet-room work with no explicit host delivery rows, the claim
    route may create the selected host delivery only after eligibility passes.
12. Controller NATS integration must consume host heartbeat/capability updates,
    task ACKs, and host message ACKs through injectable NATS subscriptions or
    route-equivalent test hooks, then persist those state changes in the
    controller/messaging stores.
13. Controller NATS publishing must keep task assignment subjects scoped to
    `openclaw.task.<host_id>.assigned` and host message delivery subjects
    scoped to `openclaw.host.<host_id>.inbox`.
14. Host daemons must consume only their scoped delivery subjects, persist task
    and host-message ACK attempts locally, and publish ACKs through NATS or the
    existing outbox when direct publish fails.
15. Replayed Telegram webhook updates, replayed long-poll updates, replayed
    command refs, replayed task deliveries, and replayed ACKs must be
    idempotent. They must not create duplicate messages, command refs,
    deliveries, NATS task publishes, local task submissions, or ACK rows.
16. Keep command promotion policy intact. Telegram command-like text remains
    chat unless the adapter is explicitly enabled, the platform identity is
    verified with `command:write`, and the room route policy allows the command
    type and target kind.
17. Update docs to make the final architecture clear: one Telegram adapter,
    one persistent messaging store, one controller assignment/claim path, NATS
    scoped delivery, and no per-host Telegram control plane.

## Acceptance Criteria

- A Telegram update for `@lenny please check my email` creates one stored
  message with routing metadata `host_alias=lenny`; assignment resolves to
  Lenny's stable host ID or rejects clearly without falling back to Rosie.
- `@rosie please check my email` behaves the same for Rosie.
- `/assign task-123 @lenny repair the inbox test` creates one allowed signed
  `assign_task` command ref, preserves the original room message body, uses
  `repair the inbox test` as the task prompt, and publishes only
  `openclaw.task.<lenny_host_id>.assigned`.
- `please check my email` creates one claimable chat message. If Lenny and
  Rosie both claim it, exactly one host gets exactly one task assignment
  publish and the other receives a deterministic rejection.
- Replayed webhook and long-poll Telegram updates return the existing message
  and do not create duplicate command refs or delivery rows.
- Replayed task claims, command refs, task deliveries, and ACK events do not
  dispatch duplicate local Agent Runs or duplicate ACK rows.
- Host task ACKs update or can be reconciled with
  `openclaw_message_deliveries`; host message ACKs are persisted
  monotonically and do not downgrade already-acked deliveries.
- A host-scoped principal cannot claim work as another host, cannot ingest
  another host's heartbeat, and cannot ACK another host's delivery.
- Tests prove the controller can bridge assignment through NATS into a hostd
  `TaskInbox` using fakes, with the host submitting exactly one local task.
- Tests prove controller-side NATS consumers or equivalent test hooks persist
  host heartbeat/capability updates and ACK events without requiring a live
  broker.
- Docs explicitly state that bot tokens/secrets live in configuration and are
  never committed or embedded in task prompts, tests, or examples.

## Verification

Run targeted tests first:

```bash
python3 -m pytest tests/openclaw_messaging/test_telegram_adapter.py -q
python3 -m pytest tests/openclaw_messaging/test_message_delivery.py -q
python3 -m pytest tests/openclaw_controller/test_scheduler.py -q
python3 -m pytest tests/openclaw_controller/test_api.py -q
python3 -m pytest tests/openclaw_hostd/test_inbox.py -q
python3 -m pytest tests/openclaw_hostd/test_service.py -q
```

Then run the broader suite:

```bash
python3 -m pytest tests/openclaw_messaging -q
python3 -m pytest tests/openclaw_controller -q
python3 -m pytest tests/openclaw_hostd -q
python3 -m pytest tests -q
```

If a full-suite failure is unrelated to this slice, document the exact failing
test, command, and reason in the final Sandcastle report instead of hiding it.

## Completion

Commit the completed work on the Sandcastle branch.

The final Sandcastle output must include a concise implementation summary, the
verification commands run, any known unrelated failures, and exactly:

```text
<promise>COMPLETE</promise>
```
