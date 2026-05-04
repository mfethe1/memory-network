from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_context.manifest import CodeIndexContextProbe
from code_index.openclaw_context.manifest import ContextManifestBuilder
from code_index.openclaw_context.manifest import ManifestRequest
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
