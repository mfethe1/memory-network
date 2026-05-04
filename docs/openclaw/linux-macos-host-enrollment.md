# Linux/macOS Host Enrollment

This is the M1 deployment path for the two local OpenClaw hosts:

- Lenny: Linux host using user `systemd` services.
- Rosie: macOS host using user `launchd` services.

Both hosts publish stable `host_id` scoped NATS subjects. The human names
`lenny` and `rosie` are `host_aliases`; they are routing labels for Telegram and
Fleet Controller assignment, not authorization keys.

## Shared Prerequisites

On each host:

1. Clone or sync this repo.
2. Create `.venv` and install the package with `python -m pip install -e .`.
3. Make the persistent NATS broker reachable over the private network.
4. Use a controller signing secret and Telegram tokens only in controller-side
   service config; do not place Telegram secrets on host daemons.
5. Provision NATS streams, KV buckets, and host consumers from an admin
   deployment context before routine host enrollment. Host installers do not
   mutate shared broker topology unless `--provision-broker` is explicitly set.

Use the same broker URL for both hosts:

```bash
export OPENCLAW_NATS_URL="nats://openclaw-m1-broker-01.internal:4222"
```

## Lenny Linux Install

Run from the deployed repo on Lenny:

```bash
python scripts/install_openclaw_m1_systemd.py \
  --repo "$PWD" \
  --host-display-name lenny \
  --host-alias lenny \
  --nats-url "$OPENCLAW_NATS_URL"
```

Then verify:

```bash
systemctl --user status ai.openclaw.memory-claude-m1.graph-server.service
systemctl --user status ai.openclaw.memory-claude-m1.hostd.service
systemctl --user status ai.openclaw.memory-claude-m1.fleet-mcp.service
journalctl --user -u ai.openclaw.memory-claude-m1.hostd.service -n 100 --no-pager
```

For a dry run without starting services:

```bash
python scripts/install_openclaw_m1_systemd.py \
  --repo "$PWD" \
  --host-display-name lenny \
  --host-alias lenny \
  --nats-url "$OPENCLAW_NATS_URL" \
  --no-start
```

## Rosie macOS Install

Run from the deployed repo on Rosie:

```bash
python scripts/install_openclaw_m1_launchd.py \
  --repo "$PWD" \
  --host-display-name rosie \
  --host-alias rosie \
  --nats-url "$OPENCLAW_NATS_URL"
```

Then verify:

```bash
launchctl print "gui/$(id -u)/ai.openclaw.memory-claude-m1.graph-server"
launchctl print "gui/$(id -u)/ai.openclaw.memory-claude-m1.hostd"
launchctl print "gui/$(id -u)/ai.openclaw.memory-claude-m1.fleet-mcp"
tail -n 100 "$HOME/.openclaw/logs/memory-claude-openclaw-m1/hostd.log"
```

For a dry run without bootstrapping services:

```bash
python scripts/install_openclaw_m1_launchd.py \
  --repo "$PWD" \
  --host-display-name rosie \
  --host-alias rosie \
  --nats-url "$OPENCLAW_NATS_URL" \
  --no-start
```

## Controller Verification

After both host daemons are running, the controller should see two active host
records. Each heartbeat must include a stable `host_id` and exactly one routing
alias:

```text
lenny host: host_aliases = ["lenny"]
rosie host: host_aliases = ["rosie"]
```

Telegram assignment should then resolve by alias:

```text
@lenny summarize the local repo state
@rosie check the graph server health
```

The controller must deliver those assignments to
`openclaw.task.<host_id>.assigned`; it must not publish to alias-scoped subjects.

## Admin Broker Provisioning

Routine Lenny/Rosie installs should omit `--provision-broker`. Use that flag
only when intentionally running the installer as the broker-admin bootstrap path
for a fresh M1 environment:

```bash
python scripts/install_openclaw_m1_systemd.py \
  --repo "$PWD" \
  --host-display-name lenny \
  --host-alias lenny \
  --nats-url "$OPENCLAW_NATS_URL" \
  --no-start \
  --provision-broker
```

For normal redeploys, keep broker provisioning in controller/admin deployment
automation so hosts cannot create, update, or delete streams, consumers, or KV
buckets.
