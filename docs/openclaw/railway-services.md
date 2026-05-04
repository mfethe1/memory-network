# OpenClaw Railway Services

This slice makes the controller plus embedded Messaging API, the Fleet MCP HTTP surface, and the local fumemory-compatible SQLite store usable as Railway services without making Railway the default M1 NATS broker.

## Service Layout

- `openclaw-controller`
  Exposes the controller plus embedded Messaging API.
  Routes existing messaging paths and fleet paths.
  Exposes `GET /health` and `GET /ready`.
- `openclaw-fleet-mcp`
  Runs `code-index fleet-mcp-serve --transport http`.
  Keeps the existing bearer-token posture.
  Reads the same `OPENCLAW_CONTEXT_STORE_PATH` as the controller when you want shared fumemory-compatible context data.
- Optional `openclaw-fumemory`
  Not required for M1 completion durability in this repo because the durable local path is `SQLiteContextStore`.
  If you add one later, keep it private on Railway and point it at a volume-backed SQLite path.
  This slice documents the wiring contract but does not add a dedicated fumemory HTTP entry point.
- External or restart-verified persistent NATS broker
  Required for controller task assignment and fleet coordination.
  Do not treat Railway as the default M1 broker until the restart and deploy verification in [broker-deployment.md](/home/agent/workspace/docs/openclaw/broker-deployment.md:1) passes.

## Required Variables

Controller:

- `OPENCLAW_CONTROLLER_SIGNING_SECRET`
- `OPENCLAW_NATS_URL`
- `OPENCLAW_CONTEXT_STORE_PATH`
- `OPENCLAW_MESSAGING_DB_PATH`
- `OPENCLAW_CONTROLLER_DB_PATH`

Optional controller variables:

- `OPENCLAW_TELEGRAM_SECRET_TOKEN`
- `OPENCLAW_REQUIRE_NATS=1`
- `OPENCLAW_BIND_HOST=::`

Fleet MCP:

- `OPENCLAW_CONTEXT_STORE_PATH`
- `OPENCLAW_FLEET_MCP_TOKEN`
- `OPENCLAW_BIND_HOST=::`

Railway sets `PORT` automatically. When a volume is attached, Railway also sets `RAILWAY_VOLUME_MOUNT_PATH`.

## Path Defaults

In `railway` mode, the controller now derives these defaults when `RAILWAY_VOLUME_MOUNT_PATH` is present:

```text
${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/controller-state.db
${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/messaging.db
${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/context-store.db
```

Startup fails before serving traffic when:

- `OPENCLAW_CONTROLLER_SIGNING_SECRET` is missing
- a required SQLite path resolves to `:memory:` or another in-memory URI
- a Railway SQLite path is not under `RAILWAY_VOLUME_MOUNT_PATH`
- a required path points at a directory
- `OPENCLAW_NATS_URL` is malformed

`/ready` is stricter than `/health`. A missing or unreachable required NATS broker makes the service non-ready while keeping liveness available for diagnosis.

## Private Networking

- Use Railway private DNS names such as `http://openclaw-controller.railway.internal`.
- Bind internal listeners to `::` so the service is reachable on Railway private networking.
- Use `http://`, not public domains, for controller-to-Fleet MCP or future fumemory calls inside the Railway environment.
- Do not assume Linux/macOS fleet hosts can reach `*.railway.internal` directly. Hosts still need a reachable external broker/API path or a VPN/private overlay that you operate.

## Start Commands

Controller:

```bash
code-index-openclaw-controller --serve
```

Fleet MCP:

```bash
code-index fleet-mcp-serve --transport http
```

The Fleet MCP HTTP mode still requires its bearer token. Supply `OPENCLAW_FLEET_MCP_TOKEN` explicitly on Railway instead of relying on a generated local token file.

## Config-As-Code Files

Use per-service Railway config files instead of one shared root config:

- `/deploy/railway/controller.railway.json`
- `/deploy/railway/fleet-mcp.railway.json`

Set each Railway service to use its matching config file path from the dashboard.
If you later add a dedicated fumemory HTTP wrapper, give it its own config file instead of reusing the controller file.
The controller file uses `/ready` as the deployment health check. The Fleet MCP file currently sets start and restart policy only because the existing `mcp` streamable HTTP surface in this repo does not add a dedicated health endpoint.

## Recommended Wiring

Controller service:

```text
OPENCLAW_DEPLOYMENT_MODE=railway
OPENCLAW_BIND_HOST=::
OPENCLAW_REQUIRE_NATS=1
OPENCLAW_NATS_URL=nats://<external-or-proven-broker>:4222
OPENCLAW_CONTROLLER_DB_PATH=${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/controller-state.db
OPENCLAW_MESSAGING_DB_PATH=${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/messaging.db
OPENCLAW_CONTEXT_STORE_PATH=${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/context-store.db
```

Fleet MCP service:

```text
OPENCLAW_DEPLOYMENT_MODE=railway
OPENCLAW_BIND_HOST=::
OPENCLAW_CONTEXT_STORE_PATH=${RAILWAY_VOLUME_MOUNT_PATH}/openclaw/context-store.db
OPENCLAW_FLEET_MCP_TOKEN=<bearer-token>
```

If you split controller and Fleet MCP into different Railway services, attach a volume to both only when they truly need separate local SQLite state. If they must share one context store file, keep them on the same persistent service boundary or move the context layer behind a dedicated service before enabling concurrent writers.
