# OpenClaw M1 Slice: Telegram Host Aliases And Claimable Work

## Goal

Implement the Rosie/Lenny routing path for the single Telegram Messaging
Service:

- `@rosie` and `@lenny` explicitly target one OpenClaw host alias.
- Untagged actionable messages are claimable work that eligible hosts can
  observe.
- The first valid host claim wins and only one Agent Run can start for the
  message.

## Scope

Owned paths:

- `code_index/openclaw_messaging/telegram.py`
- `code_index/openclaw_messaging/store.py`
- `code_index/openclaw_messaging/models.py` only if a narrow model field is
  required
- `code_index/openclaw_controller/scheduler.py`
- `code_index/openclaw_controller/app.py` or controller routes only if needed
  to expose the claim endpoint
- `tests/openclaw_messaging/test_telegram_adapter.py`
- `tests/openclaw_controller/test_scheduler.py`
- `tests/openclaw_controller/test_api.py` only if an HTTP claim endpoint is
  added
- `docs/openclaw/messaging-service.md`
- `docs/openclaw/messaging-adapters.md`
- `plans/openclaw-network-control-plane-plan.md`

Do not edit provider adapters, Fleet MCP, live CMA, Sandcastle runtime files,
or Cursor sidecar files in this slice.

## Existing Primitives To Reuse

1. `openclaw_command_refs` already provides a single-use command claim:
   `pending -> active -> assigned/rejected`.
2. `FleetController.assign_task_from_command_ref` already verifies signed
   command references before publishing host work.
3. `openclaw_hostd.leases` already provides exclusive task leases.
4. `TaskInbox` already fails closed on duplicate task leases.

Do not add a parallel claim table unless tests prove `openclaw_command_refs`
cannot model the message claim safely.

## Required Behavior

1. Parse `@rosie` and `@lenny` in Telegram messages and `/assign` commands as
   host-alias routing hints. Preserve the original text as the message body.
2. Store host aliases in message metadata or signed command target metadata.
   Do not treat aliases as host IDs, Telegram users, or authorization keys.
3. Resolve host aliases in the Fleet Controller from host inventory metadata
   or heartbeat data. Unknown aliases are rejected with a clear reason.
4. For an untagged actionable Telegram chat message in a mapped room, support a
   host claim path that promotes the existing message to one pending
   `assign_task` command reference.
5. Derive a deterministic task ID for untagged work when no task ID is supplied,
   such as `telegram-msg:<message_id>`.
6. Add claimant-aware assignment. A claim from Lenny must assign Lenny when
   Lenny is eligible and delivery-visible. It must not select Rosie simply
   because Rosie sorts first.
7. Claiming must be atomic. If Rosie and Lenny race to claim the same message,
   exactly one command ref can move to active and exactly one task assignment
   can publish.
8. Losing claimants receive a consumed, already-claimed, or lease-conflict
   result and must not dispatch local work.

## Acceptance Criteria

- `@rosie please check my email` resolves to Rosie's stable host ID and either
  assigns Rosie or rejects the message without silently falling back to Lenny.
- `@lenny please check my email` behaves the same for Lenny.
- `please check my email` creates one claimable message. When Rosie and Lenny
  both claim it, one host gets exactly one `openclaw.task.<host>.assigned`
  publish and the other gets a deterministic rejection.
- Replayed Telegram updates do not create duplicate claimable messages, command
  refs, deliveries, or task publishes.
- A claimant outside the message delivery set cannot steal the work.
- An ineligible claimant, stale claimant, or claimant missing the requested
  repo/provider capability is rejected before assignment.
- Existing explicit `/assign` command behavior remains compatible.

## Verification

Run:

```bash
python3 -m pytest tests/openclaw_messaging/test_telegram_adapter.py -q
python3 -m pytest tests/openclaw_controller/test_scheduler.py -q
python3 -m pytest tests/openclaw_hostd/test_inbox.py -q
python3 -m pytest tests -q
```

Commit the completed work on the Sandcastle branch and output
`<promise>COMPLETE</promise>` when done.
