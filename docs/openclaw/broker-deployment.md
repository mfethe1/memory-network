# OpenClaw Broker Deployment

Slice 0 decision: the first real OpenClaw broker test uses a persistent VM
running NATS with JetStream file storage. Railway is not the NATS target until
JetStream persistence has been proven across restarts, deploys, and network
interruptions.

## Selected Target

Use one small dedicated Linux VM on the private fleet network:

- NATS Server with JetStream enabled.
- Persistent disk mounted at `/var/lib/nats`.
- JetStream store directory at `/var/lib/nats/jetstream`.
- Disk encryption and VM snapshots enabled by the infrastructure provider.
- NATS client port `4222` exposed only on the private network.
- NATS monitoring port `8222` exposed only to admin hosts on the private
  network.
- TLS required whenever a client path leaves the private overlay.

Managed NATS is acceptable later if it provides the same JetStream persistence,
NKey/account controls, monitoring, and restart evidence. Railway may host
`fumemory` and control APIs, but it is not the broker for Milestone 1 until the
restart verification below passes on Railway.

## Broker Role

NATS is the fleet coordination bus, not the Graph Agent Companion local graph
database. The first broker must persist:

- Agent Task assignment and acknowledgement streams.
- Agent Run event, status, and verification streams.
- Audit streams.
- Messaging and room delivery streams.
- Host capability and fleet state KV buckets.
- Controller, messaging, adapter, context, and lease KV buckets.

## Initial Streams And Buckets

Create streams with file storage and explicit retention:

```text
OPENCLAW_TASKS       openclaw.task.*.assigned, openclaw.task.*.ack
OPENCLAW_RUN_EVENTS  openclaw.run.*.*.events, openclaw.run.*.*.status, openclaw.run.*.*.verification
OPENCLAW_AUDIT       openclaw.audit.*, openclaw.context.audit
OPENCLAW_MESSAGES    openclaw.message.>, openclaw.room.*.events, openclaw.host.*.inbox, openclaw.host.*.messages.ack
OPENCLAW_CONTEXT     openclaw.context.*.*.metrics, openclaw.context.*.*.health, openclaw.context.*.*.manifest.*, openclaw.context.*.*.handoff.*
```

Create KV buckets with file storage:

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
openclaw_agent_states
openclaw_mcp_clients
```

## Minimum NATS Posture

The NATS config should be generated from host inventory and role definitions.
The hand-written baseline is:

```conf
server_name: "openclaw-m1-broker-01"

jetstream {
  store_dir: "/var/lib/nats/jetstream"
  max_mem_store: 512M
  max_file_store: 20G
}

authorization {
  # Use operator/account/NKey credentials in the real config.
  # Do not ship shared user/password credentials.
}
```

Operational requirements:

- Keep the VM image ephemeral and the JetStream store on the persistent disk.
- Back up the persistent disk before destructive broker maintenance.
- Run NATS as a supervised service and stop it cleanly during planned restarts.
- Do not let hosts create, update, or delete streams, consumers, or KV buckets.
- Keep stream and KV creation in controller/admin deployment automation.

## Restart Verification

Before any host daemon depends on this broker, run this on the selected VM:

1. Create the streams and KV buckets above.
2. Publish one canary Agent Task assignment to
   `openclaw.task.<host_a>.assigned`.
3. Publish one canary Agent Run event to
   `openclaw.run.<host_a>.<run_id>.events`.
4. Write canary keys to `openclaw_hosts` and `openclaw_agent_states`.
5. Stop NATS, reboot the VM, and start NATS again.
6. Confirm stream state still includes the task and run event messages.
7. Confirm a durable consumer or replay from sequence 1 can read both canary
   messages.
8. Confirm both KV canary keys are still present with the expected revisions.

Acceptance: the restart test passes only if Agent Task assignment data, Agent
Run event data, and KV data survive the VM restart without manual rebuild. If
any canary is missing, do not use that deployment target for Milestone 1.
