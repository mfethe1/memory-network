# OpenClaw M1 Slice: Lenny/Rosie Windows Deployment

## Goal

Make the OpenClaw host daemon installable and operable as a Windows PC
service for the two first test hosts, `lenny` and `rosie`, so each host can:

- keep a stable host identity across reboots,
- publish its human host alias in heartbeat/capability snapshots,
- connect outbound to the shared NATS broker and Fleet Controller,
- receive only its own assigned task deliveries,
- expose enough enrollment/runbook documentation for an operator to reproduce
  the install without hard-coded secrets.

This is a deployment/connectivity slice, not a new scheduling algorithm. Reuse
the existing Fleet Controller alias resolution and host-claim behavior.

## Branch Suggestion

Use a Sandcastle branch named:

```text
agent/openclaw-m1-lenny-rosie-windows-deployment
```

## Scope

Owned paths:

- `code_index/openclaw_hostd/config.py`
- `code_index/openclaw_hostd/heartbeat.py`
- `code_index/openclaw_hostd/service.py` only for narrow runtime wiring needed
  by the Windows service/config changes
- `code_index/openclaw_hostd/nats_client.py` only if a bounded inbox prefix or
  credential-loading hook is required by the Windows NATS client posture
- `code_index/openclaw_controller/models.py` only if heartbeat alias parsing
  needs a small compatibility adjustment
- `scripts/install_openclaw_m1_windows.ps1`
- `scripts/install_openclaw_m1_windows.py` only if Python is the better local
  pattern after inspecting the existing systemd/launchd installers
- `scripts/install_openclaw_m1_launchd.py` and
  `scripts/install_openclaw_m1_systemd.py` only for shared helper extraction or
  keeping installer behavior consistent after adding aliases
- `pyproject.toml` only if adding a console entry point for a Windows installer
  or helper command
- `tests/openclaw_hostd/test_heartbeat.py`
- `tests/openclaw_hostd/test_service.py`
- `tests/openclaw_hostd/test_identity.py`
- `tests/openclaw_hostd/test_systemd_installer.py` only if shared installer
  helpers move
- `tests/openclaw_hostd/test_windows_installer.py`
- `tests/openclaw_controller/test_scheduler.py` only for alias heartbeat
  compatibility coverage
- `tests/openclaw_controller/test_api.py` only for end-to-end heartbeat alias
  ingestion coverage
- `docs/openclaw/host-identity.md`
- `docs/openclaw/broker-deployment.md`
- `docs/openclaw/nats-subject-acls.md`
- `docs/openclaw/lease-model.md` only if the Windows runbook changes the
  shared lease-store guidance
- `docs/openclaw/windows-host-enrollment.md`
- `docs/openclaw/windows-host-runbook.md`
- `README.md` only for a short pointer to the Windows OpenClaw runbook

Do not edit unrelated provider adapter work, Messaging Service routing logic,
Fleet MCP/CMA recovery implementation, Cursor sidecar files, Sandcastle runtime
configuration, package lock files, or broad code-index indexing behavior in
this slice.

## Existing Primitives To Reuse

1. `code_index.openclaw_hostd.config.load_config` already supports JSON config
   plus environment overrides for state dir, identity path, repo roots, graph
   URL/token, SSH hostname, heartbeat interval, NATS URL, fleet lease store,
   and context store.
2. `load_or_create_host_identity` already creates one stable host identity with
   Windows transient file-access retry coverage.
3. `build_heartbeat_payload` already publishes top-level `host_id`,
   `ssh_hostname`, heartbeat interval, repo roots, providers, OS details, and
   graph server status without leaking secrets.
4. `publish_host_snapshot` already publishes
   `openclaw.host.<host_id>.heartbeat` and
   `openclaw.host.<host_id>.capabilities`.
5. `HostInventoryRecord.from_heartbeat` already accepts `host_aliases`,
   `aliases`, or `host_alias` from top-level heartbeat payloads or capabilities
   and lowercases aliases for Fleet Controller resolution.
6. Existing alias/claim tests in `tests/openclaw_controller/test_scheduler.py`
   and `tests/openclaw_controller/test_api.py` already cover `rosie`/`lenny`
   routing once heartbeats include aliases.
7. `scripts/install_openclaw_m1_launchd.py` and
   `scripts/install_openclaw_m1_systemd.py` already establish the install
   shape: state/config/log dirs, host identity creation, hostd JSON config,
   graph-server service, hostd service, fleet MCP service, NATS provisioning
   helpers, and JSON install output.
8. `pyproject.toml` already exposes hostd/controller entry points:
   `code-index-openclaw-hostd`,
   `code-index-openclaw-context-probe`,
   `code-index-openclaw-controller`, and
   `code-index-openclaw-fleet-controller`.
9. NATS docs already define the required subjects, host ACL template, durable
   push consumer shape, bounded `_INBOX.<host_id>.>` reply posture, and the
   `openclaw_agent_states` TTL requirement.

## Required Behavior

1. Add host-alias config support to hostd:
   - JSON config key: `host_aliases`, accepting a string or list of strings.
   - Environment override: `OPENCLAW_HOSTD_HOST_ALIASES`.
   - Normalize aliases to lowercase, trim whitespace, drop blanks, dedupe while
     preserving order, and reject aliases containing whitespace, `@`, path
     separators, or NATS subject wildcards.
   - Keep aliases as routing labels only. They are not host IDs,
     authorization keys, Telegram identities, Windows usernames, or NATS
     credential names.
2. Include aliases in every heartbeat payload:
   - Add top-level `host_aliases`.
   - Include the same alias list in `openclaw.host.<host_id>.capabilities`
     payloads if useful for controller or operator consumers.
   - Preserve existing redaction behavior and do not leak graph tokens, NATS
     credentials, enrollment codes, or Windows account names.
3. Ensure `lenny` and `rosie` are first-class deployment aliases:
   - The Windows installer must accept `-HostAlias lenny` or
     `-HostAlias rosie`; also allow `-HostDisplayName`/`-SshHostname` to remain
     separate from alias.
   - The generated hostd config for Lenny includes
     `"host_aliases": ["lenny"]`.
   - The generated hostd config for Rosie includes
     `"host_aliases": ["rosie"]`.
   - Do not hard-code either alias as a hidden default that would make every
     install claim to be Lenny or Rosie. Defaults may be examples only.
4. Implement a Windows service installer/runbook path:
   - Prefer a PowerShell installer at
     `scripts/install_openclaw_m1_windows.ps1` unless repo style strongly
     favors Python for this target.
   - Install path should be deterministic and Windows-native, for example:
     `C:\ProgramData\OpenClaw\memory-claude-openclaw-m1\state`,
     `C:\ProgramData\OpenClaw\memory-claude-openclaw-m1\config`, and
     `C:\ProgramData\OpenClaw\memory-claude-openclaw-m1\logs`.
   - The repo root and `.venv` path must be caller-provided or inferred from
     `--repo`; do not assume a developer-specific drive letter.
   - Register services for graph-server, hostd, and fleet MCP. Use Windows
     service names consistent with the existing basename
     `ai.openclaw.memory-claude-m1`, adapted to Windows service-name
     constraints.
   - Include install, no-start/dry-run, start, stop, status, and uninstall
     guidance in docs. The implementation can expose these as switches or as
     documented `sc.exe`/PowerShell commands.
   - Make the service commands use the existing entry points and flags:
     hostd runs `code-index-openclaw-hostd --config <config> --json
     --probe-graph-server --probe-context`; graph-server binds to
     `127.0.0.1`; fleet MCP binds to `127.0.0.1`.
5. Define a safe Windows config/env/secret layout:
   - Non-secret config may live in JSON under ProgramData.
   - Secrets must not be written into examples, tests, committed config, or
     docs. Use placeholders such as `<NATS_URL>`, `<CONTROLLER_TOKEN>`, and
     `<ENROLLMENT_CODE>`.
   - Prefer Windows Credential Manager or DPAPI-protected material for NATS
     credentials/controller tokens. If the first implementation must pass a
     NATS URL into hostd, keep it operator-supplied at install time and ensure
     tests/docs use fake placeholders only.
   - Do not log NATS URLs with credentials, controller tokens, enrollment
     codes, NKey seeds, or generated credential material.
6. Cover controller connectivity:
   - Document the Fleet Controller heartbeat ingest route
     `POST /fleet/hosts/heartbeat` and host-scoped principal expectation.
   - If hostd currently only publishes heartbeat to NATS, document the expected
     controller-side bridge/ingest path rather than inventing a second
     authorization model.
   - Keep `host_id` as the authorization namespace for subjects such as
     `openclaw.task.<host_id>.assigned`.
7. Cover NATS connectivity:
   - Reuse the existing subject model:
     `openclaw.host.<host_id>.heartbeat`,
     `openclaw.host.<host_id>.capabilities`,
     `openclaw.deliver.<host_id>.tasks`,
     `openclaw.host.<host_id>.inbox`,
     `openclaw.task.<host_id>.ack`, and
     `openclaw_agent_states`.
   - The Windows runbook must state that hosts do not create streams,
     consumers, or KV buckets. Deployment/admin automation creates those.
   - The Windows runbook must state that host credentials must not be able to
     subscribe to another host's delivery subject.
8. Add enrollment/runbook docs:
   - Create a Windows enrollment doc that describes operator-created pending
     enrollment, one-time enrollment code, local host identity, local NKey
     generation preference, credential storage, alias selection, and canary
     verification without embedding real credentials.
   - Create a Windows host runbook for installing Lenny and Rosie, verifying
     heartbeat/capability publication, verifying alias projection through Fleet
     Controller, verifying canary task delivery, rotating credentials, and
     uninstalling services.
   - Update existing host identity/NATS docs only where needed to point to the
     Windows-specific runbook.
9. Tests must drive the behavior:
   - Add hostd config tests for JSON and env alias parsing, normalization, and
     invalid alias rejection.
   - Add heartbeat tests proving aliases appear in heartbeat/capability
     payloads and secrets remain redacted.
   - Add Windows installer tests that validate generated paths, service command
     arguments, alias config for `lenny` and `rosie`, no real credentials in
     sample output, and dry-run/no-start behavior without requiring Windows or
     admin privileges in CI.
   - Add or extend controller tests proving a heartbeat with
     `host_aliases=["lenny"]` makes `@lenny` assignment resolve to that stable
     host ID.
   - Keep existing systemd/launchd installer tests passing.

## Acceptance Criteria

- A generated Lenny hostd config contains a stable identity path, repo root,
  graph URL, fleet lease store path, context store path, heartbeat interval,
  NATS placeholder/operator-supplied value, and `host_aliases: ["lenny"]`.
- A generated Rosie hostd config does the same with
  `host_aliases: ["rosie"]`.
- `code-index-openclaw-hostd --config <generated-config> --once --json`
  emits a heartbeat with `host_aliases` and does not leak secrets in stdout.
- `publish_host_snapshot` publishes heartbeat/capability subjects scoped to
  the stable `host_id`, not the alias.
- The Fleet Controller can resolve `lenny` and `rosie` aliases from heartbeat
  inventory and still rejects unknown or ambiguous aliases.
- Windows service install tests validate graph-server, hostd, and fleet MCP
  service command lines without touching the real host service manager.
- Docs include separate enrollment and runbook steps for Lenny and Rosie,
  including canary heartbeat, capabilities, task delivery, alias projection,
  and cross-host NATS negative checks.
- No committed file contains real NATS credentials, controller tokens,
  enrollment codes, NKey seeds, private IP credentials, or machine-specific
  absolute repo paths as required defaults.
- Existing Telegram host-claim and Fleet Controller assignment behavior remains
  compatible.

## Verification

Run:

```bash
python3 -m pytest tests/openclaw_hostd/test_heartbeat.py -q
python3 -m pytest tests/openclaw_hostd/test_identity.py -q
python3 -m pytest tests/openclaw_hostd/test_service.py -q
python3 -m pytest tests/openclaw_hostd/test_systemd_installer.py -q
python3 -m pytest tests/openclaw_hostd/test_windows_installer.py -q
python3 -m pytest tests/openclaw_controller/test_scheduler.py -q
python3 -m pytest tests/openclaw_controller/test_api.py -q
python3 -m pytest tests -q
```

If the environment uses `python` instead of `python3`, use the repo-local
equivalent command and report that substitution in the final Sandcastle output.

## Final Instructions

Preserve any unrelated existing worktree changes. Do not revert or overwrite
edits outside the owned paths.

Commit the completed work on the Sandcastle branch and output
`<promise>COMPLETE</promise>` when done.
