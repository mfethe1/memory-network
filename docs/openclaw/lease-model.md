# OpenClaw Lease Model

Milestone 1 uses two different coordination layers.

Fleet leases are central and cover only host, repo, and task resources. They
prevent duplicate cross-host assignment and recovery actions. File-level claims
remain local to each host's graph-server SQLite database and are not mirrored
through NATS in Milestone 1.

The Slice 5 implementation provides a shared SQLite-backed fleet lease store.
Hosts can point `OPENCLAW_HOSTD_FLEET_LEASE_STORE_PATH` at the same database to
get central conflict enforcement across host daemon processes. If the variable
is not set, hostd uses `fleet-leases.db` under its configured state directory.
This is the file-backed Slice 5 stand-in for the later NATS KV deployment.

## Fleet Lease Scopes

- `host`: exclusive ownership of host-level controller work for one host.
- `repo`: exclusive ownership of repo-level fleet work.
- `task`: exclusive ownership of one Agent Task assignment.

All three scopes use the same record shape:

- `scope`
- `resource_id`
- `owner_host_id`
- `owner_run_id`
- `lease_id`
- `status`
- `fencing_revision`
- `acquired_at`
- `updated_at`
- `expires_at`

Acquisition fails closed. If an active non-expired lease already exists for the
same `(scope, resource_id)` and belongs to another host, a new acquisition is
denied and no local run should be dispatched. Task leases also treat
`owner_run_id` as part of the owner when it is available, so a second daemon for
the same host cannot reuse or release a lease held by a different run.

## Fencing

Every successful lease mutation returns a monotonic `fencing_revision`. Release,
renewal, revocation, and overwrite operations must use the current revision. A
stale lower revision cannot release, renew, or replace a newer lease.

The current host daemon implementation stores the task lease revision in
`openclaw_task_inbox` when it accepts a task and updates that row after every
successful renewal or owner-run binding. That gives terminal-status cleanup the
exact token needed to release the central task lease later.

## Renewal And Release

Lease renewal validates both:

- the owning host, and
- the owning run when `owner_run_id` is present, and
- the current `fencing_revision`.

The daemon loop renews task leases for active graph-server runs before
publishing the heartbeat/agent-state batch. Renewal returns a new
`fencing_revision`, which is immediately persisted back to
`openclaw_task_inbox`.

Terminal local run statuses release task leases through
`release_task_lease_on_terminal_status`. Terminal statuses include `completed`,
`failed`, `cancelled`, `canceled`, `review`, `needs_review`, `needs-review`, and
`done`.

Non-terminal statuses do not release fleet leases. Terminal release is
run-scoped: the graph row must include a non-empty `run_id`, and that id must
match the inbox row and the central task lease owner run. A stale terminal row
for the same task with a missing or different run leaves the active lease and
task state unchanged.

The host daemon creates the configured lease store during NATS runtime setup and
passes it into `TaskInbox`. A task delivery that conflicts with an active task
lease publishes a `lease_conflict` ACK and does not dispatch to graph-server.

The daemon loop also polls graph-server's agent board through the existing
injectable graph client. When a local run is reported with a terminal status,
the matching task lease is released with the stored fencing revision.

## No-Progress Detection

The Fleet Controller reads `openclaw_agent_states`-like entries and active task
leases. The default no-progress threshold is 10 minutes.

For each active task lease:

1. Find the matching agent state by `task_id`, `host_id`, and `run_id`.
2. Skip the task if a terminal status has already been recorded.
3. Parse `last_action_at`.
4. If `now - last_action_at` is greater than or equal to the configured
   threshold, revoke the task lease and mark the task `reassignable`.

A normally completed task is never marked `reassignable`, even if an old agent
state remains visible until KV TTL removes it.

Malformed or incomplete agent-state rows are ignored. The no-progress path does
not fall back to a row that only matches `task_id`; `host_id` and `run_id` must
also match the active lease owner.

Slice 5 exposes this as `FleetLeaseController.run_no_progress_check()`. It reads
agent-state rows from the shared store, revokes stale active task leases, writes
the task state as `reassignable`, and returns both revocation records and the
updated task records. This is intentionally not the full Slice 6 assignment API.

## File Claims Stay Local

File claims continue to use the existing graph-server SQLite tables and fencing
tokens. They are intentionally not promoted to cross-host NATS leases in
Milestone 1.

This keeps first-milestone coordination coarse enough to avoid duplicate
cross-host task execution without turning local file edits into a distributed
lock service.
