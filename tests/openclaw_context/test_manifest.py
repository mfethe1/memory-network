from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_context.manifest import CodeIndexContextProbe
from code_index.openclaw_context.manifest import ContextManifestBuilder
from code_index.openclaw_context.manifest import ManifestRequest
from code_index.openclaw_context.manifest import verify_context_manifest
from code_index.openclaw_context.models import canonical_json
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_context.store import SQLiteContextStore


NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


class FakeProbe:
    def __init__(self, *, doctor_ok: bool = True) -> None:
        self.doctor_ok = doctor_ok
        self.calls: list[tuple[str, Any]] = []

    def doctor(self) -> dict[str, Any]:
        self.calls.append(("doctor", None))
        return {"ok": self.doctor_ok, "stale": not self.doctor_ok}

    def impact(self, target_symbols: tuple[str, ...]) -> dict[str, Any]:
        self.calls.append(("impact", target_symbols))
        return {
            "target_symbols": list(target_symbols),
            "impacted": [
                {
                    "canonical_name": "pkg.service.caller",
                    "def_file": "pkg/service.py",
                }
            ],
        }

    def tests(self, target_symbols: tuple[str, ...]) -> dict[str, Any]:
        self.calls.append(("tests", target_symbols))
        return {
            "target_symbols": list(target_symbols),
            "node_ids": ["tests/test_service.py::test_handle"],
        }

    def repo_map(self, *, limit: int = 50) -> str:
        self.calls.append(("repo_map", limit))
        return "pkg.service.handle -> pkg/service.py"

    def agent_states(self) -> list[dict[str, Any]]:
        self.calls.append(("agent_states", None))
        return [
            {
                "agent_id": "peer",
                "run_id": "run-peer",
                "active_symbols_json": '["pkg.service.caller"]',
            }
        ]


def _request(**overrides: Any) -> ManifestRequest:
    values = {
        "host_id": "host-a",
        "repo_id": "repo-a",
        "task_id": "task-a",
        "run_id": "run-a",
        "provider": "codex",
        "target_symbols": ("pkg.service.handle",),
        "token_budget": 900,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return ManifestRequest(**values)


def test_manifest_generation_fits_budget_keeps_required_and_signs_idempotently(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        required = store.upsert_context_pointer(
            source_uri="file://repo/pkg/service.py",
            source_kind="code",
            content_hash="sha256:service-v1",
            locator={"path": "pkg/service.py", "start_line": 1, "end_line": 30},
            summary="required service implementation",
            tokens_estimate=140,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
            required=True,
        )
        optional = store.upsert_context_pointer(
            source_uri="fumemory://decision/service",
            source_kind="decision",
            pointer_kind="decision",
            content_hash="sha256:decision-v1",
            locator={"decision_id": "service"},
            summary="keep the existing retry semantics",
            tokens_estimate=110,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        probe = FakeProbe()
        builder = ContextManifestBuilder(
            store=store,
            probe=probe,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(
            _request(required_pointer_ids=(required.pointer_id,))
        )
        replay = builder.build_manifest(
            _request(required_pointer_ids=(required.pointer_id,))
        )

        assert manifest.status == "signed"
        assert manifest.estimated_tokens <= manifest.token_budget["max_tokens"]
        assert required.pointer_id in manifest.required_pointer_ids
        assert required.pointer_id in manifest.pointer_ids
        assert optional.pointer_id in manifest.pointer_ids
        assert manifest.signature
        assert manifest.source_hashes[required.pointer_id] == "sha256:service-v1"
        assert manifest.peer_agent_states[0]["run_id"] == "run-peer"
        assert replay.manifest_id == manifest.manifest_id
        assert replay.signature == manifest.signature
        assert [name for name, _ in probe.calls] == [
            "doctor",
            "impact",
            "tests",
            "repo_map",
            "agent_states",
        ]
    finally:
        store.close()


def test_long_soul_file_is_not_auto_loaded_without_selected_section(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        soul = store.upsert_context_pointer(
            source_uri="file://repo/SOUL.md",
            source_kind="soul",
            content_hash="sha256:soul-v1",
            locator={"path": "SOUL.md"},
            summary="full project soul file",
            tokens_estimate=10,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        selected_section = store.upsert_context_pointer(
            source_uri="file://repo/SOUL.md",
            source_kind="soul",
            content_hash="sha256:soul-v1",
            locator={"path": "SOUL.md", "section": "context policy"},
            summary="selected context policy section",
            tokens_estimate=10,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        builder = ContextManifestBuilder(
            store=store,
            probe=FakeProbe(),
            signing_secret="test-secret",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(_request())

        assert soul.pointer_id not in manifest.pointer_ids
        assert selected_section.pointer_id in manifest.pointer_ids
        assert {
            item["pointer_id"]: item["reason"] for item in manifest.omitted_context
        }[soul.pointer_id] == "auto_load_blocked"
    finally:
        store.close()


def test_manifest_builder_aborts_with_error_manifest_when_doctor_reports_stale(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        probe = FakeProbe(doctor_ok=False)
        builder = ContextManifestBuilder(
            store=store,
            probe=probe,
            signing_secret="test-secret",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(_request())

        assert manifest.status == "error"
        assert manifest.error_kind == "stale_index"
        assert manifest.pointer_ids == ()
        assert [name for name, _ in probe.calls] == ["doctor"]
    finally:
        store.close()


def test_manifest_builder_replaces_stale_doctor_error_after_recovery(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        probe = FakeProbe(doctor_ok=False)
        builder = ContextManifestBuilder(
            store=store,
            probe=probe,
            signing_secret="test-secret",
            now=lambda: NOW,
        )
        request = _request()

        stale = builder.build_manifest(request)
        probe.doctor_ok = True
        repaired = builder.build_manifest(request)

        assert stale.status == "error"
        assert stale.error_kind == "stale_index"
        assert repaired.status == "signed"
        assert repaired.request_hash == stale.request_hash
        assert store.get_manifest_by_request_hash(stale.request_hash or "") == repaired
    finally:
        store.close()


def test_manifest_builder_filters_candidates_and_required_pointers_by_policy(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        visible = store.upsert_context_pointer(
            source_uri="file://repo/pkg/service.py",
            source_kind="code",
            content_hash="sha256:visible",
            locator={"path": "pkg/service.py"},
            summary="visible repo pointer",
            sensitivity="repo",
            host_id="host-b",
            provider="claude",
            target_symbols=["pkg.service.handle"],
        )
        host_private = store.upsert_context_pointer(
            source_uri="memo://host-private",
            source_kind="run_metadata",
            content_hash="sha256:host-private",
            locator={"id": "host-private"},
            summary="foreign host-private pointer",
            sensitivity="host_private",
            host_id="host-b",
            provider="claude",
            target_symbols=["pkg.service.handle"],
        )
        provider_private = store.upsert_context_pointer(
            source_uri="memo://provider-private",
            source_kind="transcript",
            content_hash="sha256:provider-private",
            locator={"id": "provider-private"},
            summary="foreign provider-private pointer",
            sensitivity="provider_private",
            host_id="host-b",
            provider="claude",
            target_symbols=["pkg.service.handle"],
        )
        builder = ContextManifestBuilder(
            store=store,
            probe=FakeProbe(),
            signing_secret="test-secret",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(_request())
        required_private = builder.build_manifest(
            _request(required_pointer_ids=(host_private.pointer_id,))
        )

        assert manifest.status == "signed"
        assert visible.pointer_id in manifest.pointer_ids
        assert host_private.pointer_id not in manifest.pointer_ids
        assert provider_private.pointer_id not in manifest.pointer_ids
        assert required_private.status == "error"
        assert required_private.error_kind == "missing_required_pointer"
        assert host_private.pointer_id not in required_private.pointer_ids
    finally:
        store.close()


def test_manifest_verification_rejects_wrong_key_expiry_and_tampering(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        store.upsert_context_pointer(
            source_uri="file://repo/pkg/service.py",
            source_kind="code",
            content_hash="sha256:service-v1",
            locator={"path": "pkg/service.py"},
            summary="service implementation",
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        builder = ContextManifestBuilder(
            store=store,
            probe=FakeProbe(),
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(_request())
        tampered_payload = dict(
            json.loads(manifest.signed_payload or "{}"),
            pointer_ids=["ptr_tampered"],
        )
        tampered_signature = replace(manifest, signature="0" * 64)
        tampered_payload_manifest = replace(
            manifest,
            signed_payload=canonical_json(tampered_payload),
        )
        tampered_row = replace(manifest, pointer_ids=("ptr_tampered",))

        assert verify_context_manifest(
            manifest,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )
        assert not verify_context_manifest(
            manifest,
            signing_secret="test-secret",
            signature_key_id="wrong-key",
            now=lambda: NOW,
        )
        assert not verify_context_manifest(
            manifest,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW + timedelta(hours=1),
        )
        assert not verify_context_manifest(
            tampered_signature,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )
        assert not verify_context_manifest(
            tampered_payload_manifest,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )
        assert not verify_context_manifest(
            tampered_row,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )
    finally:
        store.close()


def test_manifest_default_expiry_is_signed_and_verifiable(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        builder = ContextManifestBuilder(
            store=store,
            probe=FakeProbe(),
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )
        request = ManifestRequest(
            host_id="host-a",
            repo_id="repo-a",
            task_id="task-default-expiry",
            run_id="run-default-expiry",
            provider="codex",
            target_symbols=("pkg.service.handle",),
        )

        manifest = builder.build_manifest(request)

        assert manifest.status == "signed"
        assert manifest.expires_at == (NOW + timedelta(minutes=30)).isoformat()
        assert verify_context_manifest(
            manifest,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: NOW,
        )
    finally:
        store.close()


def test_manifest_default_expiry_cache_regenerates_after_ttl(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        current = {"now": NOW}
        probe = FakeProbe()
        builder = ContextManifestBuilder(
            store=store,
            probe=probe,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: current["now"],
        )
        request = ManifestRequest(
            host_id="host-a",
            repo_id="repo-a",
            task_id="task-default-expiry-replay",
            run_id="run-default-expiry-replay",
            provider="codex",
            target_symbols=("pkg.service.handle",),
        )

        first = builder.build_manifest(request)
        replay = builder.build_manifest(request)
        current["now"] = NOW + timedelta(minutes=31)
        refreshed = builder.build_manifest(request)

        assert replay.manifest_id == first.manifest_id
        assert replay.signature == first.signature
        assert replay.expires_at == first.expires_at
        assert not verify_context_manifest(
            first,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: current["now"],
        )
        assert refreshed.request_hash == first.request_hash
        assert refreshed.expires_at == (
            NOW + timedelta(minutes=61)
        ).isoformat()
        assert refreshed.signature != first.signature
        assert verify_context_manifest(
            refreshed,
            signing_secret="test-secret",
            signature_key_id="test-key",
            now=lambda: current["now"],
        )
        assert [name for name, _ in probe.calls] == [
            "doctor",
            "impact",
            "tests",
            "repo_map",
            "agent_states",
            "doctor",
            "impact",
            "tests",
            "repo_map",
            "agent_states",
        ]
    finally:
        store.close()


def test_manifest_builder_prunes_dead_reference_pointers(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    try:
        dead = store.upsert_context_pointer(
            source_uri="file://repo/pkg/deleted.py",
            source_kind="code",
            content_hash="sha256:deleted",
            locator={"path": "pkg/deleted.py", "exists": False},
            summary="deleted implementation",
            tokens_estimate=10,
            sensitivity="repo",
            target_symbols=["pkg.service.handle"],
        )
        builder = ContextManifestBuilder(
            store=store,
            probe=FakeProbe(),
            signing_secret="test-secret",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(_request())

        assert manifest.status == "signed"
        assert dead.pointer_id not in manifest.pointer_ids
        assert {
            item["pointer_id"]: item["reason"] for item in manifest.omitted_context
        }[dead.pointer_id] == "dead_reference"
    finally:
        store.close()


class FakeCommandResult:
    def __init__(self, stdout: str, *, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_manifest_builder_default_probe_reads_fleet_context_graph_from_lease_store(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(tmp_path / "context.db")
    lease_store = InMemoryFleetLeaseStore()
    lease_store.put_agent_state(
        "host-peer.run-peer",
        {
            "host_id": "host-peer",
            "agent_id": "agent-peer",
            "task_id": "task-peer",
            "run_id": "run-peer",
            "active_symbols_json": '["pkg.service.handle"]',
            "current_subtask": "found retry behavior decision",
            "last_action_at": NOW.isoformat(),
        },
    )
    commands: list[list[str]] = []

    def runner(args: list[str]) -> FakeCommandResult:
        commands.append(args)
        if "doctor" in args:
            return FakeCommandResult('{"ok": true}')
        if "impact" in args:
            return FakeCommandResult('{"impacted": []}')
        if "tests" in args:
            return FakeCommandResult('{"node_ids": []}')
        if "repo-map" in args:
            return FakeCommandResult("pkg.service.handle -> pkg/service.py")
        return FakeCommandResult("{}", returncode=1)

    try:
        builder = ContextManifestBuilder(
            store=store,
            probe=CodeIndexContextProbe(runner=runner, lease_store=lease_store),
            signing_secret="test-secret",
            now=lambda: NOW,
        )

        manifest = builder.build_manifest(_request())

        assert manifest.status == "signed"
        assert manifest.peer_agent_states == (
            {
                "host_id": "host-peer",
                "agent_id": "agent-peer",
                "task_id": "task-peer",
                "run_id": "run-peer",
                "active_symbols_json": '["pkg.service.handle"]',
                "current_subtask": "found retry behavior decision",
                "last_action_at": NOW.isoformat(),
            },
        )
        assert any("repo-map" in command for command in commands)
    finally:
        store.close()
