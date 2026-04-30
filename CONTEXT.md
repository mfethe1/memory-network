# Graph Agent Companion

Graph Agent Companion is a local control plane for coding agents that combines codebase indexing, graph context, agent task supervision, coordination leases, retrieval, and operational visibility.

## Language

**Graph Agent Companion**:
The product context for local agent supervision over an indexed codebase.
_Avoid_: Code Index, graph-server, control plane when referring to the whole product context

**Agent Task**:
A requested unit of coding work, including the prompt and the context needed to understand what should be done.
_Avoid_: Run, session

**Agent Run**:
A coding session in which a specific agent works on an **Agent Task**.
_Avoid_: Task, prompt

**Run Orchestrator**:
A deterministic supervisor that classifies and manages **Agent Run** lifecycle state.
_Avoid_: Orchestrator agent, coding agent

**Agent Swarm**:
A coordinated team of **Agent Runs** working together as the execution strategy for one **Agent Task**.
_Avoid_: Provider, model, single run

**Swarm Lead**:
An **Agent Run** in an **Agent Swarm** responsible for decomposition, coordination, review, and synthesis.
_Avoid_: Run Orchestrator

**Agent Run Status**:
The durable lifecycle state recorded for an **Agent Run**.
_Avoid_: Health, freshness, process state

**Run Health**:
A derived operational classification of an **Agent Run** based on recent events, process liveness, claims, and review needs.
_Avoid_: Status

**Verification State**:
The outcome of checks requested for an **Agent Run**.
_Avoid_: Run status

## Relationships

- **Graph Agent Companion** uses codebase indexing, graph context, retrieval, leases, and run transcripts to supervise coding agents.
- An **Agent Task** can have one or more **Agent Runs**.
- An **Agent Run** belongs to exactly one **Agent Task**.
- An **Agent Task** may use an **Agent Swarm** as its execution strategy.
- An **Agent Swarm** coordinates multiple **Agent Runs** for the same **Agent Task**.
- An **Agent Swarm** may have one **Swarm Lead** and multiple worker **Agent Runs**.
- The **Swarm Lead** handles task decomposition, worker review, follow-up requests, and synthesis.
- The **Run Orchestrator** watches **Agent Runs** and may close, fail, restart, retry, or escalate them.
- Coding agents perform implementation work; the **Run Orchestrator** manages run lifecycle.
- The **Run Orchestrator** manages deterministic lifecycle and safety for **Agent Swarms** and their **Agent Runs**.
- An **Agent Run** has exactly one durable **Agent Run Status**.
- An **Agent Run** can have one derived **Run Health** at a point in time.
- Review is a durable **Agent Run Status** for a stopped run that needs human or policy acceptance before it can become completed.
- The **Run Orchestrator** derives **Run Health** from last event time, process liveness, terminal events, active claims, changed files, final messages, blockers, and verification state.
- The **Run Orchestrator** may automatically write a terminal **Agent Run Status** only when process liveness is known false and no active claims remain.
- **Verification State** can be passed, failed, blocked, or not run.
- Blocked **Verification State** means checks could not start or complete because of an environment/runtime problem, not because the code failed the checks.
- An **Agent Run** with completed code changes and blocked **Verification State** should move to review rather than completed.
- A retry that needs another coding session should create a new **Agent Run** for the same **Agent Task**.
- The **Run Orchestrator** may automatically derive **Run Health**, move an inactive run to review, mark pre-edit launch failures as failed, release expired claims, and run configured verification-only checks.
- The **Run Orchestrator** requires human confirmation to revert code, accept review as completed, restart coding after changes exist, force-cancel a live process, or override conflicting claims.
- The **Run Orchestrator** should have one shared core that can be invoked by both CLI commands and graph-server.

## Example dialogue

> **Dev:** "Is this a graph-server issue or a **Graph Agent Companion** issue?"
> **Domain expert:** "The browser route belongs to graph-server, but the user-facing supervision workflow belongs to **Graph Agent Companion**."
>
> **Dev:** "The agent failed to launch; should I edit the **Agent Task**?"
> **Domain expert:** "No, the **Agent Task** is still the work request. Start or inspect the **Agent Run**, because that is the coding session that failed."
>
> **Dev:** "Should the orchestrator agent fix the code after a run stalls?"
> **Domain expert:** "No. The **Run Orchestrator** decides whether the **Agent Run** is stale, orphaned, failed, or ready to retry; a coding agent does the implementation work."
>
> **Dev:** "Is Kimi K2.6 the swarm?"
> **Domain expert:** "No. Kimi K2.6 is a provider/model choice for one or more **Agent Runs**. The **Agent Swarm** is the coordinated team working on the **Agent Task**."
>
> **Dev:** "Does the **Run Orchestrator** decide how to split the work?"
> **Domain expert:** "No. The **Swarm Lead** can plan and synthesize work, while the **Run Orchestrator** creates, monitors, gates, and records the runs safely."
>
> **Dev:** "Should we write stale into the run status?"
> **Domain expert:** "No. The **Agent Run Status** may still be working, while **Run Health** can be stale or orphaned based on current evidence."
>
> **Dev:** "Can the **Run Orchestrator** complete a quiet run?"
> **Domain expert:** "Only if it knows the process is gone and no active claims remain; otherwise it should report stale **Run Health** and wait for policy or human confirmation."
>
> **Dev:** "PowerShell failed with `CreateProcessAsUserW failed: 5`; did verification fail?"
> **Domain expert:** "No. The **Verification State** is blocked because the runner could not start. Failed means the checks ran and found a problem."
>
> **Dev:** "Should we restart the same **Agent Run** after blocked verification?"
> **Domain expert:** "No. Keep that run's transcript intact and move it to review. If more coding work is needed, start a new **Agent Run** for the same **Agent Task**."
>
> **Dev:** "Is review just health?"
> **Domain expert:** "No. Review is an **Agent Run Status** because it is a durable handoff state between active work and accepted completion."
>
> **Dev:** "Can the **Run Orchestrator** mark reviewed work completed?"
> **Domain expert:** "Not automatically. Completing review accepts the work, so it requires human confirmation or an explicit acceptance policy."
>
> **Dev:** "Should graph-server own run orchestration?"
> **Domain expert:** "No. graph-server can invoke the **Run Orchestrator**, but the orchestration rules belong in a shared core that the CLI can also run."

## Flagged ambiguities

- "Code Index", "graph-server", and "control plane" were used as possible names for the whole context. Resolved: **Graph Agent Companion** is the context name; Code Index and graph-server are narrower system surfaces.
- "task" and "run" were used interchangeably. Resolved: **Agent Task** is the requested work; **Agent Run** is the coding session attempting that work.
- "orchestrator agent" was used for lifecycle supervision. Resolved: use **Run Orchestrator** because this role is deterministic supervision, not coding work.
- "stale" and "orphaned" were treated like statuses. Resolved: durable **Agent Run Status** is separate from derived **Run Health**.
- "swarm" was introduced alongside a specific model. Resolved: **Agent Swarm** is the coordinated team strategy for an **Agent Task**; provider/model selection is separate.
- Swarm coordination and run lifecycle supervision were conflated. Resolved: **Swarm Lead** coordinates work content; **Run Orchestrator** manages deterministic lifecycle and safety.
