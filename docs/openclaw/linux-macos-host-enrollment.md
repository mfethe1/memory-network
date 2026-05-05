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

On Linux, enable user lingering when OpenClaw user services must survive reboot
before the user logs in:

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Use the same broker URL for both hosts:

```bash
export OPENCLAW_NATS_URL="nats://openclaw-m1-broker-01.internal:4222"
```

For the canonical broker cutover, both hosts must use the same authenticated
`OPENCLAW_NATS_URL` as the Railway controller. Do not rely on Rosie-local
`--nats-conf` defaults for that cutover.

## Canonical Broker Cutover Targets

The expected production identities and aliases are:

- Lenny: `host_id = host_6a163e09f5744561a0827d30253b3ba8`, `host_aliases = ["lenny"]`
- Rosie: `host_id = host_a23037f43daa41b19d1d441ec514af33`, `host_aliases = ["rosie"]`

Both installers reuse
`~/.openclaw/state/memory-claude-openclaw-m1/hostd/host-identity.json` when it
already exists. That file is the stable host identity source for cutover. Do
not delete or replace it during Rosie/Lenny broker migration.

## Canonical Broker Cutover Preflight

These checks are safe to run before cutover. They do not restart services,
provision broker resources, or print broker credentials.

On either host, first confirm the shared canonical broker URL is set and points
at the expected host and port:

```bash
test -n "$OPENCLAW_NATS_URL"
python3 - <<'PY'
import os
from urllib.parse import urlsplit

parts = urlsplit(os.environ["OPENCLAW_NATS_URL"])
print(
    {
        "scheme": parts.scheme,
        "hostname": parts.hostname,
        "port": parts.port,
        "has_auth": bool(parts.username or parts.password),
    }
)
PY
```

On Lenny, verify the existing host identity, then rewrite the config with
`--no-start` and confirm the generated alias and broker URL:

```bash
export OPENCLAW_EXPECTED_HOST_ID="host_6a163e09f5744561a0827d30253b3ba8"
export OPENCLAW_EXPECTED_ALIAS="lenny"
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path.home() / ".openclaw/state/memory-claude-openclaw-m1/hostd/host-identity.json"
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload["host_id"] == os.environ["OPENCLAW_EXPECTED_HOST_ID"], payload
print({"identity_path": str(path), "host_id_ok": True})
PY
python3 scripts/install_openclaw_m1_systemd.py \
  --repo "$PWD" \
  --host-display-name lenny \
  --host-alias lenny \
  --nats-url "$OPENCLAW_NATS_URL" \
  --no-start
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path.home() / ".openclaw/config/memory-claude-openclaw-m1-hostd.json"
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload["host_aliases"] == [os.environ["OPENCLAW_EXPECTED_ALIAS"]], payload
assert payload["nats_url"] == os.environ["OPENCLAW_NATS_URL"], "nats_url mismatch"
print(
    {
        "config_path": str(path),
        "host_aliases": payload["host_aliases"],
        "nats_url_matches_env": True,
    }
)
PY
```

On Rosie, run the same non-starting preflight against the macOS installer:

```bash
export OPENCLAW_EXPECTED_HOST_ID="host_a23037f43daa41b19d1d441ec514af33"
export OPENCLAW_EXPECTED_ALIAS="rosie"
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path.home() / ".openclaw/state/memory-claude-openclaw-m1/hostd/host-identity.json"
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload["host_id"] == os.environ["OPENCLAW_EXPECTED_HOST_ID"], payload
print({"identity_path": str(path), "host_id_ok": True})
PY
python3 scripts/install_openclaw_m1_launchd.py \
  --repo "$PWD" \
  --host-display-name rosie \
  --host-alias rosie \
  --nats-url "$OPENCLAW_NATS_URL" \
  --no-start
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path.home() / ".openclaw/config/memory-claude-openclaw-m1-hostd.json"
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload["host_aliases"] == [os.environ["OPENCLAW_EXPECTED_ALIAS"]], payload
assert payload["nats_url"] == os.environ["OPENCLAW_NATS_URL"], "nats_url mismatch"
print(
    {
        "config_path": str(path),
        "host_aliases": payload["host_aliases"],
        "nats_url_matches_env": True,
    }
)
PY
```

If those checks pass on both machines, the cutover can proceed to the live
service restart window with stable host IDs, stable aliases, and a shared
canonical broker URL already staged in config.

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
tail -n 100 "$HOME/.openclaw/logs/ai.openclaw.memory-claude-m1.hostd.log"
tail -n 100 "$HOME/.openclaw/logs/ai.openclaw.memory-claude-m1.hostd.err.log"
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
/task task-lenny @lenny summarize the local repo state
/assign task-rosie @rosie check the graph server health
```

The controller must deliver those assignments to
`openclaw.task.<host_id>.assigned`; it must not publish to alias-scoped subjects.
Bare `@alias` messages are promoted to generated task IDs with the
`telegram-msg:<message_id>` shape. `/task` and `/assign` let operators choose
the task ID explicitly.

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
