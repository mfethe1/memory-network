"""Focused MCP tests for retrieval broker delegation."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_index import config as cfg_mod
from code_index.cli import main
from code_index.commands.mcp_tool_impl import _tool_retrieval_broker


@dataclass
class _FakeRetrievalRequest:
    query: str
    scope: str | None = None
    include_kinds: list[str] | None = None
    limit: int = 10
    byte_budget: int = 20_000
    selected_paths: list[str] | None = None
    selected_nodes: list[Any] | None = None


def test_retrieval_broker_tool_delegates_to_code_index_retrieval(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = cfg_mod.load(tmp_path)
    fake_module = types.ModuleType("code_index.retrieval")
    fake_module.RetrievalRequest = _FakeRetrievalRequest
    broker_payload = {"kind": "fake_broker_payload", "results": []}
    seen: dict[str, Any] = {}

    def retrieve(config, request):
        seen["config"] = config
        seen["request"] = request
        return broker_payload

    fake_module.retrieve = retrieve
    monkeypatch.setitem(sys.modules, "code_index.retrieval", fake_module)

    import code_index

    monkeypatch.setattr(code_index, "retrieval", fake_module, raising=False)

    result = _tool_retrieval_broker(
        cfg,
        query="auth service",
        scope="graph",
        include_kinds=["file", "symbol"],
        limit=7,
        byte_budget=4096,
        selected_paths=["pkg/auth.py"],
        selected_nodes=["file:pkg/auth.py"],
    )

    assert result is broker_payload
    assert seen["config"] is cfg
    assert seen["request"] == _FakeRetrievalRequest(
        query="auth service",
        scope="graph",
        include_kinds=["file", "symbol"],
        limit=7,
        byte_budget=4096,
        selected_paths=["pkg/auth.py"],
        selected_nodes=["file:pkg/auth.py"],
    )


def test_retrieval_broker_tool_invokes_real_broker_with_connection(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "memory.py").write_text(
        "def remember():\n    return 'broker needle'\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    cfg = cfg_mod.load(tmp_path)
    result = _tool_retrieval_broker(
        cfg,
        query="broker needle",
        include_kinds=["code_chunk"],
        limit=5,
        byte_budget=10_000,
    )

    assert result["kind"] == "code_index_retrieval"
    assert result["bytes_used"] <= result["budget_bytes"]
    assert any(item["source_kind"] == "code_chunk" for item in result["results"])
