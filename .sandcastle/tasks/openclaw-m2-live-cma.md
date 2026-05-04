# OpenClaw M2 Slice: Live CMA Invocation

## Goal

Implement the Milestone 2 live Context Manager Agent path on top of the passive Slice 7A context modules.

## Scope

Owned paths:

- `code_index/openclaw_context/**`
- `tests/openclaw_context/**`
- docs only if they directly describe the live CMA behavior

Do not edit Sandcastle files, provider registry files, or Fleet Controller MCP files in this slice.

## Required Behavior

Build a thin, testable live CMA layer that:

1. Selects Kimi K2.6 for routine manifest/threshold/correction work, Claude Opus for quality-gate/dependency review, and GPT-5.5 for goal drift, repeated handoff failures, or cross-host conflicts.
2. Invokes a provider through an injectable runner abstraction. Tests must use fakes; production code may default to provider command presets or a callable runner.
3. Requires structured JSON output with at least `escalate: bool`, `decision_kind`, `summary`, `pointer`, and `confidence`.
4. Enforces budget guardrails from the plan: max five concurrent CMA invocations, 90 second cooldown per `run_id`, 30 second dedup window for identical triggers, max two escalation hops per trigger event.
5. Persists/audits decisions as context health events and injects correction pointers when a quality gate needs active enforcement.
6. Keeps compaction as degraded fallback and does not auto-load long source material.

## Acceptance Criteria

- `evaluate_context_health` or a new adjacent public interface can trigger live CMA decisions without changing passive behavior by default.
- A quality-gate "premature done" signal can produce an enforced correction pointer with `invoked_llm: true`.
- Escalation stops after two hops and records the selected tiers.
- Cooldown/dedup/budget limits return deterministic skipped/degraded records instead of calling the provider.
- Existing passive tests still pass.

## Verification

Run:

```bash
python3 -m pytest tests/openclaw_context -q
python3 -m pytest tests -q
```

Commit the completed work on your Sandcastle branch and output `<promise>COMPLETE</promise>` when done.
