"""Fleet MCP tool implementations and surface description for OpenClaw M2.

Six read-heavy fleet tools are exposed:
  fleet_task_status         - task and run projection for a specific task.
  fleet_query_agent_states  - Fleet Context Graph projection (hosts + runs).
  fleet_submit_handoff      - handoff proposal via existing M1 auth constraints.
  fleet_query_fumemory      - query fumemory context pointers from local store.
  fleet_get_context_manifest- retrieve a signed context manifest.
  fleet_publish_work_summary- record an auditable work-summary event.

Write operations (update, assign, cancel, shell, lease mutation) are NOT
exposed. fleet_submit_handoff is authorized through the existing controller
constraints and is not a raw lease or task mutation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from code_index import __version__ as _code_index_version


FLEET_TOOL_DESCRIPTIONS: dict[str, str] = {
    "fleet_task_status": (
        "Read task status and run projection for a fleet task. "
        "Returns host, run, and context-health details from the M1 fleet store."
    ),
    "fleet_query_agent_states": (
        "Query the Fleet Context Graph agent-state projection. "
        "Returns hosts and runs from the passive M1 stores; filter by host_id or run_id."
    ),
    "fleet_submit_handoff": (
        "Submit a handoff proposal via the Fleet Controller. "
        "Follows existing M1 authorization constraints (lease validation, restart cooldown). "
        "Rejected if leases are invalid or cooldown is active."
    ),
    "fleet_query_fumemory": (
        "Query fumemory context pointers from the local SQLite context store. "
        "Filter by host_id, repo_id, or pointer_kind; returns pointer summaries."
    ),
    "fleet_get_context_manifest": (
        "Retrieve a signed context manifest from the local context store by "
        "manifest_id or request_hash."
    ),
    "fleet_publish_work_summary": (
        "Record a work-summary event as an auditable fleet context pointer. "
        "Requires host_id and run_id; summary text is stored with sensitivity='repo'."
    ),
}


def describe_fleet_surface() -> dict[str, Any]:
    """Return the static fleet MCP surface description.

    The returned tool list never contains ``update``, ``assign``, ``cancel``,
    shell, or lease-mutation tools.
    """
    return {
        "server": "openclaw_fleet",
        "version": _code_index_version,
        "transport": "stdio",
        "read_only": False,
        "tools": [
            {"name": name, "description": desc}
            for name, desc in FLEET_TOOL_DESCRIPTIONS.items()
        ],
    }


class FleetMcpTools:
    """Read-heavy MCP tool implementations over the M1 fleet stores."""

    def __init__(
        self,
        *,
        fleet_controller: Any,
        context_store: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.fleet_controller = fleet_controller
        self.context_store = context_store
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock is not None else datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # fleet_task_status
    # ------------------------------------------------------------------

    def fleet_task_status(self, task_id: str) -> dict[str, Any]:
        task_id = _required_text(task_id, "task_id")
        fleet = self.fleet_controller.project_fleet(now=self._now())
        matching = [r for r in fleet.get("runs", []) if r.get("task_id") == task_id]
        return {
            "task_id": task_id,
            "runs": matching,
            "found": bool(matching),
        }

    # ------------------------------------------------------------------
    # fleet_query_agent_states
    # ------------------------------------------------------------------

    def fleet_query_agent_states(
        self,
        host_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        fleet = self.fleet_controller.project_fleet(now=self._now())
        runs = list(fleet.get("runs", []))
        hosts = list(fleet.get("hosts", []))
        if host_id:
            runs = [r for r in runs if r.get("host_id") == host_id]
            hosts = [h for h in hosts if h.get("host_id") == host_id]
        if run_id:
            runs = [r for r in runs if r.get("run_id") == run_id]
        return {"hosts": hosts, "runs": runs}

    # ------------------------------------------------------------------
    # fleet_submit_handoff
    # ------------------------------------------------------------------

    def fleet_submit_handoff(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(proposal, Mapping):
            return {"error": "proposal must be an object", "status": "rejected"}
        result = self.fleet_controller.submit_handoff_proposal(
            dict(proposal),
            now=self._now(),
        )
        return result.to_dict()

    # ------------------------------------------------------------------
    # fleet_query_fumemory
    # ------------------------------------------------------------------

    def fleet_query_fumemory(
        self,
        host_id: str | None = None,
        repo_id: str | None = None,
        pointer_kind: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if self.context_store is None:
            return {"error": "context store not configured", "pointers": []}
        try:
            pointers = self.context_store.list_context_pointers(
                pointer_kind=pointer_kind,
            )
        except Exception as exc:
            return {"error": str(exc), "pointers": []}
        if host_id:
            pointers = [p for p in pointers if p.host_id == host_id]
        if repo_id:
            pointers = [p for p in pointers if p.repo_id == repo_id]
        limit = max(1, int(limit or 20))
        page = pointers[:limit]
        return {
            "pointers": [_pointer_summary(p) for p in page],
            "total": len(pointers),
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # fleet_get_context_manifest
    # ------------------------------------------------------------------

    def fleet_get_context_manifest(
        self,
        manifest_id: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        if self.context_store is None:
            return {"error": "context store not configured", "manifest": None}
        manifest = None
        if manifest_id:
            get_by_id = getattr(self.context_store, "get_manifest_by_id", None)
            if get_by_id is not None:
                manifest = get_by_id(manifest_id)
            if manifest is None:
                # Fall back to scanning all manifests by manifest_id field.
                manifest = _find_manifest_by_id(self.context_store, manifest_id)
        if manifest is None and request_hash:
            manifest = self.context_store.get_manifest_by_request_hash(request_hash)
        if manifest is None:
            return {"manifest": None, "found": False}
        return {"manifest": _manifest_summary(manifest), "found": True}

    # ------------------------------------------------------------------
    # fleet_publish_work_summary
    # ------------------------------------------------------------------

    def fleet_publish_work_summary(
        self,
        host_id: str,
        run_id: str,
        summary: str,
        task_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        host_id = _required_text(host_id, "host_id")
        run_id = _required_text(run_id, "run_id")
        summary_text = _required_text(summary, "summary")
        if self.context_store is None:
            return {
                "error": "context store not configured",
                "recorded": False,
            }
        content_hash = _sha(f"{host_id}\0{run_id}\0{task_id or ''}\0{summary_text}")
        source_uri = (
            f"openclaw://work-summary/{host_id}/{run_id}/{content_hash[:16]}"
        )
        extra: dict[str, Any] = {}
        if task_id:
            extra["task_id"] = task_id
        if metadata:
            extra.update(dict(metadata))
        try:
            pointer = self.context_store.upsert_context_pointer(
                source_uri=source_uri,
                source_kind="work_summary",
                pointer_kind="work_summary",
                content_hash=content_hash,
                locator={"host_id": host_id, "run_id": run_id, **extra},
                summary=summary_text,
                tokens_estimate=max(1, (len(summary_text) + 3) // 4),
                sensitivity="repo",
                host_id=host_id,
                repo_id=extra.get("repo_id"),
                provider=extra.get("provider"),
            )
            return {"recorded": True, "pointer_id": pointer.pointer_id}
        except Exception as exc:
            return {"error": str(exc), "recorded": False}


# ------------------------------------------------------------------
# FastMCP wiring (optional; requires the `mcp` package)
# ------------------------------------------------------------------


def build_fleet_fastmcp(
    fleet_controller: Any,
    context_store: Any | None = None,
) -> Any:
    """Build a FastMCP server exposing the six read-heavy fleet tools.

    Returns a ``FastMCP`` instance.  Raises ``ImportError`` if the ``mcp``
    package is not installed.
    """
    from mcp.server.fastmcp import FastMCP

    tools_impl = FleetMcpTools(
        fleet_controller=fleet_controller,
        context_store=context_store,
    )
    descriptions = FLEET_TOOL_DESCRIPTIONS
    mcp = FastMCP("openclaw_fleet")

    @mcp.tool(description=descriptions["fleet_task_status"])
    def fleet_task_status(task_id: str) -> dict:
        return tools_impl.fleet_task_status(task_id)

    @mcp.tool(description=descriptions["fleet_query_agent_states"])
    def fleet_query_agent_states(
        host_id: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        return tools_impl.fleet_query_agent_states(host_id=host_id, run_id=run_id)

    @mcp.tool(description=descriptions["fleet_submit_handoff"])
    def fleet_submit_handoff(proposal: dict) -> dict:
        return tools_impl.fleet_submit_handoff(proposal)

    @mcp.tool(description=descriptions["fleet_query_fumemory"])
    def fleet_query_fumemory(
        host_id: str | None = None,
        repo_id: str | None = None,
        pointer_kind: str | None = None,
        limit: int = 20,
    ) -> dict:
        return tools_impl.fleet_query_fumemory(
            host_id=host_id,
            repo_id=repo_id,
            pointer_kind=pointer_kind,
            limit=limit,
        )

    @mcp.tool(description=descriptions["fleet_get_context_manifest"])
    def fleet_get_context_manifest(
        manifest_id: str | None = None,
        request_hash: str | None = None,
    ) -> dict:
        return tools_impl.fleet_get_context_manifest(
            manifest_id=manifest_id,
            request_hash=request_hash,
        )

    @mcp.tool(description=descriptions["fleet_publish_work_summary"])
    def fleet_publish_work_summary(
        host_id: str,
        run_id: str,
        summary: str,
        task_id: str | None = None,
    ) -> dict:
        return tools_impl.fleet_publish_work_summary(
            host_id=host_id,
            run_id=run_id,
            summary=summary,
            task_id=task_id,
        )

    return mcp


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _pointer_summary(pointer: Any) -> dict[str, Any]:
    return {
        "pointer_id": getattr(pointer, "pointer_id", None),
        "pointer_kind": getattr(pointer, "pointer_kind", None),
        "source_kind": getattr(pointer, "source_kind", None),
        "source_uri": getattr(pointer, "source_uri", None),
        "summary": getattr(pointer, "summary", None),
        "host_id": getattr(pointer, "host_id", None),
        "repo_id": getattr(pointer, "repo_id", None),
        "tokens_estimate": getattr(pointer, "tokens_estimate", None),
        "created_at": getattr(pointer, "created_at", None),
    }


def _manifest_summary(manifest: Any) -> dict[str, Any]:
    return {
        "manifest_id": getattr(manifest, "manifest_id", None),
        "request_hash": getattr(manifest, "request_hash", None),
        "status": getattr(manifest, "status", None),
        "host_id": getattr(manifest, "host_id", None),
        "repo_id": getattr(manifest, "repo_id", None),
        "task_id": getattr(manifest, "task_id", None),
        "run_id": getattr(manifest, "run_id", None),
        "provider": getattr(manifest, "provider", None),
        "expires_at": getattr(manifest, "expires_at", None),
        "signature_key_id": getattr(manifest, "signature_key_id", None),
        "pointer_ids": list(getattr(manifest, "pointer_ids", ()) or ()),
        "error_kind": getattr(manifest, "error_kind", None),
        "error_message": getattr(manifest, "error_message", None),
        "created_at": getattr(manifest, "created_at", None),
    }


def _find_manifest_by_id(context_store: Any, manifest_id: str) -> Any:
    conn = getattr(context_store, "_conn", None)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM context_manifests WHERE manifest_id = ? LIMIT 1",
            (manifest_id,),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    from_row = getattr(context_store, "_manifest_from_row", None)
    if from_row is not None:
        return from_row(row)
    # Use the module-level helper if accessible.
    try:
        from code_index.openclaw_context.store import _manifest_from_row

        return _manifest_from_row(row)
    except Exception:
        return None


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
