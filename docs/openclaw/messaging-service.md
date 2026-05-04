# OpenClaw Messaging Service

The OpenClaw Messaging Service is the durable room, message, delivery, and
command-reference layer for Milestone 1. It is embedded by the minimal
controller app in this slice and uses a self-contained SQLite store.

Telegram is only an adapter at this layer. Lenny and Rosie do not run separate
Telegram bots, direct Telegram command parsers, or host-to-host Telegram
bridges. Every inbound Telegram event is stored once as a canonical OpenClaw
message, and every mutating action still flows through signed command refs,
Fleet Controller eligibility checks, central leases, scoped NATS delivery, and
persistent ACK state.

## Responsibilities

- Store rooms for fleet, repo, task, run, host, and swarm conversations.
- Normalize web UI and adapter input into one message envelope.
- Store one message once, then create separate delivery records for hosts,
  Agent Runs, adapters, web clients, or controller recipients.
- Track delivery state as queued, delivered, acked, failed, or expired.
- Convert mutating messages into signed command references before host, run,
  agent, or controller delivery.
- Keep room-to-platform mappings and external identity links in the same
  store as message routing policy.
- Apply high-signal notification rules for `needs_attention`, `blocked`,
  `failed`, `completed`, `lease_conflict`, and `verification_blocked`.

It does not assign work to hosts, acquire leases, publish host tasks directly,
or own local Agent Run status. Those are later Fleet Controller and host daemon
responsibilities.

When embedded in `OpenClawControllerApp`, a newly created Telegram
`assign_task` command reference is handed to the Fleet Controller immediately.
That app-level handoff keeps scheduling and leases in the Fleet Controller
while allowing one Telegram intake chat to submit work without a second manual
API call. The same handoff runs for webhook ingestion and long-poll ingestion.

## Storage

The SQLite schema is created by `code_index.openclaw_messaging.store`:

```text
openclaw_rooms
openclaw_messages
openclaw_message_deliveries
openclaw_command_refs
openclaw_messaging_adapters
openclaw_platform_room_mappings
openclaw_external_identities
openclaw_adapter_cursors
```

`openclaw_messages.idempotency_key` is unique. Adapter events derive it from
adapter ID, platform room/thread ID, and platform event/message ID so replayed
webhook updates return the existing message and do not create duplicate
deliveries or command refs.

Delivery rows carry a normalized delivery key in addition to recipient
kind/ID. Adapter deliveries include platform target identity in that key so
two Telegram room targets do not collapse into one delivery. ACK updates are
monotonic; a late `delivered` report cannot downgrade an already `acked`
delivery. When a message has multiple deliveries with the same recipient
kind/ID, ACK calls must identify the exact row with `delivery_id` or
`delivery_key`; recipient kind/ID alone is rejected as ambiguous.

`openclaw_adapter_cursors` stores adapter-owned replay cursors such as Telegram
`getUpdates` offsets. The adapter may return the next offset to the caller,
persist it in the store, or do both. Replay safety still depends on the
message idempotency key, so repeated webhook updates, repeated poll batches,
and repeated ACK reports all resolve back to the existing canonical rows.

## Rooms And Projections

Rooms are the user-facing conversation model. A task room can include swarm
metadata:

```json
{
  "swarm": {
    "lead_run": {"run_id": "run-lead", "agent_name": "Kimi Swarm Lead"},
    "child_runs": [
      {"run_id": "run-impl", "agent_name": "Kimi Implementer", "role": "implementer"}
    ]
  }
}
```

`get_room_projection()` expands that metadata into participants so the UI can
show the Swarm Lead and child Agent Runs in one task room. The projection is
not a fan-out rule to Telegram; adapter notification targets remain explicit.

## Target Preview

`preview_target()` supports `fleet`, `task`, `swarm`, `run`, and `host` target
scopes. It returns the expected recipients before send. Task rooms can include
both delivery targets and notification targets, allowing one room message to
fan out to host/run recipients while sending a single adapter notification.

For automatic fleet pickup, a command message can omit host delivery targets.
The Fleet Controller treats existing host delivery records as explicit
predelegation; if none exist, it chooses an eligible host and creates the host
delivery record during assignment. This lets a single Telegram-mapped fleet
room accept `/assign <task_id> <task prompt>` and still preserve per-recipient
delivery state for the selected host.

## Host Aliases And Claimable Work

Telegram-facing names such as `@rosie` and `@lenny` are host aliases. They are
not host IDs, Telegram identities, or authorization keys. Before execution, the
Fleet Controller resolves a host alias to one stable host ID and records the
chosen host in delivery and lease state.

Explicit mentions are hard routing hints:

```text
@rosie please check my email
@lenny summarize the fleet state
/assign task-123 @rosie repair the failing inbox test
```

After identity and room policy validation, an explicit alias creates or
constrains the host delivery record for that message. Scheduling still goes
through the Fleet Controller. If the resolved host is stale, lacks the repo or
provider capability, or conflicts with an active lease, the message is rejected
or left unclaimed rather than falling through to another host silently.

Untagged actionable messages become claimable work:

```text
please check my email
look into the latest failed run
```

Claimable work is visible to eligible hosts in the mapped room. Rosie and Lenny
may both observe the same message and use their local memory, capabilities, and
current context to decide whether they can handle it. A host must make a task
claim before starting an Agent Run. The claim path acquires the central task
lease for the deterministic task ID derived from the message or command
reference. The first eligible host that wins the lease receives the task
assignment; later claims get a `task_lease_conflict` or already-claimed result
and must not dispatch a local run.

The Messaging Service remains the room and delivery layer. It stores the
incoming Telegram message once, stores host-visible delivery records for the
claim opportunity, and records claim/assignment/rejection events in the room
timeline. It does not let Rosie and Lenny each create separate tasks from the
same untagged message.

Implementation status:

1. Existing `openclaw_command_refs` rows already act as single-use controller
   command claims for explicit mutating commands. `claim_command_ref` flips a
   pending command to active before the Fleet Controller publishes work.
2. Existing task leases already prevent duplicate Agent Runs if two hosts ever
   receive or replay the same task assignment.
3. Untagged Telegram messages marked as claimable work can be promoted into one
   pending `assign_task` command reference for the same message, using a
   deterministic task ID such as `telegram-msg:<message_id>` when no task ID
   was supplied.
4. The Fleet Controller claim path accepts a `claimant_host_id`, verifies that
   host is eligible and delivery-visible, and assigns that same host instead of
   sorting all eligible hosts. Replayed or racing claims reuse the existing
   command-ref state and return claimed/consumed results without publishing a
   second task.

## Routes

`MessagingRouter` provides the slice API surface:

```text
GET  /rooms
GET  /rooms/{room_id}/messages
POST /messages
POST /messages/{message_id}/ack
GET  /messages/stream
POST /messages/preview
POST /adapters/telegram/webhook
POST /adapters/telegram/poll
```

The dispatcher is intentionally framework-free. A future HTTP server can wrap
the same router without changing store behavior.

`POST /adapters/telegram/poll` uses an injected HTTP transport plus a
configured bot token. It calls the same Telegram normalization and persistence
path as the webhook handler and can persist the next Telegram update offset in
`openclaw_adapter_cursors`.

`POST /messages` signs commands only when the trusted route context includes a
principal with `command:write`. A JSON body field named `principal` is ignored
for authorization because request bodies are untrusted. Chat messages may be
accepted without a trusted principal in this slice, but mutating command refs
are never created by an anonymous route caller.

## Command References

Messages with `message_type = command` create an `openclaw_command_refs` row.
The store requires an explicit signing secret; there is no production default.
The command payload is signed with deterministic local HMAC for Milestone 1
tests. Verification checks HMAC, expected key ID, pending/active status,
expiration, DB row identity, command/message IDs, target fields, and message
body hash. Host/run/controller deliveries for command messages include the
`command_id` in delivery metadata.

External adapters cannot create command refs unless adapter policy allows
promotion, the platform user is linked to a verified OpenClaw identity with
`command:write`, the event maps to the room through an explicit platform-room
mapping, and that mapping allows the command type and target kind.

## Controller ACK Reconciliation

The Fleet Controller consumes:

- `openclaw.host.*.heartbeat`
- `openclaw.host.*.capabilities`
- `openclaw.task.*.ack`
- `openclaw.host.*.messages.ack`

Host task ACKs and host message ACKs reconcile back onto
`openclaw_message_deliveries`. The controller does not create a second durable
ACK table for cross-host delivery state. A host may acknowledge only its own
delivery row; mismatched `host_id` and `delivery_id` pairs are rejected.

## Configuration

Telegram bot tokens and webhook secret tokens live in configuration, injected
app setup, or environment such as `OPENCLAW_TELEGRAM_BOT_TOKEN` and
`OPENCLAW_TELEGRAM_SECRET_TOKEN`.

Do not commit tokens or secrets to the repo, include them in tests, or embed
them in task prompts or message bodies.
