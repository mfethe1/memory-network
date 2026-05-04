# OpenClaw Host Identity

An OpenClaw host is a Windows PC that runs the future host daemon and connects
outbound to the fleet broker and controller. Host identity is stable across
reboots and hostname changes, but it is not a human identity and it is not an
Agent Run identity.

## Identity Fields

The controller stores one host record per enrolled PC:

```text
host_id                     Stable controller-assigned ID, for example oclh_01hz...
display_name                Human label, not used for authorization
environment                 dev, staging, or prod
owner                       Person or team responsible for the PC
machine_fingerprint_hash    Salted hash of local machine identifiers
install_nonce_hash          Hash of the OpenClaw install nonce
os_family                   windows
os_version                  Windows version/build
arch                        CPU architecture
private_network_name        Tailscale or private DNS name
private_network_addresses   Private overlay IPs
ssh_admin_endpoint          Private-only OpenSSH endpoint, if enabled
capabilities                Providers, repos, GPU/CPU/RAM, tool versions
graph_agent_companion_root  Local Graph Agent Companion root path
nats_account                Environment account used by this host credential
nats_nkey_public            Public NKey bound to this host
credential_id               Controller credential record ID
credential_issued_at        Issue time
credential_expires_at       Expiry or rotation deadline
credential_rotation_epoch   Monotonic rotation counter
secret_storage              Credential Manager or DPAPI target names only
created_at                  Enrollment time
last_seen_at                Last heartbeat time
status                      pending, active, disabled, or rotated
```

Never use hostname, Windows username, MAC address, or IP address as the
authorization key. Those values can change or collide. `host_id` is the
authorization namespace used in NATS subjects such as
`openclaw.task.<host_id>.assigned`.

## Enrollment Flow

1. An operator creates a pending host enrollment in the controller with
   `display_name`, `environment`, `owner`, and expected private-network scope.
2. The controller returns a one-time enrollment code with a short TTL. The code
   is single use and is not a reusable API token.
3. The Windows installer starts under the dedicated OpenClaw service account,
   collects local identity fields, and creates an install nonce.
4. Preferred credential path: the host generates its NKey seed locally and
   sends only the public NKey to the controller over authenticated HTTPS.
5. The controller assigns `host_id`, signs or provisions the NATS credential
   for that public NKey, and binds subject ACLs to the literal `host_id`.
6. The host stores the NATS credential and controller token using Windows
   secret storage, then deletes any transient enrollment material.
7. The host connects to NATS and publishes
   `openclaw.host.<host_id>.heartbeat` and
   `openclaw.host.<host_id>.capabilities`.
8. The controller publishes a canary Agent Task assignment to
   `openclaw.task.<host_id>.assigned`; the host receives it through the
   deployment-created `openclaw.deliver.<host_id>.tasks` push-consumer subject
   and publishes `openclaw.task.<host_id>.ack`.

If host-side NKey generation is not available in the first installer, the
controller may generate the NKey seed and deliver it once over authenticated
HTTPS. That fallback material must never be logged by the installer,
controller, reverse proxy, telemetry pipeline, or crash-reporting path. If the
deployment cannot prove those paths redact or skip NKey seeds and credentials,
host-side generation is required.

## Credential Rotation

Rotation creates a second NKey for the same `host_id`, installs the new
credential, verifies heartbeat and canary task flow, then revokes the old NKey.
Disabling a host revokes its NKey and controller token, but it must not delete
local worktrees, transcripts, `.code_index`, or Graph Agent Companion data.

## Windows Secret Storage

Store host secrets with Windows Credential Manager or DPAPI:

```text
OpenClaw/<host_id>/nats-creds
OpenClaw/<host_id>/controller-token
OpenClaw/<host_id>/install-nonce
```

Required posture:

- Do not store NKey seeds, `.creds` files, controller tokens, or enrollment
  codes in plaintext config files.
- Prefer Credential Manager entries owned by the dedicated OpenClaw service
  account.
- If a file must exist for a NATS client library, protect its contents with
  DPAPI and restrict the file ACL to the service account, `SYSTEM`, and local
  Administrators.
- Do not pass secrets through environment variables for long-running services.
- Configure installer, controller, proxy, telemetry, and crash reporting to
  treat NKey seeds, JWTs, controller tokens, and enrollment codes as
  never-capture fields rather than log-and-redact fields.

## SSH Posture

Windows OpenSSH is allowed only for admin and break-glass operations over a
private network such as Tailscale:

- No public inbound SSH exposure.
- Firewall allows SSH only from approved admin or recovery nodes on the private
  overlay.
- Key-only authentication; password authentication disabled.
- Admin users use named personal keys. Shared break-glass keys require explicit
  rotation after use.
- SSH is not the Agent Task, Agent Run, event, or messaging transport.
- Recovery automation must be narrow and command allowlisted before it can be
  used by the OpenClaw Context Manager Agent.

The normal control path remains outbound NATS plus HTTPS. SSH exists to repair
or inspect a host when that path is unavailable.

## Verification

Enrollment is acceptable when:

1. A new host receives credentials scoped to its literal `host_id`.
2. The host can publish its heartbeat and capabilities.
3. The host can receive its own canary task on
   `openclaw.deliver.<host_id>.tasks`.
4. The same credential cannot read
   `openclaw.task.<other_host_id>.assigned` or
   `openclaw.deliver.<other_host_id>.tasks`.
5. The same credential cannot publish controller config or broker admin
   subjects.
