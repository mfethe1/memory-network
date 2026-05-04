# OpenClaw UI Command Center

The OpenClaw Web UI command center is the operator surface for room timelines,
message delivery state, target preview, and command validation. It is a UI
layout contract for Milestone 1; this slice implements the backing room and
route primitives, not a browser application.

## Layout

Use a dense operational layout:

```text
+-------------------+-------------------------------+----------------------+
| Inbox             | Room timeline                 | Context side panel   |
|                   |                               |                      |
| fleet             | messages and command cards    | task/run/host facts  |
| repo              | delivery state per recipient  | swarm participants   |
| task              | ack/failure details           | source pointers      |
| run               |                               | notification policy  |
| host              | composer + target selector    |                      |
| swarm             |                               |                      |
+-------------------+-------------------------------+----------------------+
```

## Inbox

The inbox lists fleet, repo, task, run, host, and swarm rooms. It should
prioritize rooms with failed, blocked, lease-conflict, verification-blocked, or
needs-attention events. Completed notifications remain visible but should not
crowd out active operator decisions.

## Room Timeline

The timeline renders:

- Human chat
- Agent replies
- Host alerts
- Controller events
- Delivery updates
- Command messages as command cards

For task rooms using an Agent Swarm, the participant strip shows the Swarm Lead
and child Agent Runs from the room projection. This avoids making Telegram or
another adapter carry one separate conversation per child run.

## Target Selector And Preview

The composer includes a target selector for:

```text
fleet
task
swarm
run
host
```

Before send, the UI calls target preview and shows the expected recipients.
This preview is the operator's chance to catch accidental fleet-wide messages
or unexpected adapter notifications.

## Command Cards

Mutating messages render as command cards. A command card shows:

- Command type
- Target scope
- Expected delivery recipients
- Identity and policy validation state
- Signature status
- Expiration time
- Delivery and acknowledgement state

The UI never publishes host tasks directly. It posts a message to the
Messaging Service; the service creates a signed command reference; later Fleet
Controller slices consume that reference under lease and policy rules.

## Delivery State

Each message can have multiple delivery records. The timeline should expose
recipient-level state:

```text
queued
delivered
acked
failed
expired
```

For command messages, host/run/controller deliveries must show the command
reference ID so an operator can audit exactly what was signed.

## Context Side Panel

The side panel shows compact room context:

- Room kind and display name
- Repo, task, run, host IDs when present
- Swarm Lead and child Agent Runs
- Platform room mappings
- Notification policy
- Trace and correlation IDs for selected messages
- Context handles attached to selected messages

The panel should link to local Graph Agent Companion views later, but it should
not duplicate raw transcripts or local graph-server state.
