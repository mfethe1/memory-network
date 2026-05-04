# OpenClaw NATS Subject ACLs

Milestone 1 uses per-role and per-host NKey credentials. No OpenClaw component
uses shared user/password credentials, and no host receives broad `$JS.>` or
`_INBOX.>` permissions.

## Credential Model

Selected first-test model:

- One NATS account per OpenClaw environment, for example `OPENCLAW_M1_DEV`.
- One NKey user for the Fleet Controller runtime.
- One separate deployment/admin NKey for stream, consumer, and KV setup.
- One NKey user for the Messaging Service.
- One NKey user for the OpenClaw Context Manager Agent.
- One NKey user per external adapter identity.
- One NKey user per Windows host, bound to exactly one `host_id`.

Per-host NATS accounts can be added later for stronger account-level isolation,
but the Milestone 1 control surface starts with per-host NKey users and strict
subject ACLs inside the environment account. This keeps the first real test
auditable without introducing account import/export automation before the host
daemon exists.

## Task Delivery Mode

Milestone 1 host Agent Task delivery uses deployment-created durable push
consumers. Hosts do not create consumers, do not pull messages, and do not
subscribe directly to `openclaw.task.<host_id>.assigned`.

For each host, deployment automation creates one durable push consumer:

```text
stream:          OPENCLAW_TASKS
consumer:        HOST_<host_id>
filter_subject:  openclaw.task.<host_id>.assigned
deliver_subject: openclaw.deliver.<host_id>.tasks
ack_policy:      explicit
```

The host subscribes to `openclaw.deliver.<host_id>.tasks` and ACKs each
delivered task by publishing to the scoped ACK subject carried in the message
reply field. The only ACK publish pattern a host may use is:

```text
$JS.ACK.OPENCLAW_TASKS.HOST_<host_id>.>
```

Do not add these broad permissions to host credentials:

```text
$JS.>
$JS.API.>
$JS.API.CONSUMER.>
$JS.API.CONSUMER.MSG.NEXT.>
$JS.API.STREAM.>
$JS.API.INFO
```

## Bounded Reply Inboxes

NATS clients often use `_INBOX.<random>` reply subjects for request/reply and
JetStream publish acknowledgements. OpenClaw clients must use a bounded inbox
prefix or equivalent NATS response permissions:

```text
_INBOX.<credential_id>.>
```

For hosts, use the `host_id` as the prefix:

```text
_INBOX.oclh_devbox01.>
```

The host daemon's NATS client configuration must set its custom inbox prefix to
`_INBOX.<host_id>` when the client library supports custom inbox prefixes. If a
library cannot set the prefix, the credential must use bounded response
permissions with a short expiry and low max-response count instead. Never grant
hosts `_INBOX.>`.

## Host ACL Template

Host credentials are generated from inventory. A host ACL must interpolate the
literal `host_id`; it must not include a wildcard in the host position.

For host `oclh_devbox01`, the generated NATS permission block should look like
this shape:

```conf
{
  nkey: "UHOSTXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  permissions: {
    publish: {
      allow: [
        "openclaw.host.oclh_devbox01.heartbeat",
        "openclaw.host.oclh_devbox01.capabilities",
        "openclaw.task.oclh_devbox01.ack",
        "openclaw.run.oclh_devbox01.*.events",
        "openclaw.run.oclh_devbox01.*.status",
        "openclaw.run.oclh_devbox01.*.verification",
        "openclaw.audit.oclh_devbox01",
        "openclaw.host.oclh_devbox01.messages.ack",
        "openclaw.context.oclh_devbox01.*.metrics",
        "openclaw.context.oclh_devbox01.*.manifest.response",
        "$JS.ACK.OPENCLAW_TASKS.HOST_oclh_devbox01.>"
      ]
      deny: [
        "openclaw.command.>",
        "openclaw.controller.>",
        "$KV.openclaw_controller_config.>",
        "$JS.API.>"
      ]
    }
    subscribe: {
      allow: [
        "openclaw.deliver.oclh_devbox01.tasks",
        "openclaw.host.oclh_devbox01.inbox",
        "openclaw.context.oclh_devbox01.*.manifest.request",
        "_INBOX.oclh_devbox01.>"
      ]
      deny: [
        "$KV.openclaw_controller_config.>",
        "$JS.API.>"
      ]
    }
  }
}
```

The permission generator must refuse to emit `_INBOX.>` for host credentials.
The only host reply inbox allow is the scoped `_INBOX.<host_id>.>` prefix, or
an equivalent bounded response-permission policy.

Host credentials must not allow:

```text
openclaw.task.*.assigned
openclaw.task.*.ack for any other host
openclaw.deliver.*.tasks for any other host
openclaw.run.*.*.>
openclaw.host.*.>
openclaw.command.>
openclaw.controller.>
openclaw.message.>
openclaw.adapter.>
_INBOX.>
$KV.openclaw_controller_config.>
$JS.API.>
```

## Controller ACL Template

The Fleet Controller runtime credential assigns Agent Tasks, consumes host
reports, and updates controller-owned KV. It does not need host credentials.

```conf
{
  nkey: "UCONTROLLERXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  permissions: {
    publish: {
      allow: [
        "openclaw.task.*.assigned",
        "openclaw.controller.>",
        "$KV.openclaw_controller_config.>",
        "$KV.openclaw_hosts.>",
        "$KV.openclaw_leases.>",
        "$KV.openclaw_provider_caps.>",
        "_INBOX.controller.>"
      ]
    }
    subscribe: {
      allow: [
        "openclaw.host.*.heartbeat",
        "openclaw.host.*.capabilities",
        "openclaw.task.*.ack",
        "openclaw.run.*.*.events",
        "openclaw.run.*.*.status",
        "openclaw.run.*.*.verification",
        "openclaw.audit.*",
        "openclaw.host.*.messages.ack",
        "_INBOX.controller.>"
      ]
    }
  }
}
```

Broker deployment automation may use a separate admin credential with exact
JetStream setup subjects only during provisioning. Generate one consumer API
subject pair per host; the sample below includes host `oclh_devbox01`.

```conf
{
  nkey: "UADMINXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  permissions: {
    publish: {
      allow: [
        "$JS.API.STREAM.CREATE.OPENCLAW_TASKS",
        "$JS.API.STREAM.UPDATE.OPENCLAW_TASKS",
        "$JS.API.STREAM.INFO.OPENCLAW_TASKS",
        "$JS.API.CONSUMER.DURABLE.CREATE.OPENCLAW_TASKS.HOST_oclh_devbox01",
        "$JS.API.CONSUMER.INFO.OPENCLAW_TASKS.HOST_oclh_devbox01",
        "$JS.API.STREAM.CREATE.OPENCLAW_RUN_EVENTS",
        "$JS.API.STREAM.UPDATE.OPENCLAW_RUN_EVENTS",
        "$JS.API.STREAM.INFO.OPENCLAW_RUN_EVENTS",
        "$JS.API.STREAM.CREATE.OPENCLAW_AUDIT",
        "$JS.API.STREAM.UPDATE.OPENCLAW_AUDIT",
        "$JS.API.STREAM.INFO.OPENCLAW_AUDIT",
        "$JS.API.STREAM.CREATE.OPENCLAW_MESSAGES",
        "$JS.API.STREAM.UPDATE.OPENCLAW_MESSAGES",
        "$JS.API.STREAM.INFO.OPENCLAW_MESSAGES",
        "$JS.API.STREAM.CREATE.OPENCLAW_CONTEXT",
        "$JS.API.STREAM.UPDATE.OPENCLAW_CONTEXT",
        "$JS.API.STREAM.INFO.OPENCLAW_CONTEXT",
        "$JS.API.STREAM.CREATE.KV_openclaw_hosts",
        "$JS.API.STREAM.INFO.KV_openclaw_hosts",
        "$JS.API.STREAM.CREATE.KV_openclaw_leases",
        "$JS.API.STREAM.INFO.KV_openclaw_leases",
        "$JS.API.STREAM.CREATE.KV_openclaw_provider_caps",
        "$JS.API.STREAM.INFO.KV_openclaw_provider_caps",
        "$JS.API.STREAM.CREATE.KV_openclaw_controller_config",
        "$JS.API.STREAM.INFO.KV_openclaw_controller_config",
        "$JS.API.STREAM.CREATE.KV_openclaw_message_routes",
        "$JS.API.STREAM.INFO.KV_openclaw_message_routes",
        "$JS.API.STREAM.CREATE.KV_openclaw_messaging_adapters",
        "$JS.API.STREAM.INFO.KV_openclaw_messaging_adapters",
        "$JS.API.STREAM.CREATE.KV_openclaw_platform_room_mappings",
        "$JS.API.STREAM.INFO.KV_openclaw_platform_room_mappings",
        "$JS.API.STREAM.CREATE.KV_openclaw_context_policy",
        "$JS.API.STREAM.INFO.KV_openclaw_context_policy",
        "$JS.API.STREAM.CREATE.KV_openclaw_context_leases",
        "$JS.API.STREAM.INFO.KV_openclaw_context_leases",
        "$JS.API.STREAM.CREATE.KV_openclaw_agent_states",
        "$JS.API.STREAM.INFO.KV_openclaw_agent_states",
        "$JS.API.STREAM.CREATE.KV_openclaw_mcp_clients",
        "$JS.API.STREAM.INFO.KV_openclaw_mcp_clients",
        "_INBOX.admin.>"
      ]
    }
    subscribe: {
      allow: [
        "_INBOX.admin.>"
      ]
    }
  }
}
```

Keep the admin credential out of host machines and routine host-daemon
configuration. If a NATS server version requires a different exact API subject
for durable consumer creation, update the generated allow list to that exact
subject instead of granting `$JS.API.>`.

## Messaging Service ACL Template

The Messaging Service may publish room events, host inbox messages, adapter
outbound deliveries, delivery state, and messaging route KV records.

```conf
{
  nkey: "UMESSAGINGXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  permissions: {
    publish: {
      allow: [
        "openclaw.message.inbound",
        "openclaw.room.*.events",
        "openclaw.host.*.inbox",
        "openclaw.adapter.*.*.outbound",
        "openclaw.deadletter",
        "$KV.openclaw_message_routes.>",
        "$KV.openclaw_messaging_adapters.>",
        "$KV.openclaw_platform_room_mappings.>",
        "_INBOX.messaging.>"
      ]
      deny: [
        "openclaw.task.>",
        "openclaw.run.>",
        "openclaw.host.*.heartbeat",
        "openclaw.host.*.capabilities",
        "$KV.openclaw_controller_config.>",
        "$JS.API.>"
      ]
    }
    subscribe: {
      allow: [
        "openclaw.message.inbound",
        "openclaw.message.inbound.*.*",
        "openclaw.adapter.*.*.inbound",
        "openclaw.adapter.*.*.ack",
        "openclaw.adapter.*.*.health",
        "openclaw.host.*.messages.ack",
        "_INBOX.messaging.>"
      ]
    }
  }
}
```

## Context Manager ACL Template

The OpenClaw Context Manager Agent may consume context metrics and publish
context health, manifest requests, handoff proposals, and audit records. It
cannot authorize a fresh provider run; that remains a Fleet Controller action.

```conf
{
  nkey: "UCONTEXTXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  permissions: {
    publish: {
      allow: [
        "openclaw.context.*.*.health",
        "openclaw.context.*.*.manifest.request",
        "openclaw.context.*.*.handoff.proposed",
        "openclaw.context.audit",
        "$KV.openclaw_context_policy.>",
        "$KV.openclaw_context_leases.>",
        "_INBOX.context.>"
      ]
      deny: [
        "openclaw.task.>",
        "openclaw.command.>",
        "$KV.openclaw_controller_config.>",
        "$JS.API.>"
      ]
    }
    subscribe: {
      allow: [
        "openclaw.context.*.*.metrics",
        "openclaw.context.*.*.manifest.response",
        "openclaw.context.*.*.handoff.ack",
        "_INBOX.context.>"
      ]
    }
  }
}
```

## Adapter ACL Template

Each external adapter credential is scoped to one adapter type and adapter ID.
Example for Telegram adapter `tg_primary`:

```conf
{
  nkey: "UADAPTERXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  permissions: {
    publish: {
      allow: [
        "openclaw.message.inbound.telegram.tg_primary",
        "openclaw.adapter.telegram.tg_primary.inbound",
        "openclaw.adapter.telegram.tg_primary.ack",
        "openclaw.adapter.telegram.tg_primary.health",
        "_INBOX.adapter.telegram.tg_primary.>"
      ]
      deny: [
        "openclaw.command.>",
        "openclaw.task.>",
        "openclaw.host.>",
        "openclaw.context.*.*.manifest.>",
        "$KV.openclaw_controller_config.>",
        "$JS.API.>"
      ]
    }
    subscribe: {
      allow: [
        "openclaw.adapter.telegram.tg_primary.outbound",
        "_INBOX.adapter.telegram.tg_primary.>"
      ]
    }
  }
}
```

## Credential Isolation CLI Checks

These examples assume PowerShell, a running broker, and credential files under
`.\creds`. The `nats` CLI is not required by this repo, but these command
shapes are the intended operator checks.

```powershell
$env:NATS_URL = "nats://openclaw-m1-broker-01.internal:4222"

nats --server $env:NATS_URL --creds .\creds\controller.creds `
  pub openclaw.task.oclh_a.assigned '{"task_id":"canary-a"}'
# Expected: PASS, controller can assign an Agent Task.

nats --server $env:NATS_URL --creds .\creds\host-a.creds `
  sub openclaw.deliver.oclh_a.tasks --count 1 --timeout 5s
# Expected: PASS, host_a can receive its own pushed task delivery.

nats --server $env:NATS_URL --creds .\creds\host-b.creds `
  sub openclaw.deliver.oclh_a.tasks --count 1 --timeout 2s
# Expected: FAIL with authorization violation, or no delivery if the server
# rejects before delivery.

nats --server $env:NATS_URL --creds .\creds\host-b.creds `
  sub openclaw.task.oclh_a.assigned --count 1 --timeout 2s
# Expected: FAIL with authorization violation; hosts cannot subscribe to raw
# Agent Task stream subjects.

nats --server $env:NATS_URL --creds .\creds\host-a.creds `
  sub _INBOX.> --count 1 --timeout 2s
# Expected: FAIL with authorization violation.

nats --server $env:NATS_URL --creds .\creds\host-a.creds `
  sub _INBOX.oclh_a.> --count 1 --timeout 2s
# Expected: PASS authorization and timeout with no messages.

nats --server $env:NATS_URL --creds .\creds\host-a.creds `
  pub '$KV.openclaw_controller_config.canary' 'bad'
# Expected: FAIL with authorization violation.

nats --server $env:NATS_URL --creds .\creds\host-a.creds `
  pub '$JS.API.STREAM.INFO.OPENCLAW_TASKS' '{}'
# Expected: FAIL with authorization violation.
```

Acceptance: a compromised host credential can affect only that host's
heartbeat, capabilities, Agent Run reports, acknowledgements, audit records,
message acknowledgements, context metric/report subjects, scoped JetStream task
ACKs, and bounded reply inbox. It cannot read another host's Agent Task
stream or delivery and cannot publish controller config.
