# OpenClaw Lease Model

Milestone 1 uses two different coordination layers.

Fleet leases are central and cover only host, repo, and task resources. They
prevent duplicate cross-host assignment and recovery actions. File-level claims
remain local to each host's graph-server SQLite database and are not mirrored
through NATS in Milestone 1.

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
denied and no local run should be dispatched.

## Fencing

Every successful lease mutation returns a monotonic `fencing_revision`. Release,
renewal, revocation, and overwrite operations must use the current revision. A
stale lower revision cannot release, renew, or replace a newer lease.

The current host daemon implementation stores the task lease revision in
`openclaw_task_inbox` when it accepts a task. That gives terminal-status cleanup
the exact token needed to release the central task lease later.

## Renewal And Release

Lease renewal validates both:

- the owning host, and
- the current `fencing_revision`.

Terminal local run statuses release task leases through
`release_task_lease_on_terminal_status`. Terminal statuses include `completed`,
`failed`, `cancelled`, `canceled`, `review`, `needs_review`, `needs-review`, and
`done`.

Non-terminal statuses do not release fleet leases.

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

## File Claims Stay Local

File claims continue to use the existing graph-server SQLite tables and fencing
tokens. They are intentionally not promoted to cross-host NATS leases in
Milestone 1.

This keeps first-milestone coordination coarse enough to avoid duplicate
cross-host task execution without turning local file edits into a distributed
lock service.
