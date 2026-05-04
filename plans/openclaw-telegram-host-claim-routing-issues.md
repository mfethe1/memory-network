# OpenClaw Telegram Host Claim Routing - Issue Breakdown Draft

This draft follows the repo-local issue tracker guidance in `docs/agents`.
Publish with `gh issue create` only after the slice boundaries are approved.

## Proposed Slices

1. **Telegram Host Alias Parsing**
   - Type: AFK
   - Blocked by: None
   - User stories covered: target work with `@rosie` or `@lenny`
   - What to build: Parse host aliases from Telegram messages and commands,
     preserve the original message body, and store the alias as a routing hint
     that is not a host ID or authorization identity.
   - Acceptance criteria:
     - [ ] `@rosie <prompt>` and `@lenny <prompt>` create message metadata with
           the requested host alias.
     - [ ] `/assign <task_id> @rosie <prompt>` and `/task <task_id> @lenny
           <prompt>` preserve task IDs and assignment prompt text.
     - [ ] Unknown mention text is stored as normal chat unless route policy
           explicitly allows it.
     - [ ] Telegram update replay remains idempotent.

2. **Fleet Host Alias Resolution**
   - Type: AFK
   - Blocked by: Telegram Host Alias Parsing
   - User stories covered: route explicit mentions to the intended machine
   - What to build: Resolve `rosie` and `lenny` aliases to stable Fleet
     Controller host IDs from host inventory and reject unknown, stale, or
     ineligible alias targets without silently falling back.
   - Acceptance criteria:
     - [ ] Host heartbeats can expose host aliases without changing host ID
           semantics.
     - [ ] Explicit alias assignment creates or constrains host delivery for
           the resolved host ID.
     - [ ] Unknown alias, stale host, missing repo, and missing provider
           capability each reject with distinct reasons.

3. **Claimable Telegram Work Promotion**
   - Type: AFK
   - Blocked by: None
   - User stories covered: untagged actionable messages can be picked up by a
     capable agent
   - What to build: Promote one stored untagged Telegram chat message into one
     pending signed `assign_task` command reference when a valid host claims it,
     deriving a deterministic task ID when the human did not supply one.
   - Acceptance criteria:
     - [ ] `please check my email` stores one message and can be promoted to
           one command ref.
     - [ ] Repeated claims for the same message do not create duplicate command
           refs.
     - [ ] Replayed Telegram updates do not create duplicate claimable work.
     - [ ] Non-actionable or policy-blocked messages cannot be promoted.

4. **Claimant-Aware Assignment Race Fence**
   - Type: AFK
   - Blocked by: Claimable Telegram Work Promotion
   - User stories covered: Rosie and Lenny do not run the same task
     simultaneously
   - What to build: Add claimant-aware Fleet Controller assignment so the host
     that wins the claim is the host that receives the assignment, with
     command-ref and task-lease conflicts returned deterministically to losing
     claimants.
   - Acceptance criteria:
     - [ ] Rosie and Lenny racing on the same message produce exactly one task
           publish.
     - [ ] A claim from Lenny assigns Lenny when Lenny is eligible, even if
           Rosie sorts first in host inventory.
     - [ ] A claimant outside delivery scope cannot steal work.
     - [ ] Existing explicit `/assign` command replay tests still pass.

5. **Telegram Claim Events And Room Timeline**
   - Type: AFK
   - Blocked by: Claimant-Aware Assignment Race Fence
   - User stories covered: humans can see who claimed untagged work and why a
     losing claimant stood down
   - What to build: Record claim accepted, claim rejected, assignment, and
     lease-conflict outcomes as Messaging Service room events or message
     metadata updates.
   - Acceptance criteria:
     - [ ] Accepted claim appears in the originating room timeline.
     - [ ] Losing claimant result appears without creating a duplicate task.
     - [ ] Assignment rejection reasons are visible from the message history.

## Suggested Parent Issue

Title: `OpenClaw: Rosie/Lenny Telegram host aliases and claimable work`

Labels: `needs-triage`

Body:

```markdown
## What to build

Add a single-Telegram-service routing path where `@rosie` and `@lenny`
explicitly target host aliases, while untagged actionable messages become
claimable work that exactly one eligible host can claim and execute.

## Acceptance criteria

- [ ] Explicit `@rosie` and `@lenny` messages route through Fleet Controller
      host alias resolution.
- [ ] Untagged actionable Telegram messages can be claimed by one eligible host.
- [ ] Rosie and Lenny cannot start simultaneous Agent Runs for the same
      message.
- [ ] Existing command-ref signing, delivery records, and task leases remain
      the authoritative execution fences.

## Blocked by

None - can start immediately.
```
