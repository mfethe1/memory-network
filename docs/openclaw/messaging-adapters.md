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

Command promotion requires both:

1. `openclaw_messaging_adapters.command_promotion_enabled = true`
2. A verified identity link with `command:write`

If either condition is missing, the inbound event is stored as chat with
`metadata.command_promotion = blocked` and no command ref is created.

## Telegram

The Telegram adapter handles inbound webhook updates and outbound notification
payloads. It derives idempotency from:

```text
telegram:<platform_room_id>:<platform_thread_id>:<platform_event_id>
```

For Telegram, `platform_event_id` is the update ID when available, falling
back to the message ID. A replayed update returns the existing message,
deliveries, and command ref.

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
