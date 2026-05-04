"""Signed passive context manifest builder for Slice 7A."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from code_index.openclaw_context.models import ContextManifest
from code_index.openclaw_context.models import ContextPointer
from code_index.openclaw_context.models import canonical_json


LONG_CONTEXT_SOURCE_KINDS = {
    "soul",
    "global_memory",
    "raw_transcript",
    "project_context",
}


@dataclass(frozen=True)
class ManifestRequest:
    host_id: str
    repo_id: str
    task_id: str
    run_id: str
    provider: str
    target_symbols: tuple[str, ...] = ()
    token_budget: int = 8_000
    required_pointer_ids: tuple[str, ...] = ()
    expires_at: datetime | str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "repo_id": self.repo_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "target_symbols": list(self.target_symbols),
            "token_budget": int(self.token_budget),
            "required_pointer_ids": list(self.required_pointer_ids),
            "expires_at": _datetime_text(self.expires_at),
        }


class CodeIndexContextProbe:
    """Injectable command runner for the five-step manifest pipeline."""

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        runner: Callable[[list[str]], Any] | None = None,
        agent_state_reader: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd())
        self.runner = runner or self._subprocess_run
        self.agent_state_reader = agent_state_reader or (lambda: [])

    def doctor(self) -> dict[str, Any]:
        result = self.runner([sys.executable, "-m", "code_index", "doctor", "--json"])
        return _json_result(result)

    def impact(self, target_symbols: tuple[str, ...]) -> dict[str, Any]:
        return {
            "targets": [
                _json_result(
                    self.runner(
                        [
                            sys.executable,
                            "-m",
                            "code_index",
                            "impact",
                            symbol,
                            "--json",
                        ]
                    )
                )
                for symbol in target_symbols
            ]
        }

    def tests(self, target_symbols: tuple[str, ...]) -> dict[str, Any]:
        return {
            "targets": [
                _json_result(
                    self.runner(
                        [
                            sys.executable,
                            "-m",
                            "code_index",
                            "tests",
                            symbol,
                            "--json",
                        ]
                    )
                )
                for symbol in target_symbols
            ]
        }

    def repo_map(self, *, limit: int = 50) -> str:
        result = self.runner(
            [
                sys.executable,
                "-m",
                "code_index",
                "repo-map",
                "--format",
                "text",
                "--limit",
                str(limit),
            ]
        )
        return str(getattr(result, "stdout", result) or "")

    def agent_states(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.agent_state_reader()]

    def _subprocess_run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.repo_root,
            check=False,
            text=True,
            capture_output=True,
        )


class ContextManifestBuilder:
    def __init__(
        self,
        *,
        store: Any,
        probe: Any | None = None,
        signing_secret: str,
        signature_key_id: str = "local",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.probe = probe or CodeIndexContextProbe()
        self.signing_secret = signing_secret.encode("utf-8")
        self.signature_key_id = signature_key_id
        self.now = now or (lambda: datetime.now(timezone.utc))

    def build_manifest(self, request: ManifestRequest) -> ContextManifest:
        request_hash = _sha(canonical_json(request.to_dict()))
        cached = self.store.get_manifest_by_request_hash(request_hash)
        if cached is not None and cached.status == "signed":
            return cached

        doctor = self.probe.doctor()
        if not _doctor_ok(doctor):
            manifest = self._error_manifest(
                request,
                request_hash=request_hash,
                error_kind="stale_index",
                error_message="code_index doctor reported a stale or unhealthy index",
            )
            return self.store.store_manifest(manifest)

        impact = self.probe.impact(tuple(request.target_symbols))
        tests = self.probe.tests(tuple(request.target_symbols))
        repo_map = self.probe.repo_map(limit=50)
        peer_states = tuple(dict(item) for item in self.probe.agent_states())

        candidates = self._candidate_pointers(
            request,
            impact=impact,
            tests=tests,
            repo_map=repo_map,
        )
        candidate_ids = {pointer.pointer_id for pointer in candidates}
        missing_required = sorted(
            set(request.required_pointer_ids).difference(candidate_ids)
        )
        if missing_required:
            manifest = self._error_manifest(
                request,
                request_hash=request_hash,
                error_kind="missing_required_pointer",
                error_message="required context pointer was not found",
            )
            return self.store.store_manifest(manifest)
        selected, omitted = self._select_pointers(request, candidates)
        required_total = sum(
            pointer.tokens_estimate
            for pointer in selected
            if pointer.pointer_id in request.required_pointer_ids
        )
        if required_total > request.token_budget:
            manifest = self._error_manifest(
                request,
                request_hash=request_hash,
                error_kind="required_budget_exceeded",
                error_message="required context pointers exceed the configured budget",
            )
            return self.store.store_manifest(manifest)

        pointer_ids = tuple(pointer.pointer_id for pointer in selected)
        required_ids = tuple(
            pointer_id
            for pointer_id in request.required_pointer_ids
            if pointer_id in set(pointer_ids)
        )
        source_hashes = {
            pointer.pointer_id: pointer.content_hash for pointer in selected
        }
        estimated_tokens = sum(pointer.tokens_estimate for pointer in selected)
        token_budget = {
            "max_tokens": int(request.token_budget),
            "estimated_tokens": estimated_tokens,
            "truncated": bool(omitted),
        }
        payload = {
            "schema_version": 1,
            "status": "signed",
            "host_id": request.host_id,
            "repo_id": request.repo_id,
            "task_id": request.task_id,
            "run_id": request.run_id,
            "provider": request.provider,
            "pointer_ids": list(pointer_ids),
            "required_pointer_ids": list(required_ids),
            "load_order": list(pointer_ids),
            "omitted_context": omitted,
            "token_budget": token_budget,
            "source_hashes": source_hashes,
            "peer_agent_states": [dict(item) for item in peer_states],
            "expires_at": _datetime_text(request.expires_at),
            "request_hash": request_hash,
        }
        signed_payload = canonical_json(payload)
        signature = hmac.new(
            self.signing_secret,
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        manifest = ContextManifest(
            manifest_id=f"manifest_{request_hash[:24]}",
            request_hash=request_hash,
            status="signed",
            host_id=request.host_id,
            repo_id=request.repo_id,
            task_id=request.task_id,
            run_id=request.run_id,
            provider=request.provider,
            pointer_ids=pointer_ids,
            required_pointer_ids=required_ids,
            load_order=pointer_ids,
            omitted_context=tuple(dict(item) for item in omitted),
            token_budget=token_budget,
            estimated_tokens=estimated_tokens,
            source_hashes=source_hashes,
            peer_agent_states=peer_states,
            expires_at=_datetime_text(request.expires_at),
            signature_key_id=self.signature_key_id,
            signature=signature,
            signed_payload=signed_payload,
            created_at=_datetime_text(self.now()),
        )
        return self.store.store_manifest(manifest)

    def _candidate_pointers(
        self,
        request: ManifestRequest,
        *,
        impact: dict[str, Any],
        tests: dict[str, Any],
        repo_map: str,
    ) -> list[ContextPointer]:
        candidates = self.store.list_context_pointers(
            target_symbols=tuple(request.target_symbols)
        )
        required = [
            pointer
            for pointer_id in request.required_pointer_ids
            for pointer in [self.store.get_context_pointer(pointer_id)]
            if pointer is not None
        ]
        generated = [
            self.store.upsert_context_pointer(
                source_uri=f"code_index://impact/{request.repo_id}/{request.task_id}",
                source_kind="context_packet",
                pointer_kind="impact",
                content_hash=_sha(canonical_json(impact)),
                locator={
                    "step": "impact",
                    "target_symbols": list(request.target_symbols),
                },
                summary="code_index impact candidates",
                tokens_estimate=_estimate_tokens(impact),
                sensitivity="repo",
                host_id=request.host_id,
                repo_id=request.repo_id,
                provider=request.provider,
                target_symbols=list(request.target_symbols),
            ),
            self.store.upsert_context_pointer(
                source_uri=f"code_index://tests/{request.repo_id}/{request.task_id}",
                source_kind="context_packet",
                pointer_kind="verification",
                content_hash=_sha(canonical_json(tests)),
                locator={
                    "step": "tests",
                    "target_symbols": list(request.target_symbols),
                },
                summary="code_index affected-tests verification pointers",
                tokens_estimate=_estimate_tokens(tests),
                sensitivity="repo",
                host_id=request.host_id,
                repo_id=request.repo_id,
                provider=request.provider,
                target_symbols=list(request.target_symbols),
            ),
            self.store.upsert_context_pointer(
                source_uri=f"code_index://repo-map/{request.repo_id}",
                source_kind="graph_context",
                pointer_kind="orientation",
                content_hash=_sha(repo_map),
                locator={"step": "repo-map", "limit": 50},
                summary="compact repo orientation block",
                tokens_estimate=_estimate_tokens(repo_map),
                sensitivity="repo",
                host_id=request.host_id,
                repo_id=request.repo_id,
                provider=request.provider,
                target_symbols=list(request.target_symbols),
            ),
        ]
        return _dedupe(required + candidates + generated)

    def _select_pointers(
        self,
        request: ManifestRequest,
        candidates: list[ContextPointer],
    ) -> tuple[list[ContextPointer], list[dict[str, Any]]]:
        required_ids = set(request.required_pointer_ids)
        selected: list[ContextPointer] = []
        omitted: list[dict[str, Any]] = []
        used_tokens = 0

        def priority(pointer: ContextPointer) -> tuple[int, int, str]:
            if pointer.pointer_id in required_ids or pointer.required:
                return (0, pointer.tokens_estimate, pointer.pointer_id)
            if pointer.pointer_kind in {"decision", "avoid", "verification"}:
                return (1, pointer.tokens_estimate, pointer.pointer_id)
            if pointer.pointer_kind in {"impact", "orientation"}:
                return (2, pointer.tokens_estimate, pointer.pointer_id)
            return (3, pointer.tokens_estimate, pointer.pointer_id)

        for pointer in sorted(candidates, key=priority):
            if _dead_reference(pointer) and pointer.pointer_id not in required_ids:
                omitted.append(
                    {
                        "pointer_id": pointer.pointer_id,
                        "reason": "dead_reference",
                    }
                )
                continue
            if _auto_load_blocked(pointer) and pointer.pointer_id not in required_ids:
                omitted.append(
                    {
                        "pointer_id": pointer.pointer_id,
                        "reason": "auto_load_blocked",
                        "source_kind": pointer.source_kind,
                    }
                )
                continue
            next_tokens = used_tokens + pointer.tokens_estimate
            if pointer.pointer_id in required_ids:
                selected.append(pointer)
                used_tokens = next_tokens
                continue
            if next_tokens <= request.token_budget:
                selected.append(pointer)
                used_tokens = next_tokens
            else:
                omitted.append(
                    {
                        "pointer_id": pointer.pointer_id,
                        "reason": "budget_exceeded",
                        "tokens_estimate": pointer.tokens_estimate,
                    }
                )
        return selected, omitted

    def _error_manifest(
        self,
        request: ManifestRequest,
        *,
        request_hash: str,
        error_kind: str,
        error_message: str,
    ) -> ContextManifest:
        return ContextManifest(
            manifest_id=f"manifest_{request_hash[:24]}",
            request_hash=request_hash,
            status="error",
            host_id=request.host_id,
            repo_id=request.repo_id,
            task_id=request.task_id,
            run_id=request.run_id,
            provider=request.provider,
            token_budget={"max_tokens": int(request.token_budget), "estimated_tokens": 0},
            expires_at=_datetime_text(request.expires_at),
            error_kind=error_kind,
            error_message=error_message,
            created_at=_datetime_text(self.now()),
        )


def _doctor_ok(result: dict[str, Any]) -> bool:
    if result.get("stale") is True:
        return False
    if result.get("ok") is False:
        return False
    if result.get("status") in {"stale", "unhealthy", "error"}:
        return False
    fts = result.get("fts") if isinstance(result.get("fts"), dict) else {}
    if fts.get("rebuild_recommended") is True:
        return False
    return True


def _auto_load_blocked(pointer: ContextPointer) -> bool:
    kind = str(pointer.source_kind or "").strip().lower()
    if kind not in LONG_CONTEXT_SOURCE_KINDS:
        return False
    locator = pointer.locator or {}
    return not any(
        key in locator and str(locator[key]).strip()
        for key in ("section", "selected_section", "offset", "pointer")
    )


def _dead_reference(pointer: ContextPointer) -> bool:
    locator = pointer.locator or {}
    if locator.get("dead") is True or locator.get("deleted") is True:
        return True
    if locator.get("exists") is False:
        return True
    return False


def _dedupe(pointers: list[ContextPointer]) -> list[ContextPointer]:
    seen: set[str] = set()
    result: list[ContextPointer] = []
    for pointer in pointers:
        if pointer.pointer_id in seen:
            continue
        seen.add(pointer.pointer_id)
        result.append(pointer)
    return result


def _json_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    stdout = str(getattr(result, "stdout", "") or "")
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": getattr(result, "returncode", 1) == 0,
        "stdout": stdout,
        "stderr": str(getattr(result, "stderr", "") or ""),
    }


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else canonical_json(value)
    return max(1, (len(str(text)) + 3) // 4)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _datetime_text(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
