# OpenClaw CMA SSH Recovery

## Overview

The SSH recovery feature provides a narrow, auditable automation path for
recovering stale Windows hosts in the OpenClaw fleet.  It is **not** a
general-purpose remote shell.  Only four command kinds are allowed, and the
Fleet Controller enforces three pre-conditions before authorizing any command.

SSH access itself remains admin/break-glass only, restricted to the private
Tailscale overlay, and requires key-based authentication (no passwords).

---

## Allowed Command Kinds (Allowlist)

| Command Kind       | Purpose                                                   |
|--------------------|-----------------------------------------------------------|
| `health-check`     | Read-only diagnostics: service status, connectivity.      |
| `process-check`    | List or inspect running OpenClaw processes.               |
| `service-restart`  | Restart the OpenClaw host daemon (safe, idempotent).      |
| `index-update`     | Re-run `code_index update` for the registered repo root.  |

Any command kind not in this list is rejected immediately with reason
`unknown_command_kind`.

---

## Authorization Pre-conditions

The Fleet Controller checks all three conditions before authorizing:

1. **Target host is stale** - `health_at(now)` must not be `healthy`.  A host
   that is still sending heartbeats cannot be recovered via SSH automation.
   Rejection reason: `host_not_stale`.

2. **No active fleet leases** - The target host must hold no active `repo` or
   `task` leases at the time of the request.  An active lease means the host is
   still executing work, so SSH intervention could corrupt state.
   Rejection reason: `active_leases`.

3. **No active local file claims** - The target host must have no active local
   file claims in the graph-server SQLite store.  File claims stay local and are
   not replicated to NATS; the Fleet Controller checks them through the injected
   `claims_store` interface.  If no `claims_store` is configured, this check is
   skipped.
   Rejection reason: `active_file_claims`.

All three conditions must pass simultaneously.

---

## Windows Operations Guidance

### Prerequisites

- OpenSSH is enabled on the target host (optional feature; configured during
  enrollment if the deployment includes SSH recovery).
- The SSH admin endpoint is stored in the host record as `ssh_admin_endpoint`
  (private Tailscale name or IP, never a public address).
- Key-based auth only: the operator's public key is installed in the
  `OpenClaw` service-account profile, **not** in the local admin account.
- The Tailscale overlay must be active on both the operator machine and the
  target host before connecting.

### Operator Workflow

1. Verify the host is stale in the Fleet Controller:
   ```
   GET /fleet/hosts  ->  find host with health = "stale"
   ```

2. Confirm the host has no active leases and no active file claims (the Fleet
   Controller checks these, but verify visually before invoking recovery).

3. Request SSH recovery authorization from the Fleet Controller (via the fleet
   MCP tool or the controller HTTP API):
   ```python
   result = ssh_recovery_policy.authorize_recovery(
       "health-check",
       "oclh_01hz...",
       now=datetime.now(timezone.utc),
   )
   # result.status must be "authorized" before proceeding
   ```

4. If authorized, connect over the private overlay and run **only** the
   authorized command kind:

   | Command Kind       | Windows Example                                                    |
   |--------------------|--------------------------------------------------------------------|
   | `health-check`     | `sc query OpenClawHostDaemon`                                      |
   | `process-check`    | `tasklist /fi "imagename eq python.exe"`                           |
   | `service-restart`  | `Restart-Service -Name OpenClawHostDaemon -Force`                  |
   | `index-update`     | `python -m code_index update --all` (from repo root)              |

5. Record the outcome in the fleet audit log.  The Fleet Controller records
   each authorization attempt automatically (authorized or rejected).

### Security Notes

- **Never** run arbitrary PowerShell or CMD commands through SSH recovery.
  Only execute the exact command corresponding to the authorized `command_kind`.
- SSH recovery does **not** bypass Windows secret storage or credential
  rotation.  Do not access `OpenClaw/<host_id>/nats-creds` or
  `OpenClaw/<host_id>/controller-token` during a recovery session.
- `service-restart` stops and starts the host daemon.  Any in-progress agent
  context is abandoned.  The agent will need to re-acquire its context manifest
  when it resumes.
- `index-update` may take several minutes on large repos.  Do not interrupt it.
- Do not keep the SSH session open after the authorized command completes.

### Audit Trail

Every call to `SshRecoveryPolicy.authorize_recovery()` appends an entry to the
in-memory audit log (accessible via `list_audit_log()`).  Each entry includes:

- `attempt_id` - deterministic `sshrec_<hex>` identifier.
- `command_kind` - the requested command kind.
- `target_host_id` - the fleet host identifier.
- `status` - `"authorized"` or `"rejected"`.
- `rejection_reason` / `rejection_message` - populated on rejection.
- `authorized_at` - ISO-8601 timestamp if authorized; `null` if rejected.
- `recorded_at` - ISO-8601 timestamp of the authorization attempt.

Production deployments should persist the audit log to a durable fleet event
store (NATS JetStream `AUDIT` stream or equivalent).

---

## M2 Scope Notes

- SSH connectivity validation (ping, port check) is **out of scope** for M2.
- Automated rollback on `service-restart` failure is **out of scope** for M2.
- CMA invocation of SSH recovery (LLM-driven) is **out of scope** for M2;
  operators trigger recovery manually or via the fleet MCP tool.
- File-claim persistence to NATS is **out of scope** for M2; claims remain
  local SQLite only.
