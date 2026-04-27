"""Regression tests for the read-only default surface of `code_index mcp-serve`.

The default mode must NOT expose mutating tools (`update`, `rebuild_fts`).
They only register when `--allow-writes` is passed. `describe_surface()` must
stay consistent with the actually-exposed FastMCP surface in both modes.
"""

from __future__ import annotations

import io

import pytest

from code_index.cli import build_parser
from code_index.commands import mcp_serve_cmd as m


# ---------- describe_surface honours the flag ----------


def test_describe_surface_default_omits_mutating_tools() -> None:
    surface = m.describe_surface()
    names = {t["name"] for t in surface["tools"]}
    assert "update" not in names
    assert "rebuild_fts" not in names
    # Read tools must still be present. `ask` is read-only (it dispatches
    # to lookup/impact/tests/grep/FTS/similar/repo-map/doctor — never to
    # mutating primitives), so it stays in the default surface.
    for read_tool in (
        "search_text",
        "search_query",
        "search_ast",
        "find_symbol",
        "impact",
        "affected_tests",
        "doctor",
        "ask",
        "code_graph",
        "agent_activity",
    ):
        assert read_tool in names, f"missing read tool {read_tool!r} in default surface"


def test_describe_surface_with_allow_writes_includes_mutating_tools() -> None:
    surface = m.describe_surface(allow_writes=True)
    names = {t["name"] for t in surface["tools"]}
    assert "update" in names
    assert "rebuild_fts" in names
    assert "agent_start" in names
    assert "agent_event" in names
    assert "agent_end" in names
    # Mutating tool descriptions must warn the caller.
    for tool in surface["tools"]:
        if tool["name"] in (
            "update",
            "rebuild_fts",
            "agent_start",
            "agent_event",
            "agent_end",
        ):
            assert "MUTATING" in tool["description"].upper()


# ---------- FastMCP registration honours the flag ----------


def _registered_tool_names(mcp) -> set[str]:
    """Pull the registered tool names out of a FastMCP instance across SDK layouts."""
    # FastMCP exposes a private `_tool_manager` with `_tools` dict in current SDK.
    mgr = getattr(mcp, "_tool_manager", None)
    if mgr is not None:
        tools = getattr(mgr, "_tools", None)
        if isinstance(tools, dict):
            return set(tools.keys())
    # Fallback: list_tools() coroutine; call via anyio if needed.
    lister = getattr(mcp, "list_tools", None)
    if callable(lister):
        import anyio

        result = anyio.run(lister)
        return {t.name for t in result}
    raise RuntimeError("cannot introspect FastMCP tool registry")


def test_build_fastmcp_default_does_not_register_mutating_tools(tmp_path) -> None:
    pytest.importorskip("mcp")
    from code_index import config as cfg_mod

    cfg = cfg_mod.load(tmp_path)
    mcp = m._build_fastmcp(cfg)  # default: allow_writes=False
    names = _registered_tool_names(mcp)
    assert "update" not in names
    assert "rebuild_fts" not in names
    assert "agent_start" not in names
    assert "agent_event" not in names
    assert "agent_end" not in names
    assert "search_text" in names
    assert "doctor" in names
    assert "agent_activity" in names


def test_build_fastmcp_allow_writes_registers_mutating_tools(tmp_path) -> None:
    pytest.importorskip("mcp")
    from code_index import config as cfg_mod

    cfg = cfg_mod.load(tmp_path)
    mcp = m._build_fastmcp(cfg, allow_writes=True)
    names = _registered_tool_names(mcp)
    assert "update" in names
    assert "rebuild_fts" in names
    assert "agent_event" in names


# ---------- CLI subparser exposes the flag ----------


def test_cli_mcp_serve_subparser_has_allow_writes_flag() -> None:
    parser = build_parser()
    ns = parser.parse_args(["mcp-serve", "--describe"])
    assert hasattr(ns, "allow_writes")
    assert ns.allow_writes is False

    ns2 = parser.parse_args(["mcp-serve", "--describe", "--allow-writes"])
    assert ns2.allow_writes is True


# ---------- resources never include a write URI ----------


def test_no_resource_uri_triggers_mutation() -> None:
    """Belt-and-braces: the resource list must not mention update / rebuild_fts."""
    for uri, _desc in m._RESOURCE_DESCRIPTIONS:
        assert "update" not in uri.lower()
        assert "rebuild" not in uri.lower()
