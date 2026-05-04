"""Passive context routing, avoid-pointer, and quality-gate policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from code_index.openclaw_context.models import ContextPointer
from code_index.openclaw_context.models import HoldDecision
from code_index.openclaw_context.models import QualityGateFlag


@dataclass(frozen=True)
class ContextRetrievalPolicy:
    host_id: str | None = None
    provider: str | None = None
    route_scope: str = "local"


def pointer_visible(pointer: ContextPointer, policy: ContextRetrievalPolicy) -> bool:
    sensitivity = str(pointer.sensitivity or "repo").strip().lower()
    route_scope = str(policy.route_scope or "local").strip().lower()
    same_host = not pointer.host_id or pointer.host_id == policy.host_id
    same_provider = not pointer.provider or pointer.provider == policy.provider

    if route_scope == "external_message":
        return sensitivity == "public"
    if route_scope == "cross_host" and not same_host:
        return sensitivity in {"public", "repo_public"}
    if sensitivity in {"public", "repo", "repo_public"}:
        return True
    if sensitivity == "host_private":
        return same_host and route_scope in {"local", "cross_provider"}
    if sensitivity == "provider_private":
        return same_host and same_provider and route_scope == "local"
    if sensitivity == "external_blocked":
        return same_host and route_scope != "external_message"
    return route_scope == "local" and same_host and same_provider


def hold_assignment_for_avoid_pointers(
    store: Any,
    *,
    task_id: str,
    target_symbols: list[str] | tuple[str, ...],
) -> HoldDecision:
    pointers = tuple(store.list_avoid_pointers(target_symbols))
    if not pointers:
        return HoldDecision(status="allow", task_id=task_id)
    return HoldDecision(
        status="held",
        reason="avoid_pointer",
        pointer_ids=tuple(pointer.pointer_id for pointer in pointers),
        task_id=task_id,
        invoked_context_manager=False,
    )


def detect_quality_gate_flags(agent_state: dict[str, Any]) -> tuple[QualityGateFlag, ...]:
    flags: list[QualityGateFlag] = []
    complexity = str(agent_state.get("task_complexity") or "").strip().lower()
    is_complex = complexity in {"complex", "high"} or bool(agent_state.get("complex_task"))
    test_run_count = _int(agent_state.get("test_run_count") or agent_state.get("tests_run_count"))
    if is_complex and test_run_count <= 0:
        flags.append(
            _flag(
                "zero_test_runs",
                "warning",
                "complex task has no recorded test run",
                {"test_run_count": test_run_count},
            )
        )

    edited = _string_list(agent_state.get("edited_symbols")) or _string_list(
        agent_state.get("edited_files")
    )
    impact_calls = _int(agent_state.get("impact_call_count"))
    if edited and impact_calls <= 0 and not _tool_history_contains(agent_state, "code_index impact"):
        flags.append(
            _flag(
                "missing_impact_before_edit",
                "warning",
                "symbol edit happened before an impact check was recorded",
                {"edited": edited},
            )
        )

    status = str(
        agent_state.get("run_status") or agent_state.get("status") or ""
    ).strip().lower()
    verification = str(agent_state.get("verification_state") or "").strip().lower()
    if status in {"done", "completed", "complete"} and not _has_verification(agent_state, verification):
        flags.append(
            _flag(
                "premature_done_without_verification",
                "warning",
                "run marked done without a verification signal",
                {},
            )
        )

    approaches = _approach_history(agent_state.get("approach_history_json") or agent_state.get("approach_history"))
    repeated = sorted(
        {approach for approach in approaches if approaches.count(approach) > 1}
    )
    if repeated:
        flags.append(
            _flag(
                "repeated_approach",
                "warning",
                "approach history repeats a prior attempt",
                {"approaches": repeated},
            )
        )

    criteria = _string_list(agent_state.get("acceptance_criteria"))
    calls = _string_list(agent_state.get("last_tool_calls"))[-3:]
    if criteria and calls and _looks_drifted(criteria, calls):
        flags.append(
            _flag(
                "goal_drift",
                "warning",
                "recent activity no longer matches task acceptance criteria",
                {"acceptance_criteria": criteria, "last_tool_calls": calls},
            )
        )

    return tuple(flags)


def _flag(
    flag_kind: str,
    severity: str,
    message: str,
    details: dict[str, Any],
) -> QualityGateFlag:
    return QualityGateFlag(
        flag_kind=flag_kind,
        severity=severity,
        message=message,
        passive=True,
        invoked_llm=False,
        details=details,
    )


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            values = parsed
        else:
            values = [value]
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        values = [value]
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _tool_history_contains(agent_state: dict[str, Any], needle: str) -> bool:
    haystack = " ".join(
        _string_list(agent_state.get("tool_history"))
        + _string_list(agent_state.get("last_tool_calls"))
    ).lower()
    return needle.lower() in haystack


def _has_verification(agent_state: dict[str, Any], verification: str) -> bool:
    if verification and verification not in {"none", "not run", "not_run"}:
        return True
    if _int(agent_state.get("test_run_count") or agent_state.get("tests_run_count")) > 0:
        return True
    return bool(agent_state.get("verification_passed"))


def _approach_history(value: Any) -> list[str]:
    return [item.lower() for item in _string_list(value)]


def _looks_drifted(criteria: list[str], calls: list[str]) -> bool:
    criteria_terms = _keywords(" ".join(criteria))
    call_terms = _keywords(" ".join(calls))
    if not criteria_terms:
        return False
    return len(criteria_terms.intersection(call_terms)) == 0


def _keywords(text: str) -> set[str]:
    stop = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    terms: set[str] = set()
    token = ""
    for char in text.lower():
        if char.isalnum() or char == "_":
            token += char
            continue
        if len(token) >= 4 and token not in stop:
            terms.add(token)
        token = ""
    if len(token) >= 4 and token not in stop:
        terms.add(token)
    return terms
