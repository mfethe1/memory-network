# OpenClaw Messaging Adapters

Messaging adapters are thin platform edges. They parse inbound events, render
outbound notifications, report delivery acknowledgements, expose health, and
declare capabilities. They do not own room history and they do not mutate host
or Fleet Controller state directly.

## Contract

Every adapter implements the same behavior:

```text
normalize_inbound(payload) -> MessageDraft
render_outbound(delivery) -> platform payload
acknowledge_delivery(payload) -> DeliveryAck
health() -> AdapterHealth
capabilities() -> AdapterCapabilities
```

Inbound normalization must strip untrusted command refs or execution
directives. Text that looks like a command is still just a message until the
Messaging Service validates identity and policy and creates its own signed
command reference.

## Default Registrations

The default registry creates:

```text
web        command promotion enabled
telegram   command promotion disabled by default
slack      command promotion disabled by default
discord    command promotion disabled by default
matrix     command promotion disabled by default
email      command promotion disabled by default
webhook    command promotion disabled by default
```

Slack, Discord, Matrix, email, and webhook are registered as policy stubs in
Milestone 1. They expose capabilities and routing posture but cannot promote
commands without explicit adapter policy changes and verified identity links.

## Identity And Policy

External identity links live in `openclaw_external_identities` and carry
OpenClaw scopes such as:

```text
message:write
command:propose
command:write
```

Command promotion requires all of:

1. `openclaw_messaging_adapters.command_promotion_enabled = true`
2. A verified identity link with `command:write`
3. A platform-room mapping for the adapter room/thread to the OpenClaw room
4. A route policy that enables promotion for the command type and target kind

If any condition is missing, the inbound event is stored as chat with
`metadata.command_promotion = blocked` and no command ref is created.

## Telegram

The Telegram adapter handles inbound webhook updates and outbound notification
payloads. Inbound webhook handling requires the configured Telegram secret
token to match the `X-Telegram-Bot-Api-Secret-Token` header before identity
lookup or command promotion runs. It derives idempotency from:

```text
telegram:<platform_room_id>:<platform_thread_id>:<platform_event_id>
```

For Telegram, `platform_event_id` is the update ID when available, falling
back to the message ID. A replayed update returns the existing message,
deliveries, and command ref.

The host-claim routing extension must recognize:

```text
/assign <task_id> <task prompt>
/task <task_id> <task prompt>
@rosie <task prompt>
@lenny <task prompt>
/assign <task_id> @rosie <task prompt>
/task <task_id> @lenny <task prompt>
```

The `/assign` and `/task` forms set the message target scope to `task` and put
the prompt text in message metadata for the Fleet Controller assignment
payload. The mention forms add a host-alias routing hint. The original Telegram
text remains the room message body for audit and command-signature
verification.

Host aliases are resolved by the Fleet Controller after identity and route
policy validation. The Telegram adapter must not turn `@rosie` or `@lenny` into
host IDs by itself, and it must not create separate command paths per host.

An untagged, actionable message in a mapped fleet or task room is claimable
work. The adapter stores it as one OpenClaw message and leaves claiming to the
Fleet Controller and host daemons. Eligible hosts may observe the same message,
but only the host that wins the central task lease may start an Agent Run.
Hosts claim stored Telegram work through the Fleet Controller task-claim route;
the route promotes the existing message to one signed command reference and
assigns the claimant host when it is eligible.

Outbound rendering returns a Bot API-shaped payload:

```json
{
  "method": "sendMessage",
  "payload": {
    "chat_id": "-100123",
    "text": "[completed] Task completed.",
    "disable_web_page_preview": true
  }
}
```

## Webhook Posture

The generic webhook adapter can create inbound messages for audit and room
timeline visibility. It cannot create signed commands by default. This keeps
scripts and future integrations from becoming an unreviewed control path
around the Messaging Service and Fleet Controller.
