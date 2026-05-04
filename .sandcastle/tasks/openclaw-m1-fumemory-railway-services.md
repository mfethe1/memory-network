# OpenClaw M1 Slice: fumemory Reliability And Railway Services

## Goal

Make the OpenClaw controller, embedded Messaging Service, Fleet MCP, and local
fumemory-compatible store deployable as reliable Railway services without
turning Railway into the M1 NATS broker.

The result should provide:

- Long-running controller/fleet/messaging service entry points suitable for
  Railway.
- Explicit persistent database/volume path configuration for SQLite-backed
  messaging, fleet/controller state, and fumemory-compatible context storage.
- Durable Completed Work Index recording or sync queuing that does not block
  run completion during fumemory outages.
- Railway deployment config and runbook guidance for controller, Fleet MCP,
  messaging, and fumemory service wiring.
- Health checks and environment validation that fail loudly on missing secrets
  or unsafe ephemeral storage.

## Branch

Suggested Sandcastle branch:

```text
agent/openclaw-m1-fumemory-railway-services
```

## Scope

Owned paths:

- `code_index/openclaw_controller/app.py`
- `code_index/openclaw_controller/**` only for controller/fleet service wiring
  and health/readiness support
- `code_index/commands/mcp_fleet_serve.py`
- `code_index/openclaw_messaging/store.py` only for database path/configuration
  integration required by the service wrapper
- `code_index/openclaw_context/store.py`
- `code_index/openclaw_context/completed_work.py`
- `code_index/openclaw_hostd/fumemory_client.py` if a narrow sync client is
  required
- `code_index/openclaw_hostd/memory_sync.py` if local retry/durability needs a
  small queue helper
- `tests/openclaw_controller/**`
- `tests/openclaw_context/**`
- `tests/openclaw_hostd/test_memory_sync.py` if host-side sync helpers are
  added
- `tests/openclaw_messaging/**` only for persistence/config regression coverage
- `docs/openclaw/fumemory-sync.md`
- `docs/openclaw/railway-services.md`
- `docs/openclaw/broker-deployment.md` only to clarify that Railway services
  depend on a proven external or persistent NATS broker
- Railway config/runbook files such as `railway*.toml`, `railway*.json`,
  `Procfile`, `deploy/railway/**`, or `docs/openclaw/**`
- `pyproject.toml` only for new console entry points or optional dependencies
  required by the long-running service shape

Out-of-scope paths:

- Provider adapters and `code_index/agent_adapters/**`
- Cursor sidecar files under `plugins/cursor-agent-sidecar/**`
- Sandcastle runtime/config files outside this task brief
- Host alias or Telegram claim-routing behavior already covered by
  `openclaw-m1-telegram-host-claim-routing.md`
- SSH recovery behavior already covered by M2 fleet MCP/SSH work
- NATS subject ACL generation beyond docs/runbook references to the existing
  `docs/openclaw/nats-subject-acls.md`
- Creating or depending on real Railway project IDs, service IDs, public
  domains, tokens, or hard-coded secrets
- Making Railway-hosted NATS the default broker before JetStream persistence
  has passed the restart/deploy verification in `docs/openclaw/broker-deployment.md`

## Existing Primitives To Reuse

1. `OpenClawControllerApp` already wraps the framework-free Messaging Router
   and Fleet Router.
2. `code-index-openclaw-controller` and
   `code-index-openclaw-fleet-controller` already point at
   `code_index.openclaw_controller.app:main`, but that CLI is currently a
   one-request dispatcher, not a long-running service.
3. `MessagingStore` already creates the durable room/message/delivery/command
   SQLite schema and requires `OPENCLAW_CONTROLLER_SIGNING_SECRET` for command
   signing.
4. `SQLiteContextStore` already enables WAL, `busy_timeout`, foreign keys, and
   `synchronous=NORMAL`; it stores context pointers, manifests, health events,
   handoffs, and the Completed Work Index tables.
5. `record_completed_work_index(store, **payload)` already deduplicates by
   `idempotency_key`, strips transcript-shaped keys by default, and returns
   `stored=False` with `degraded_reason="fumemory_unavailable"` on store
   failure.
6. `FleetMcpTools` already supports `fleet_query_fumemory`,
   `fleet_get_context_manifest`, and `fleet_publish_work_summary` against an
   injected context store.
7. `code_index.openclaw_hostd.nats_client.NatsPyTransport` already validates
   NATS URLs, publishes, subscribes, supports JetStream KV puts, and verifies
   TTL for `openclaw_agent_states`.
8. `docs/openclaw/broker-deployment.md` and
   `docs/openclaw/nats-subject-acls.md` already define the M1 NATS broker,
   JetStream/KV persistence, and private subject ACL posture.
9. Railway docs currently support config-as-code with per-service start
   commands, healthcheck paths, restart policies, and private networking under
   `*.railway.internal`; Railway volumes are mounted at runtime and expose
   `RAILWAY_VOLUME_MOUNT_PATH`.

## Required Behavior

1. Add a long-running controller service shape that binds to
   `OPENCLAW_BIND_HOST` or Railway-safe defaults and `PORT`. It must expose at
   least `/health` and `/ready`, then route existing messaging and fleet paths
   through `OpenClawControllerApp`.
2. Keep the service framework decision small and justified. Prefer the standard
   library or an already-used dependency. If adding an HTTP dependency, keep it
   optional and explain why the framework-free router cannot reasonably cover
   Railway health and request serving.
3. Add explicit environment validation for production/Railway mode. Missing
   `OPENCLAW_CONTROLLER_SIGNING_SECRET`, unsafe in-memory DB paths, malformed
   NATS URLs, or missing persistent store paths must fail at startup with clear
   errors.
4. Configure SQLite database paths from environment variables such as
   `OPENCLAW_CONTROLLER_DB_PATH`, `OPENCLAW_MESSAGING_DB_PATH`, and
   `OPENCLAW_CONTEXT_STORE_PATH`, with a Railway volume helper that can derive
   paths under `RAILWAY_VOLUME_MOUNT_PATH`.
5. Do not silently use `:memory:` in Railway/production mode for controller,
   messaging, fleet lease, or context/fumemory state.
6. Add health output that reports at least: process alive, messaging DB open,
   context store open, NATS configured/reachable or degraded, and volume path
   configured. Do not include secrets in health payloads.
7. Add readiness behavior that is stricter than liveness. It should return
   non-200 when required DB paths, signing secret, or required NATS/fumemory
   configuration is invalid for the selected mode.
8. Add a Completed Work durability path for run completion. Either wire a
   reliable local Completed Work Index write path using `SQLiteContextStore` or
   add a small retryable fumemory sync queue. In both cases, fumemory outage
   must not block local run completion.
9. If a fumemory sync client is added, make it timeout-bounded,
   idempotency-key based, retryable, and safe by default: no raw transcript
   sync, no provider secrets, and no hard-coded fumemory URL.
10. Make Completed Work entries queryable by file and symbol after local write
    or after a successful sync cycle. Preserve Windows/POSIX path normalization.
11. Keep Fleet MCP on a separate service surface. `fleet-mcp-serve` should be
    able to load the same persistent context store path used by the controller
    service, and its HTTP mode must keep bearer-token posture.
12. Document Railway service layout without project-specific IDs:
    controller/messaging API, Fleet MCP, optional fumemory service, and external
    or proven persistent NATS broker.
13. Document private networking expectations:
    use Railway private DNS (`<service>.railway.internal`) between Railway
    services, bind internal listeners to `::` where needed, use `http://` for
    private Railway HTTP calls, and do not assume Windows hosts can reach
    Railway private networking directly.
14. Keep NATS as the fleet coordination bus. The Railway runbook may point
    services at a NATS URL, but it must say that M1 broker readiness still
    requires the JetStream/KV restart verification in
    `docs/openclaw/broker-deployment.md`.
15. Add Railway config examples for health checks, restart policy, start
    command, and volume-backed database paths. Prefer per-service config files
    or documented dashboard service settings over one ambiguous root config
    when multiple Railway services share the repo.
16. Add tests for startup env validation, health/readiness, persistent DB path
    selection, Completed Work outage degradation, duplicate idempotent writes,
    and Fleet MCP context-store loading.

## Acceptance Criteria

- Starting the controller service with a real SQLite path and
  `OPENCLAW_CONTROLLER_SIGNING_SECRET` exposes `/health` and `/ready`, accepts
  existing messaging routes, and keeps existing Fleet Router behavior.
- In Railway/production mode, startup fails before serving traffic if any
  required persistent DB path would resolve to `:memory:` or an ephemeral
  default.
- If `RAILWAY_VOLUME_MOUNT_PATH` is present, the documented defaults place
  SQLite files under that mount path and create parent directories safely.
- Health payloads never include signing secrets, bearer tokens, Telegram
  tokens, NATS credentials, or fumemory credentials.
- Readiness reports degraded or non-ready when NATS is required and invalid,
  but liveness remains usable for process diagnostics.
- A completed-work payload is recorded exactly once for repeated
  `idempotency_key` submissions.
- A simulated fumemory/context-store outage returns a degraded result and does
  not raise through run completion.
- Completed Work lookup by changed file and affected symbol works after
  restart of the SQLite store.
- `fleet-mcp-serve --describe` still exposes only the existing read-heavy fleet
  tools and does not add generic shell, assign, cancel, update, or lease
  mutation tools.
- `fleet-mcp-serve --db <context-store-path>` can read fumemory pointers from a
  persistent context store.
- The Railway runbook gives concrete commands/config shapes but no hard-coded
  secrets, public domains, project IDs, or service IDs.
- The broker docs continue to say Railway is acceptable for fumemory and
  control APIs, but NATS on Railway is allowed only after JetStream persistence
  has passed restart/deploy verification.

## Verification

Run focused tests first:

```bash
python3 -m pytest tests/openclaw_context/test_completed_work.py -q
python3 -m pytest tests/openclaw_context/test_pointer_store.py -q
python3 -m pytest tests/openclaw_controller/test_api.py -q
python3 -m pytest tests/openclaw_controller/test_fleet_mcp.py -q
python3 -m pytest tests/openclaw_messaging -q
```

If host-side sync helpers are added, also run:

```bash
python3 -m pytest tests/openclaw_hostd/test_memory_sync.py -q
python3 -m pytest tests/openclaw_hostd/test_service.py -q
```

Run the static service checks:

```bash
code-index-openclaw-controller --help
code-index fleet-mcp-serve --describe
```

Run the full suite before committing:

```bash
python3 -m pytest tests -q
```

## Sandcastle Completion

Commit the completed work on the Sandcastle branch and output
`<promise>COMPLETE</promise>` when done.
