# OpenClaw Messaging Service

The OpenClaw Messaging Service is the durable room, message, delivery, and
command-reference layer for Milestone 1. It is embedded by the minimal
controller app in this slice and uses a self-contained SQLite store.

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
```

`openclaw_messages.idempotency_key` is unique. Adapter events derive it from
adapter ID, platform room/thread ID, and platform event/message ID so replayed
webhook updates return the existing message and do not create duplicate
deliveries or command refs.

Delivery rows carry a normalized delivery key in addition to recipient
kind/ID. Adapter deliveries include platform target identity in that key so
two Telegram room targets do not collapse into one delivery. ACK updates are
monotonic; a late `delivered` report cannot downgrade an already `acked`
delivery.

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
```

The dispatcher is intentionally framework-free. A future HTTP server can wrap
the same router without changing store behavior.

`POST /messages` signs commands only when the request includes a principal
with `command:write`. Chat messages may be accepted without that principal in
this slice, but mutating command refs are never created by an anonymous route
caller.

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
