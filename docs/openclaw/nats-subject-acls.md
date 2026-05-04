# OpenClaw NATS Subject ACLs

Milestone 1 uses per-role and per-host NKey credentials. No OpenClaw component
uses shared user/password credentials.

## Credential Model

Selected first-test model:

- One NATS account per OpenClaw environment, for example `OPENCLAW_M1_DEV`.
- One NKey user for the Fleet Controller.
- One NKey user for the Messaging Service.
- One NKey user for the OpenClaw Context Manager Agent.
- One NKey user per external adapter identity.
- One NKey user per Windows host, bound to exactly one `host_id`.

Per-host NATS accounts can be added later for stronger account-level isolation,
but the Milestone 1 control surface starts with per-host NKey users and strict
subject ACLs inside the environment account. This keeps the first real test
auditable without introducing account import/export automation before the host
daemon exists.

## Subject Rules

Host credentials are generated from inventory. A host ACL must interpolate the
literal `host_id`; it must not include a wildcard in the host position.

For host `oclh_devbox01`, the allowed data subjects are:

```text
publish allow:
  openclaw.host.oclh_devbox01.heartbeat
  openclaw.host.oclh_devbox01.capabilities
  openclaw.task.oclh_devbox01.ack
  openclaw.run.oclh_devbox01.*.events
  openclaw.run.oclh_devbox01.*.status
  openclaw.run.oclh_devbox01.*.verification
  openclaw.audit.oclh_devbox01
  openclaw.host.oclh_devbox01.messages.ack
  openclaw.context.oclh_devbox01.*.metrics
  openclaw.context.oclh_devbox01.*.manifest.response

subscribe allow:
  openclaw.task.oclh_devbox01.assigned
  openclaw.host.oclh_devbox01.inbox
  openclaw.context.oclh_devbox01.*.manifest.request
  _INBOX.>
```

Host credentials must not allow:

```text
openclaw.task.*.assigned
openclaw.task.*.ack for any other host
openclaw.run.*.*.>
openclaw.host.*.>
openclaw.command.>
openclaw.controller.>
openclaw.message.>
openclaw.adapter.>
$KV.openclaw_controller_config.>
$JS.API.STREAM.>
$JS.API.CONSUMER.>
```

If JetStream push consumers are used, allow only the `$JS.ACK...` subjects
needed to acknowledge messages delivered to that host's pre-created consumers.
Hosts must not create, update, or delete streams or consumers.

## ACL Template

The generated NATS permission block for a host should look like this shape:

```conf
{
  nkey: "UXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
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
        "openclaw.context.oclh_devbox01.*.manifest.response"
      ]
      deny: [
        "openclaw.command.>",
        "openclaw.controller.>",
        "$KV.openclaw_controller_config.>",
        "$JS.API.STREAM.>",
        "$JS.API.CONSUMER.>"
      ]
    }
    subscribe: {
      allow: [
        "openclaw.task.oclh_devbox01.assigned",
        "openclaw.host.oclh_devbox01.inbox",
        "openclaw.context.oclh_devbox01.*.manifest.request",
        "_INBOX.>"
      ]
      deny: [
        "$KV.openclaw_controller_config.>"
      ]
    }
  }
}
```

The Fleet Controller credential owns broker administration and controller
configuration. It may publish `openclaw.task.<host_id>.assigned`, consume host
events, and update `openclaw_controller_config`. Do not reuse that credential
from hosts, adapters, or the Messaging Service.

The Messaging Service credential may publish room events, host inbox messages,
adapter outbound deliveries, and delivery acknowledgements. It must not publish
host heartbeats, Agent Run events, controller config, or broker admin subjects.

External adapter credentials may publish only their own inbound,
acknowledgement, and health subjects:

```text
openclaw.adapter.<adapter_type>.<adapter_id>.inbound
openclaw.adapter.<adapter_type>.<adapter_id>.ack
openclaw.adapter.<adapter_type>.<adapter_id>.health
```

They must not publish Agent Task, host, context manifest, controller, or
command subjects.

The OpenClaw Context Manager Agent credential may consume host context metrics
and publish context health, manifest requests, handoff proposals, and audit
records. It cannot authorize a fresh provider run; that remains a Fleet
Controller action.

## Verification

Run these checks for every newly enrolled host pair `host_a` and `host_b`:

1. Publish a canary Agent Task assignment to
   `openclaw.task.<host_a>.assigned` with the Fleet Controller credential.
2. Confirm `host_a` credentials can read the canary and publish
   `openclaw.task.<host_a>.ack`.
3. Confirm `host_b` credentials cannot read
   `openclaw.task.<host_a>.assigned`.
4. Confirm `host_a` credentials cannot subscribe to
   `openclaw.task.<host_b>.assigned`.
5. Confirm `host_a` credentials cannot publish
   `openclaw.task.<host_b>.ack`.
6. Confirm `host_a` credentials cannot publish or update
   `$KV.openclaw_controller_config.>`.
7. Confirm `host_a` credentials cannot call stream or consumer admin APIs.

Acceptance: a compromised host credential can affect only that host's
heartbeat, capabilities, Agent Run reports, acknowledgements, audit records,
message acknowledgements, and context metric/report subjects. It cannot read
another host's Agent Task subject and cannot publish controller config.
