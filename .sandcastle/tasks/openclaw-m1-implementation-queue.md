# OpenClaw M1 Sandcastle Implementation Queue

## Purpose

This queue delegates the current OpenClaw M1 deployment/control-plane gaps to
branch-isolated Sandcastle implementation runs.

The task briefs were produced after reviewing the current setup, control
system, Lenny/Rosie deployment posture, Telegram unification path, fumemory
durability, and Railway service requirements.

## Launch Preconditions

Do not launch these implementation runs from a dirty baseline.

The current Sandcastle launcher creates branch worktrees from `HEAD`; it does
not copy uncommitted or untracked OpenClaw files from the working tree into the
implementation branches. Before running the queue, make sure the OpenClaw M1
baseline and `.sandcastle/tasks/*.md` task files are committed or otherwise
present in the branch that Sandcastle will use as `HEAD`.

Verify:

```powershell
git status --short
```

If the status is dirty, either checkpoint the intended baseline on a local
branch or finish/merge the pending work first. Do not stash or discard unrelated
user work just to launch this queue.

## Tasks

### 1. Windows PC Deployment

```powershell
.\scripts\sandcastle.ps1 -Mode implement -Agent codex -TaskFile .sandcastle\tasks\openclaw-m1-lenny-rosie-windows-deployment.md -Branch agent/openclaw-m1-lenny-rosie-windows-deployment -MaxIterations 5
```

Primary outcome: Lenny/Rosie Windows hostd install path, service/runbook
coverage, host alias config, alias heartbeat publication, and deployment tests.

### 2. fumemory And Railway Services

```powershell
.\scripts\sandcastle.ps1 -Mode implement -Agent codex -TaskFile .sandcastle\tasks\openclaw-m1-fumemory-railway-services.md -Branch agent/openclaw-m1-fumemory-railway-services -MaxIterations 5
```

Primary outcome: long-running controller/fleet/messaging service shape,
Railway volume-backed configuration, health/readiness checks, persistent
fumemory/Completed Work behavior, Railway runbook, and service tests.

### 3. Unified Telegram Controller

```powershell
.\scripts\sandcastle.ps1 -Mode implement -Agent codex -TaskFile .sandcastle\tasks\openclaw-m1-unified-telegram-controller.md -Branch agent/openclaw-m1-unified-telegram-controller -MaxIterations 5
```

Primary outcome: one canonical Telegram Messaging Service and Fleet Controller
path across Lenny/Rosie, alias routing, untagged claimable work, controller
NATS consumers, persisted ACK/claim state, idempotent update handling, and
integration tests.

## Merge Guidance

Review each Sandcastle branch separately. Expect possible conflicts around
`code_index/openclaw_controller/app.py`, `code_index/openclaw_hostd/service.py`,
and shared OpenClaw docs. Merge the smallest deployment/service foundation
branches before merging the broader Telegram controller branch.

Recommended review order:

1. `agent/openclaw-m1-lenny-rosie-windows-deployment`
2. `agent/openclaw-m1-fumemory-railway-services`
3. `agent/openclaw-m1-unified-telegram-controller`

Each branch should contain its own commit, verification output, and final
`<promise>COMPLETE</promise>` in the Sandcastle log.
