"""Tests for the Fleet MCP tool surface (M2).

Coverage:
  - describe_fleet_surface() lists the 6 tools and excludes mutation tools.
  - fleet_task_status returns run projection for a known task.
  - fleet_query_agent_states returns Fleet Context Graph projection.
  - fleet_submit_handoff follows existing M1 authorization constraints.
  - fleet_query_fumemory queries context pointers from the store.
  - fleet_get_context_manifest retrieves a signed manifest.
  - fleet_publish_work_summary records an auditable work-summary event.
  - FastMCP registration exposes only the six fleet tools (requires mcp pkg).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from code_index.openclaw_controller.fleet_mcp import (
    FLEET_TOOL_DESCRIPTIONS,
    FleetMcpTools,
    describe_fleet_surface,
)
from code_index.openclaw_controller.scheduler import FleetController
from code_index.openclaw_hostd.leases import InMemoryFleetLeaseStore
from code_index.openclaw_messaging.store import MessagingStore


SIGNING_SECRET = "test-secret"
NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

FORBIDDEN_TOOLS = {
    "update",
    "assign",
    "cancel",
    "rebuild_fts",
    "agent_start",
    "agent_event",
    "agent_end",
}


# ------------------------------------------------------------------
# Fixtures / helpers
# ------------------------------------------------------------------


def _make_controller(
    tmp_path: Path,
) -> tuple[FleetController, InMemoryFleetLeaseStore]:
    store = MessagingStore(tmp_path / "msg.db", signing_secret=SIGNING_SECRET)
    leases = InMemoryFleetLeaseStore()
    controller = FleetController(
        messaging_store=store,
        lease_store=leases,
        restart_cooldown_seconds=90,
    )
    return controller, leases


def _register_host(
    controller: FleetController,
    host_id: str,
    *,
    repo_root: str = r"E:\Repos\repo-a",
    now: datetime = NOW,
) -> None:
    controller.record_host_heartbeat(
        {
            "host_id": host_id,
            "heartbeat_interval_seconds": 10,
            "capabilities": {
                "repo_roots": [{"path": repo_root, "exists": True}],
                "providers": [
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "capabilities": ["task_run"],
                    }
                ],
            },
        },
        now=now,
    )


def _tools(tmp_path: Path, *, context_store: Any = None) -> FleetMcpTools:
    controller, _ = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    return FleetMcpTools(
        fleet_controller=controller,
        context_store=context_store,
        clock=lambda: NOW,
    )


def _tools_with_controller(
    controller: FleetController,
    context_store: Any = None,
) -> FleetMcpTools:
    return FleetMcpTools(
        fleet_controller=controller,
        context_store=context_store,
        clock=lambda: NOW,
    )


# ------------------------------------------------------------------
# describe_fleet_surface
# ------------------------------------------------------------------


def test_describe_fleet_surface_has_six_tools() -> None:
    surface = describe_fleet_surface()
    names = {t["name"] for t in surface["tools"]}
    assert names == set(FLEET_TOOL_DESCRIPTIONS.keys())
    assert len(names) == 6


def test_describe_fleet_surface_excludes_forbidden_tools() -> None:
    surface = describe_fleet_surface()
    names = {t["name"] for t in surface["tools"]}
    for bad in FORBIDDEN_TOOLS:
        assert bad not in names, f"forbidden tool {bad!r} present in fleet surface"


def test_describe_fleet_surface_includes_all_six_named_tools() -> None:
    surface = describe_fleet_surface()
    names = {t["name"] for t in surface["tools"]}
    for expected in (
        "fleet_task_status",
        "fleet_query_agent_states",
        "fleet_submit_handoff",
        "fleet_query_fumemory",
        "fleet_get_context_manifest",
        "fleet_publish_work_summary",
    ):
        assert expected in names, f"missing fleet tool {expected!r}"


def test_describe_fleet_surface_server_name() -> None:
    surface = describe_fleet_surface()
    assert surface["server"] == "openclaw_fleet"


# ------------------------------------------------------------------
# fleet_task_status
# ------------------------------------------------------------------


def test_fleet_task_status_unknown_task(tmp_path: Path) -> None:
    t = _tools(tmp_path)
    result = t.fleet_task_status("task-does-not-exist")
    assert result["found"] is False
    assert result["runs"] == []
    assert result["task_id"] == "task-does-not-exist"


def test_fleet_task_status_known_task(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    controller.record_agent_state(
        {
            "host_id": "host-01",
            "run_id": "run-001",
            "task_id": "task-xyz",
            "run_status": "running",
        }
    )
    t = _tools_with_controller(controller)
    result = t.fleet_task_status("task-xyz")
    assert result["found"] is True
    assert len(result["runs"]) == 1
    assert result["runs"][0]["task_id"] == "task-xyz"
    assert result["runs"][0]["host_id"] == "host-01"


def test_fleet_task_status_requires_task_id(tmp_path: Path) -> None:
    t = _tools(tmp_path)
    with pytest.raises(ValueError):
        t.fleet_task_status("")


# ------------------------------------------------------------------
# fleet_query_agent_states
# ------------------------------------------------------------------


def test_fleet_query_agent_states_empty(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    t = _tools_with_controller(controller)
    result = t.fleet_query_agent_states()
    assert "hosts" in result
    assert "runs" in result
    assert len(result["hosts"]) == 1
    assert result["hosts"][0]["host_id"] == "host-01"
    assert result["runs"] == []


def test_fleet_query_agent_states_with_run(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    controller.record_agent_state(
        {
            "host_id": "host-01",
            "run_id": "run-001",
            "task_id": "task-a",
            "run_status": "running",
        }
    )
    t = _tools_with_controller(controller)
    result = t.fleet_query_agent_states()
    assert len(result["runs"]) == 1
    assert result["runs"][0]["run_id"] == "run-001"


def test_fleet_query_agent_states_filter_by_host_id(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    _register_host(controller, "host-02", repo_root=r"E:\Repos\repo-b")
    for host in ("host-01", "host-02"):
        controller.record_agent_state(
            {"host_id": host, "run_id": f"run-{host}", "run_status": "running"}
        )
    t = _tools_with_controller(controller)
    result = t.fleet_query_agent_states(host_id="host-01")
    assert all(r["host_id"] == "host-01" for r in result["runs"])
    assert all(h["host_id"] == "host-01" for h in result["hosts"])


def test_fleet_query_agent_states_filter_by_run_id(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    for i in range(3):
        controller.record_agent_state(
            {"host_id": "host-01", "run_id": f"run-{i}", "run_status": "running"}
        )
    t = _tools_with_controller(controller)
    result = t.fleet_query_agent_states(run_id="run-1")
    assert len(result["runs"]) == 1
    assert result["runs"][0]["run_id"] == "run-1"


# ------------------------------------------------------------------
# fleet_submit_handoff — follows existing M1 authorization constraints
# ------------------------------------------------------------------


def test_fleet_submit_handoff_missing_fields_rejected(tmp_path: Path) -> None:
    t = _tools(tmp_path)
    result = t.fleet_submit_handoff({})
    assert result["status"] == "rejected"
    assert result["rejection"] is not None
    assert result["rejection"]["reason"] == "invalid_handoff_proposal"


def test_fleet_submit_handoff_lease_invalid_rejected(tmp_path: Path) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    t = _tools_with_controller(controller)
    result = t.fleet_submit_handoff(
        {
            "handoff_id": "hoff-1",
            "host_id": "host-01",
            "task_id": "task-a",
            "run_id": "run-001",
            "repo_root": r"E:\Repos\repo-a",
            "provider": "codex",
            "required_provider_capabilities": ["task_run"],
        }
    )
    assert result["status"] == "rejected"
    assert result["rejection"]["reason"] == "lease_invalid"


def test_fleet_submit_handoff_authorized_with_valid_leases(tmp_path: Path) -> None:
    controller, leases = _make_controller(tmp_path)
    _register_host(controller, "host-01")
    leases.acquire_lease(
        "repo",
        r"E:\Repos\repo-a",
        owner_host_id="host-01",
        owner_run_id="run-001",
        ttl_seconds=1800,
        now=NOW,
    )
    leases.acquire_lease(
        "task",
        "task-a",
        owner_host_id="host-01",
        owner_run_id="run-001",
        ttl_seconds=1800,
        now=NOW,
    )
    t = _tools_with_controller(controller)
    result = t.fleet_submit_handoff(
        {
            "handoff_id": "hoff-1",
            "host_id": "host-01",
            "task_id": "task-a",
            "run_id": "run-001",
            "repo_root": r"E:\Repos\repo-a",
            "provider": "codex",
            "required_provider_capabilities": ["task_run"],
        }
    )
    assert result["status"] == "authorized"
    assert result["handoff"] is not None
    assert result["rejection"] is None


def test_fleet_submit_handoff_non_mapping_returns_error(tmp_path: Path) -> None:
    t = _tools(tmp_path)
    result = t.fleet_submit_handoff("not-a-dict")  # type: ignore[arg-type]
    assert result.get("status") == "rejected" or "error" in result


# ------------------------------------------------------------------
# fleet_query_fumemory — requires context store
# ------------------------------------------------------------------


def test_fleet_query_fumemory_no_store(tmp_path: Path) -> None:
    t = _tools(tmp_path, context_store=None)
    result = t.fleet_query_fumemory()
    assert "error" in result
    assert result["pointers"] == []


def test_fleet_query_fumemory_empty_store(tmp_path: Path) -> None:
    pytest.importorskip("sqlite3")
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_query_fumemory()
    assert result["pointers"] == []
    assert result["total"] == 0


def test_fleet_query_fumemory_returns_pointers(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    store.upsert_context_pointer(
        source_uri="openclaw://test/pointer-1",
        source_kind="context_packet",
        pointer_kind="impact",
        content_hash="abc123",
        locator={"step": "impact"},
        summary="test pointer",
        tokens_estimate=50,
        host_id="host-01",
        repo_id="repo-a",
    )
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_query_fumemory()
    assert result["total"] == 1
    assert len(result["pointers"]) == 1
    assert result["pointers"][0]["source_kind"] == "context_packet"


def test_fleet_query_fumemory_filter_by_host_id(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    for host in ("host-01", "host-02"):
        store.upsert_context_pointer(
            source_uri=f"openclaw://test/{host}",
            source_kind="context_packet",
            pointer_kind="impact",
            content_hash=f"hash-{host}",
            locator={"host_id": host},
            summary=f"pointer for {host}",
            tokens_estimate=10,
            host_id=host,
        )
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_query_fumemory(host_id="host-01")
    assert all(p["host_id"] == "host-01" for p in result["pointers"])


def test_fleet_query_fumemory_filter_by_pointer_kind(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    for kind in ("impact", "orientation", "verification"):
        store.upsert_context_pointer(
            source_uri=f"openclaw://test/{kind}",
            source_kind="context_packet",
            pointer_kind=kind,
            content_hash=f"hash-{kind}",
            locator={"kind": kind},
            summary=f"pointer kind {kind}",
            tokens_estimate=10,
        )
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_query_fumemory(pointer_kind="impact")
    assert all(p["pointer_kind"] == "impact" for p in result["pointers"])


# ------------------------------------------------------------------
# fleet_get_context_manifest
# ------------------------------------------------------------------


def test_fleet_get_context_manifest_no_store(tmp_path: Path) -> None:
    t = _tools(tmp_path, context_store=None)
    result = t.fleet_get_context_manifest(manifest_id="manifest_abc")
    assert "error" in result
    assert result["manifest"] is None


def test_fleet_get_context_manifest_not_found(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_get_context_manifest(manifest_id="manifest_nonexistent")
    assert result["found"] is False
    assert result["manifest"] is None


def test_fleet_get_context_manifest_by_request_hash(tmp_path: Path) -> None:
    from code_index.openclaw_context.models import ContextManifest
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    manifest = ContextManifest(
        manifest_id="manifest_aabbcc001122334455667788",
        request_hash="req_hash_001",
        status="error",
        host_id="host-01",
        repo_id="repo-a",
        task_id="task-a",
        run_id="run-001",
        provider="codex",
        route_scope="local",
        error_kind="stale_index",
        error_message="test error",
        token_budget={"max_tokens": 8000, "estimated_tokens": 0},
    )
    store.store_manifest(manifest)
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_get_context_manifest(request_hash="req_hash_001")
    assert result["found"] is True
    assert result["manifest"] is not None
    assert result["manifest"]["manifest_id"] == "manifest_aabbcc001122334455667788"
    assert result["manifest"]["status"] == "error"


def test_fleet_get_context_manifest_by_manifest_id(tmp_path: Path) -> None:
    from code_index.openclaw_context.models import ContextManifest
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    manifest = ContextManifest(
        manifest_id="manifest_xxyyzz998877665544332211",
        request_hash="req_hash_002",
        status="error",
        host_id="host-01",
        repo_id="repo-a",
        task_id="task-b",
        run_id="run-002",
        provider="codex",
        route_scope="local",
        error_kind="stale_index",
        error_message="test error 2",
        token_budget={"max_tokens": 8000, "estimated_tokens": 0},
    )
    store.store_manifest(manifest)
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_get_context_manifest(manifest_id="manifest_xxyyzz998877665544332211")
    assert result["found"] is True
    assert result["manifest"]["manifest_id"] == "manifest_xxyyzz998877665544332211"


# ------------------------------------------------------------------
# fleet_publish_work_summary
# ------------------------------------------------------------------


def test_fleet_publish_work_summary_no_store(tmp_path: Path) -> None:
    t = _tools(tmp_path, context_store=None)
    result = t.fleet_publish_work_summary(
        host_id="host-01",
        run_id="run-001",
        summary="Completed refactor of scheduler module.",
    )
    assert "error" in result
    assert result["recorded"] is False


def test_fleet_publish_work_summary_with_store(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    t = _tools(tmp_path, context_store=store)
    result = t.fleet_publish_work_summary(
        host_id="host-01",
        run_id="run-001",
        summary="Completed refactor of scheduler module.",
    )
    assert result["recorded"] is True
    assert "pointer_id" in result


def test_fleet_publish_work_summary_is_queryable(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    t = _tools(tmp_path, context_store=store)
    t.fleet_publish_work_summary(
        host_id="host-01",
        run_id="run-001",
        summary="Implemented new handoff policy.",
        task_id="task-m2",
    )
    query_result = t.fleet_query_fumemory(
        host_id="host-01",
        pointer_kind="work_summary",
    )
    assert query_result["total"] >= 1
    assert any(
        p["pointer_kind"] == "work_summary" for p in query_result["pointers"]
    )


def test_fleet_publish_work_summary_requires_host_id(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    t = _tools(tmp_path, context_store=store)
    with pytest.raises(ValueError):
        t.fleet_publish_work_summary(host_id="", run_id="run-001", summary="x")


def test_fleet_publish_work_summary_requires_summary(tmp_path: Path) -> None:
    from code_index.openclaw_context.store import SQLiteContextStore

    store = SQLiteContextStore(":memory:")
    t = _tools(tmp_path, context_store=store)
    with pytest.raises(ValueError):
        t.fleet_publish_work_summary(host_id="host-01", run_id="run-001", summary="")


# ------------------------------------------------------------------
# FastMCP registration (requires mcp package)
# ------------------------------------------------------------------


def _fastmcp_tool_names(mcp: Any) -> set[str]:
    mgr = getattr(mcp, "_tool_manager", None)
    if mgr is not None:
        tools = getattr(mgr, "_tools", None)
        if isinstance(tools, dict):
            return set(tools.keys())
    lister = getattr(mcp, "list_tools", None)
    if callable(lister):
        import anyio

        result = anyio.run(lister)
        return {t.name for t in result}
    raise RuntimeError("cannot introspect FastMCP tool registry")


def test_build_fleet_fastmcp_registers_exactly_six_tools(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    controller, _ = _make_controller(tmp_path)
    from code_index.openclaw_controller.fleet_mcp import build_fleet_fastmcp

    mcp = build_fleet_fastmcp(fleet_controller=controller)
    names = _fastmcp_tool_names(mcp)
    assert names == set(FLEET_TOOL_DESCRIPTIONS.keys())


def test_build_fleet_fastmcp_does_not_register_forbidden_tools(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    controller, _ = _make_controller(tmp_path)
    from code_index.openclaw_controller.fleet_mcp import build_fleet_fastmcp

    mcp = build_fleet_fastmcp(fleet_controller=controller)
    names = _fastmcp_tool_names(mcp)
    for bad in FORBIDDEN_TOOLS:
        assert bad not in names, f"forbidden tool {bad!r} registered in fleet FastMCP"
