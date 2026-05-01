"""HTTP coverage for the live graph server."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import textwrap
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import agent_activity
from code_index import lease_manager
from code_index import run_lifecycle
from code_index.cli import main
from code_index.commands import agent_adapter_cmd
from code_index.commands import graph_server_cmd
from code_index.commands import mcp_tool_impl
from code_index.commands.graph_notes import graph_notes_block
from code_index.commands.graph_server_cmd import _agent_stream_payload, _make_handler
from code_index.commands.graph_server_perf import _make_perf_state, _observe_latency


def _request_json(
    url: str, payload: dict | None = None, headers: dict | None = None
) -> dict:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


class _GraphServerHarness:
    def __init__(
        self,
        *,
        root: Path,
        server: ThreadingHTTPServer,
        thread: threading.Thread,
        capsys,
    ) -> None:
        self.root = root
        self.server = server
        self.thread = thread
        self.capsys = capsys
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def __enter__(self) -> "_GraphServerHarness":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post_json(
        self,
        path: str,
        payload: dict,
        *,
        headers: dict | None = None,
        expect_status: int = 200,
    ) -> dict:
        return self._request(
            path,
            payload=payload,
            headers=headers,
            expect_status=expect_status,
        )

    def get_json(
        self,
        path: str,
        *,
        headers: dict | None = None,
        expect_status: int = 200,
    ) -> dict:
        return self._request(path, headers=headers, expect_status=expect_status)

    def reindex(self) -> None:
        assert main(["update", "--root", str(self.root), "--json"]) == 0
        self.capsys.readouterr()

    def _request(
        self,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict | None = None,
        expect_status: int = 200,
    ) -> dict:
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = int(response.status)
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read().decode("utf-8")
        assert status == expect_status
        return json.loads(body)


def _make_server(
    tmp_path: Path,
    capsys,
    monkeypatch,
    *,
    scope: str | None = None,
) -> _GraphServerHarness:
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("CODE_INDEX_GRAPH_TOKEN", raising=False)
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
        scope=scope,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _GraphServerHarness(
        root=tmp_path,
        server=server,
        thread=thread,
        capsys=capsys,
    )


def _request_json_with_headers(
    url: str, payload: dict | None = None, headers: dict | None = None
) -> tuple[dict, dict]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return (
            json.loads(response.read().decode("utf-8")),
            dict(response.headers.items()),
        )


def _request_status(
    url: str, payload: dict | None = None, headers: dict | None = None
) -> int:
    data = json.dumps(payload or {}).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=data, headers=request_headers
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _request_raw_status(
    url: str, body: bytes, headers: dict | None = None
) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers or {}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


def _preflight_task_payload(
    base_url: str,
    payload: dict,
    *,
    headers: dict | None = None,
) -> dict:
    preflight = _request_json(
        f"{base_url}/api/agent-task-preflight",
        payload,
        headers=headers,
    )
    out = dict(payload)
    out["preflight"] = preflight["preflight"]
    if preflight["preflight"].get("requires_confirmation"):
        out["preflight_confirmed"] = True
    return out


def _request_get_status(url: str, headers: dict | None = None) -> int:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _request_text(url: str, headers: dict | None = None) -> str:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _read_sse_event(
    url: str,
    event_name: str,
    *,
    headers: dict | None = None,
    timeout: float = 5.0,
) -> dict:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(request, timeout=timeout) as response:
        current_event = "message"
        data_lines: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = response.readline().decode("utf-8")
            except socket.timeout as exc:
                raise AssertionError(f"SSE event {event_name!r} was not received") from exc
            if line == "":
                continue
            line = line.rstrip("\r\n")
            if line == "":
                if current_event == event_name and data_lines:
                    return json.loads("\n".join(data_lines))
                current_event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
    raise AssertionError(f"SSE event {event_name!r} was not received")


def _wait_for_run_status(
    config: cfg_mod.Config,
    run_id: str,
    expected: str,
    *,
    timeout: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last_run: dict | None = None
    while time.monotonic() < deadline:
        conn = db_mod.connect(config.db_path)
        try:
            last_run = agent_activity.get_run(conn, run_id)
        finally:
            db_mod.close(conn)
        if last_run and last_run["status"] == expected:
            return last_run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {expected}: {last_run}")


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"path was not created: {path}")


def _wait_for_event_message(
    config: cfg_mod.Config,
    run_id: str,
    needle: str,
    *,
    timeout: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last_messages: list[str] = []
    while time.monotonic() < deadline:
        conn = db_mod.connect(config.db_path)
        try:
            recent = agent_activity.recent_events(conn, limit=30)
        finally:
            db_mod.close(conn)
        last_messages = [
            event["message"]
            for event in recent
            if event.get("run_id") == run_id
        ]
        for event in recent:
            if event.get("run_id") == run_id and needle in event["message"]:
                return event
        time.sleep(0.05)
    raise AssertionError(f"event containing {needle!r} was not recorded: {last_messages}")


def _wait_for_process_row(config: cfg_mod.Config, run_id: str) -> dict:
    deadline = time.time() + 5
    last_status = None
    while time.time() < deadline:
        conn = db_mod.connect(config.db_path)
        try:
            row = conn.execute(
                """
                SELECT p.*, r.run_id
                  FROM agent_run_processes p
                  JOIN agent_runs r ON r.run_pk = p.run_pk
                 WHERE r.run_id = ?
                 ORDER BY p.process_pk DESC
                 LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        finally:
            db_mod.close(conn)
        if row is not None:
            last_status = row["status"]
            if row["status"] in {"completed", "failed", "cancelled", "orphaned"}:
                return dict(row)
        time.sleep(0.05)
    raise AssertionError(f"process row for {run_id} did not finish: {last_status}")


def test_graph_server_agent_providers_endpoint_returns_registry(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        providers = _request_json(f"{base_url}/api/agent-providers")
        assert providers["ok"] is True
        assert providers["kind"] == "code_index_agent_provider_registry"
        assert providers["runtime"]["dispatch"]["local_command_configured"] is False
        assert "codex" in providers["runtime"]["dispatch"]["provider_presets"]
        providers_by_id = {
            provider["id"]: provider for provider in providers["providers"]
        }
        assert providers_by_id["codex"]["display_name"] == "Codex"
        assert "stream_json_output" in providers_by_id["kimi"]["capabilities"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_scope_focuses_start_search_and_task_selection(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        "def scoped_needle() -> str:\n    return 'needle from scoped package'\n",
        encoding="utf-8",
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "b.py").write_text(
        "def outside_needle() -> str:\n    return 'needle from outside package'\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
        scope="pkg",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        graph = _request_json(f"{base_url}/repo-graph.json")
        assert graph["root"] == str(tmp_path.resolve())
        assert graph["scope"]["path"] == "pkg"
        assert graph["scope"]["explicit"] is True
        assert graph["focus_paths"] == ["pkg/a.py"]
        assert "file:other/b.py" in {node["id"] for node in graph["nodes"]}
        assert graph["agent"]["active_files"] == ["pkg/a.py"]

        search = _request_json(f"{base_url}/api/search?q=needle&scope=all&limit=10")
        assert search["files"]
        assert {item["file_path"] for item in search["files"]} == {"pkg/a.py"}

        preflight = _request_json(
            f"{base_url}/api/agent-task-preflight",
            {
                "agent_name": "Codex",
                "message": "Start from the scoped package.",
            },
        )
        assert preflight["ok"] is True
        assert preflight["draft"]["root"] == str(tmp_path.resolve())
        assert preflight["draft"]["scope"]["path"] == "pkg"
        assert preflight["draft"]["selected_paths"] == ["pkg/a.py"]
        assert preflight["draft"]["context_policy"]["retrieval_handles"][
            "selected_paths"
        ] == ["pkg/a.py"]
        assert preflight["draft"]["graph_context"]["selected_nodes"][0]["path"] == (
            "pkg/a.py"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_scope_must_stay_inside_root(tmp_path: Path, capsys):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    args = argparse.Namespace(
        root=str(tmp_path),
        scope=str(outside),
        host="127.0.0.1",
        port=0,
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )

    assert graph_server_cmd.run(args) == 2
    assert "error: scope must be inside root" in capsys.readouterr().out


def test_graph_server_scope_rejects_task_selection_outside_scope(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def scoped():\n    return 1\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "b.py").write_text("def outside():\n    return 2\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
        scope="pkg",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _request_raw_status(
            f"{base_url}/api/agent-task-preflight",
            json.dumps(
                {
                    "agent_name": "Codex",
                    "message": "Try to start outside the scoped package.",
                    "selected_paths": ["other/b.py"],
                }
            ).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        assert status == 400
        assert json.loads(body)["error"] == (
            "selected path is outside scope: other/b.py"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_preflight_returns_edit_policy_in_draft(
    tmp_path: Path, capsys, monkeypatch
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )

    with _make_server(tmp_path, capsys, monkeypatch) as server:
        resp = server.post_json(
            "/api/agent-task-preflight",
            {
                "message": "do it",
                "selected_paths": [],
                "edit_policy": "direct_edit",
                "provider": "codex",
            },
        )

    assert resp["draft"]["edit_policy"] == "direct_edit"


def test_preflight_selected_symbols_threaded_to_draft(
    tmp_path: Path, capsys, monkeypatch
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    sym = {
        "symbol_uid": "uid1",
        "canonical_name": "pkg.a.value",
        "kind": "function",
        "def_file": "pkg/a.py",
        "def_line": 1,
    }

    with _make_server(tmp_path, capsys, monkeypatch) as server:
        resp = server.post_json(
            "/api/agent-task-preflight",
            {
                "message": "review",
                "selected_symbols": [sym],
                "selected_paths": ["pkg/a.py"],
            },
        )

    symbols = resp["draft"].get("selected_symbols", [])
    assert any(symbol["canonical_name"] == "pkg.a.value" for symbol in symbols)


def test_preflight_returns_context_preview_for_selected_path(
    tmp_path: Path, capsys, monkeypatch
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "demo.py").write_text(
        "def hello():\n    return 'hi'\n",
        encoding="utf-8",
    )

    with _make_server(tmp_path, capsys, monkeypatch) as server:
        server.reindex()
        resp = server.post_json(
            "/api/agent-task-preflight",
            {
                "message": "review",
                "selected_paths": ["pkg/demo.py"],
                "edit_policy": "review_before_edit",
            },
        )

    preview = resp["draft"].get("context_preview", {})
    assert preview.get("selected_file") == "pkg/demo.py"


def test_symbols_endpoint_returns_function_hit(
    tmp_path: Path, capsys, monkeypatch
):
    (tmp_path / "mymod.py").write_text(
        "def build_context_packet(x):\n    return x\n",
        encoding="utf-8",
    )

    with _make_server(tmp_path, capsys, monkeypatch) as server:
        server.reindex()
        resp = server.get_json(
            "/api/symbols?q=build_context_packet&kind=function&limit=5"
        )

    assert resp["kind"] == "symbol_results"
    hits = resp["results"]
    assert any(
        hit["canonical_name"].endswith("build_context_packet")
        for hit in hits
    )
    first = hits[0]
    assert "symbol_uid" in first
    assert "def_file" in first
    assert "def_line" in first


def test_symbols_endpoint_requires_q_param(
    tmp_path: Path, capsys, monkeypatch
):
    (tmp_path / "mymod.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )

    with _make_server(tmp_path, capsys, monkeypatch) as server:
        resp = server.get_json("/api/symbols", expect_status=400)

    assert resp["error"]


def test_graph_server_serves_graph_and_records_notes_and_events(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        textwrap.dedent(
            """
            def value() -> int:
                return 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=["pkg/a.py"],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        graph = _request_json(f"{base_url}/repo-graph.json")
        assert graph["live"]["server"] is True
        assert graph["live"]["events_path"] == "/events"
        assert graph["live"]["search_path"] == "/api/search"
        assert graph["live"]["agent_preflight_path"] == "/api/agent-task-preflight"
        assert graph["live"]["agent_runs_path"] == "/api/agent-runs"
        assert graph["live"]["agent_providers_path"] == "/api/agent-providers"
        assert graph["live"]["agent_runtime"]["dispatch"]["provider_presets"]
        live_providers_by_id = {
            provider["id"]: provider for provider in graph["live"]["agent_providers"]
        }
        assert live_providers_by_id["codex"]["display_name"] == "Codex"
        assert graph["agent"]["active_files"] == ["pkg/a.py"]
        assert "file:pkg/a.py" in {node["id"] for node in graph["nodes"]}

        providers = _request_json(f"{base_url}/api/agent-providers")
        assert providers["kind"] == "code_index_agent_provider_registry"
        assert providers["runtime"]["dispatch"]["provider_presets"]
        providers_by_id = {
            provider["id"]: provider for provider in providers["providers"]
        }
        assert providers_by_id["codex"]["display_name"] == "Codex"
        assert "stream_json_output" in providers_by_id["kimi"]["capabilities"]

        debug = _request_json(f"{base_url}/api/debug")
        assert debug["kind"] == "code_index_graph_debug"
        assert debug["graph"]["file_count"] == graph["summary"]["file_count"]
        assert debug["graph"]["payload_bytes"] > 0
        assert debug["graph"]["build_ms"] >= 0
        assert debug["server"]["live"] is True
        assert debug["activity"]["active_file_count"] == 1

        saved = _request_json(
            f"{base_url}/api/notes",
            {
                "node_id": "file:pkg/a.py",
                "path": "pkg/a.py",
                "node_kind": "file",
                "care_level": "medium",
                "summary": "a.py",
                "note": "Review this node before editing.",
            },
        )
        assert saved["ok"] is True
        assert saved["note"]["node_id"] == "file:pkg/a.py"
        assert graph_notes_block(tmp_path)["by_node"]["file:pkg/a.py"]["note"] == (
            "Review this node before editing."
        )

        orphan = _request_json(
            f"{base_url}/api/agent-events",
            {
                "agent_name": "Codex",
                "event_type": "tool",
                "message": "anonymous stderr should not create a fake run",
            },
        )
        assert orphan["ok"] is True
        assert orphan["ignored"] is True
        assert orphan["run"] is None
        conn = db_mod.connect(config.db_path)
        try:
            assert agent_activity.latest_active_run(conn, agent_name="Codex") is None
        finally:
            db_mod.close(conn)

        event = _request_json(
            f"{base_url}/api/agent-events",
            {
                "agent_name": "Codex",
                "event_type": "edit",
                "file_path": "pkg/a.py",
                "message": "Editing a.py",
                "prompt": "Direct graph event",
            },
        )
        assert event["ok"] is True
        assert event["run"]["status"] == "working"
        claims = _request_json(f"{base_url}/api/file-claims")
        assert claims["count"] == 1
        assert claims["active_claims"][0]["file_path"] == "pkg/a.py"
        assert claims["active_claims"][0]["mode"] == "edit"
        preflight = _request_json(
            f"{base_url}/api/agent-task-preflight",
            {
                "agent_name": "Codex",
                "message": "Update the value function.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
            },
        )
        assert preflight["ok"] is True
        assert preflight["kind"] == "code_index_graph_agent_task_preflight"
        assert preflight["draft"]["kind"] == "code_index_agent_task_draft"
        assert preflight["draft"]["graph_context"]["kind"] == "code_index_graph_context"
        assert preflight["preflight"]["requires_confirmation"] is True
        assert preflight["preflight"]["overlapping_claims"][0]["file_path"] == "pkg/a.py"
        assert preflight["dispatch_path"] == "/api/agent-runs"
        conn = db_mod.connect(config.db_path)
        try:
            recent = agent_activity.recent_file_activity(conn, limit=1)
        finally:
            db_mod.close(conn)
        assert recent[0]["file_path"] == "pkg/a.py"

        task = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Update the value function.",
                    "selected_nodes": ["file:pkg/a.py"],
                    "selected_paths": ["pkg/a.py"],
                    "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
                    "parent_run_id": event["run"]["run_id"],
                    "run_context": {"recent_events": [{"event_type": "edit"}]},
                },
            ),
        )
        assert task["ok"] is True
        assert task["run"]["status"] == "queued"
        assert task["run"]["prompt"] == "Update the value function."
        assert task["dispatch"]["configured"] is False
        task_claims = _request_json(f"{base_url}/api/file-claims")
        assert any(
            claim["run_id"] == task["run"]["run_id"]
            and claim["file_path"] == "pkg/a.py"
            and claim["mode"] == "review"
            for claim in task_claims["active_claims"]
        )
        assert task["task"]["callback"]["agent_events_url"].endswith(
            "/api/agent-events"
        )
        assert task["task"]["parent_run_id"] == event["run"]["run_id"]
        assert task["task"]["run_context"]["recent_events"][0]["event_type"] == "edit"
        assert task["run"]["metadata"]["parent_run_id"] == event["run"]["run_id"]
        assert task["task"]["context_packet"]["kind"] == "code_index_context_packet"
        assert task["task"]["context_packet"]["selected_paths"][0]["path"] == "pkg/a.py"
        graph_context = task["task"]["graph_context"]
        assert graph_context["kind"] == "code_index_graph_context"
        assert graph_context["selected_nodes"][0]["path"] == "pkg/a.py"
        assert task["task"]["context_packet"]["graph_context"] == graph_context
        collaboration = task["task"]["collaboration"]
        assert collaboration["kind"] == "code_index_agent_collaboration"
        assert collaboration["mailbox"]["global_events_jsonl"] == (
            ".code_index/agent-runs/events.jsonl"
        )
        assert task["task"]["context_packet"]["collaboration"] == collaboration
        assert any(
            peer["run_id"] == event["run"]["run_id"]
            and peer["overlap_files"] == ["pkg/a.py"]
            for peer in collaboration["active_peer_runs"]
        )
        assert any(
            claim["run_id"] == event["run"]["run_id"]
            and claim["file_path"] == "pkg/a.py"
            for claim in collaboration["overlapping_file_claims"]
        )
        global_jsonl = tmp_path / ".code_index" / "agent-runs" / "events.jsonl"
        assert global_jsonl.exists()
        global_events = [
            json.loads(line)
            for line in global_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(item["run_id"] == task["run"]["run_id"] for item in global_events)
        stream_payload = _agent_stream_payload(config)
        assert stream_payload["type"] == "agent"
        assert stream_payload["agent"]["active_runs"]
        assert stream_payload["agent"]["active_claims"]

        cancelled = _request_json(
            f"{base_url}/api/agent-runs/{task['run']['run_id']}/cancel",
            {},
        )
        assert cancelled["ok"] is True
        assert cancelled["run"]["status"] == "cancelled"
        claims_after_cancel = _request_json(f"{base_url}/api/file-claims")
        assert all(
            claim["run_id"] != task["run"]["run_id"]
            for claim in claims_after_cancel["active_claims"]
        )
        archived = _request_json(
            f"{base_url}/api/agent-runs/{task['run']['run_id']}/archive",
            {},
        )
        assert archived["ok"] is True
        assert archived["run"]["archived_at"]
        stream_after_cancel = _agent_stream_payload(config)
        recent_run_ids = {
            run["run_id"] for run in stream_after_cancel["agent"]["recent_runs"]
        }
        assert task["run"]["run_id"] not in recent_run_ids

        ended = _request_json(
            f"{base_url}/api/agent-events",
            {
                "agent_name": "Codex",
                "event_type": "status",
                "message": "Finished the requested work.",
                "status": "completed",
            },
        )
        assert ended["ok"] is True
        assert ended["run"]["status"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_exposes_review_queue_in_all_agent_board_payloads(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        review_run = agent_activity.start_run(
            conn,
            agent_name="Kimi",
            prompt="Implementation ready for user review.",
            status="working",
        )
        agent_activity.end_run(conn, run_id=review_run["run_id"], status="review")
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        graph = _request_json(f"{base_url}/repo-graph.json")
        graph_review = graph["agent"]["orchestrator"]["review_runs"]
        assert [run["run_id"] for run in graph_review] == [review_run["run_id"]]
        assert graph_review[0]["run_health"]["health"] == "needs_review"
        assert (
            graph["agent"]["kanban"]["columns"]["review"]["runs"][0]["run_health"][
                "health"
            ]
            == "needs_review"
        )

        stream = _agent_stream_payload(config)
        assert [
            run["run_id"]
            for run in stream["agent"]["orchestrator"]["review_runs"]
        ] == [review_run["run_id"]]

        board = _request_json(f"{base_url}/api/agent-board")
        assert [run["run_id"] for run in board["orchestrator"]["review_runs"]] == [
            review_run["run_id"]
        ]
        assert (
            board["columns"]["review"]["runs"][0]["run_health"]["health"]
            == "needs_review"
        )

        task = _request_json(
            f"{base_url}/api/agent-runs",
            {"agent_name": "Codex", "message": "Queue a follow-up task."},
        )
        assert task["ok"] is True
        assert [
            run["run_id"]
            for run in task["board"]["orchestrator"]["review_runs"]
        ] == [review_run["run_id"]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_accepts_review_run_and_clears_review_queue(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        review_run = agent_activity.start_run(
            conn,
            agent_name="Kimi",
            prompt="Implementation ready for user review.",
            status="working",
        )
        agent_activity.record_event(
            conn,
            run_id=review_run["run_id"],
            event_type="edit",
            file_path="pkg/a.py",
            message="Updated value implementation.",
        )
        agent_activity.record_event(
            conn,
            run_id=review_run["run_id"],
            event_type="status",
            file_path="pkg/a.py",
            message="Ready for review.",
            payload={"status": "review", "changed_files": ["pkg/a.py"]},
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        board = _request_json(f"{base_url}/api/agent-board")
        assert [run["run_id"] for run in board["orchestrator"]["review_runs"]] == [
            review_run["run_id"]
        ]

        accepted = _request_json(
            f"{base_url}/api/agent-runs/{review_run['run_id']}/accept-review",
            {"decision": "Reviewed the changed files and accepted the work."},
        )

        assert accepted["ok"] is True
        assert accepted["run"]["status"] == "completed"
        assert accepted["event"]["event_type"] == "decision"
        assert accepted["event"]["payload"]["status"] == "accepted"
        assert accepted["event"]["payload"]["review_action"] == "accepted"
        assert [
            run["run_id"]
            for run in accepted["board"]["orchestrator"]["review_runs"]
        ] == []

        transcript = _request_json(
            f"{base_url}/api/agent-runs/{review_run['run_id']}"
        )
        assert transcript["run"]["status"] == "completed"
        assert transcript["changed_files"] == ["pkg/a.py"]
        assert transcript["decisions"][-1]["payload"]["status"] == "accepted"

        board_after = _request_json(f"{base_url}/api/agent-board")
        assert [
            run["run_id"]
            for run in board_after["orchestrator"]["review_runs"]
        ] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_stream_reconciles_dead_local_process_before_payload(
    tmp_path: Path, capsys
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        run = agent_activity.start_run(
            conn,
            agent_name="Kimi",
            prompt="Local command already exited.",
            status="working",
        )
        process = run_lifecycle.register_process(
            conn,
            run_id=run["run_id"],
            transport="local-command",
            provider="kimi",
            pid=123456,
            command_label="kimi --quiet",
        )
        run_lifecycle.finish_process(
            conn,
            process_id=process["process_id"],
            status="completed",
            exit_code=0,
        )
    finally:
        db_mod.close(conn)

    stream = _agent_stream_payload(config)

    assert all(
        item["run_id"] != run["run_id"]
        for item in stream["agent"]["active_runs"]
    )
    assert [item["run_id"] for item in stream["agent"]["orchestrator"]["review_runs"]] == [
        run["run_id"]
    ]
    conn = db_mod.connect(config.db_path)
    try:
        updated = agent_activity.get_run(conn, run["run_id"])
    finally:
        db_mod.close(conn)
    assert updated is not None
    assert updated["status"] == "review"


def test_graph_server_starts_agent_swarm_as_child_runs(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        result = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "provider": "custom",
                    "execution_strategy": "agent_swarm",
                    "message": "Run a coordinated implementation swarm.",
                    "selected_nodes": ["file:pkg/a.py"],
                    "selected_paths": ["pkg/a.py"],
                    "node": {
                        "id": "file:pkg/a.py",
                        "path": "pkg/a.py",
                        "kind": "file",
                    },
                    "swarm": {
                        "enabled": True,
                        "provider": "custom",
                        "size": 2,
                        "roles": ["coordinator", "implementer"],
                    },
                },
            ),
        )
        assert result["ok"] is True
        assert result["run"]["metadata"]["execution_strategy"] == "swarm"
        assert result["run"]["metadata"]["swarm"]["role"] == "lead"
        assert (
            result["run"]["metadata"]["swarm"]["completion_policy"]
            == "all_children_terminal"
        )
        assert result["run"]["status"] == "working"
        assert result["task"]["execution_strategy"] == "swarm"
        assert result["task"]["swarm"]["provider"] == "custom"
        assert any(
            child["role"] == "implementer" and child["provider"] == "custom"
            for child in result["task"]["swarm_children"]
        )
        assert [child["role"] for child in result["task"]["swarm_children"]] == [
            "coordinator",
            "implementer",
        ]
        assert result["dispatch"]["transport"] == "swarm"
        assert result["dispatch"]["status"] == "not_configured"
        assert len(result["dispatch"]["children"]) == 2

        conn = db_mod.connect(config.db_path)
        try:
            child_runs = [
                run
                for run in agent_activity.recent_runs(conn, limit=10)
                if (run.get("metadata") or {}).get("parent_run_id")
                == result["run"]["run_id"]
            ]
        finally:
            db_mod.close(conn)
        assert len(child_runs) == 2
        assert {run["status"] for run in child_runs} == {"queued"}
        assert {
            (run.get("metadata") or {}).get("swarm", {}).get("role")
            for run in child_runs
        } == {"coordinator", "implementer"}
        conn = db_mod.connect(config.db_path)
        try:
            claims = agent_activity.active_file_claims(
                conn,
                file_path="pkg/a.py",
                limit=20,
            )
        finally:
            db_mod.close(conn)
        modes_by_agent = {claim["agent_name"]: claim["mode"] for claim in claims}
        assert any(mode == "edit" for mode in modes_by_agent.values())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_reconciles_swarm_parent_when_children_stop(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        result = _request_json(
            f"{base_url}/api/agent-runs",
            {
                "provider": "custom",
                "execution_strategy": "agent_swarm",
                "message": "Run a completion-only swarm.",
                "swarm": {
                    "enabled": True,
                    "provider": "custom",
                    "size": 2,
                    "roles": ["coordinator", "reviewer"],
                },
            },
        )
        children = result["task"]["swarm_children"]
        assert len(children) == 2

        first = _request_json(
            f"{base_url}/api/agent-events",
            {
                "run_id": children[0]["run_id"],
                "event_type": "status",
                "message": "Coordinator done.",
                "status": "completed",
            },
        )
        assert first["swarm_reconciliation"]["changed"] is False
        conn = db_mod.connect(config.db_path)
        try:
            parent = agent_activity.get_run(conn, result["run"]["run_id"])
        finally:
            db_mod.close(conn)
        assert parent["status"] == "working"

        second = _request_json(
            f"{base_url}/api/agent-events",
            {
                "run_id": children[1]["run_id"],
                "event_type": "status",
                "message": "Reviewer done.",
                "status": "completed",
            },
        )

        assert second["swarm_reconciliation"]["changed"] is True
        assert second["swarm_reconciliation"]["status"] == "review"
        conn = db_mod.connect(config.db_path)
        try:
            parent = agent_activity.get_run(conn, result["run"]["run_id"])
        finally:
            db_mod.close(conn)
        assert parent["status"] == "review"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_rolls_back_swarm_start_when_child_claims_conflict(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = _preflight_task_payload(
            base_url,
            {
                "provider": "custom",
                "execution_strategy": "agent_swarm",
                "message": "Start a conflicting edit swarm.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
                "swarm": {
                    "enabled": True,
                    "provider": "custom",
                    "size": 2,
                    "roles": ["implementer", "implementer"],
                },
            },
        )

        assert _request_status(f"{base_url}/api/agent-runs", payload) == 409

        conn = db_mod.connect(config.db_path)
        try:
            assert agent_activity.recent_runs(conn, limit=10) == []
            assert agent_activity.active_file_claims(conn, limit=10) == []
        finally:
            db_mod.close(conn)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_streams_perf_tick_with_sanitized_counters(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_GRAPH_TOKEN", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "def value():\n    return 'perf tick needle'\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=0.1,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        search = _request_json(f"{base_url}/api/search?q=needle&scope=all&limit=5")
        assert search["ok"] is True

        tick = _read_sse_event(f"{base_url}/events", "perf:tick")
        assert tick["kind"] == "code_index_graph_debug_perf"
        assert tick["type"] == "perf:tick"
        assert tick["generated_at"]
        assert tick["counters"]["search_latency_ms"]["count"] == 1
        assert tick["counters"]["retrieval_budget"]["broker_configured"] is True
        assert tick["counters"]["retrieval_budget"]["requests"] == 1

        serialized = json.dumps(tick, sort_keys=True)
        assert "fence_token" not in serialized
        assert "lease_token" not in serialized
        assert "bearer_token" not in serialized
        assert "session_cookie" not in serialized
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_renews_and_releases_file_claim(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODE_INDEX_GRAPH_TOKEN", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="endpoint test",
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=0.1,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        tokenless_release = _request_json(
            f"{base_url}/api/file-claims",
            {
                "action": "release",
                "run_id": lease["claim"]["run_id"],
                "file_paths": ["pkg/a.py"],
                "mode": "edit",
            },
        )
        assert tokenless_release["ok"] is True
        assert tokenless_release["claims"] == []
        assert any(
            claim["claim_id"] == lease["claim"]["claim_id"]
            for claim in tokenless_release["active_claims"]
        )

        malformed_renew_status = _request_status(
            f"{base_url}/api/file-claims/{lease['claim']['claim_id']}/renew",
            {
                "lease_token": lease["lease_token"],
                "fence_token": "abc-fence-secret",
            },
        )
        assert malformed_renew_status == 400
        conn = db_mod.connect(config.db_path)
        try:
            events = lease_manager.claim_events(
                conn,
                claim_id=lease["claim"]["claim_id"],
            )
        finally:
            db_mod.close(conn)
        assert [event["event_type"] for event in events] == ["created", "denied"]
        assert events[-1]["metadata"] == {"reason": "bad_fence"}
        serialized_events = json.dumps(events, sort_keys=True)
        assert lease["lease_token"] not in serialized_events
        assert "abc-fence-secret" not in serialized_events

        renewed = _request_json(
            f"{base_url}/api/file-claims/{lease['claim']['claim_id']}/renew",
            {
                "lease_token": lease["lease_token"],
                "fence_token": lease["claim"]["fence_token"],
            },
        )
        released = _request_json(
            f"{base_url}/api/file-claims/{lease['claim']['claim_id']}/release",
            {"lease_token": lease["lease_token"]},
        )

        assert renewed["ok"] is True
        assert renewed["claim"]["claim_id"] == lease["claim"]["claim_id"]
        assert released["claim"]["status"] == "released"
        assert "lease_token" not in json.dumps(released, sort_keys=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_latency_observer_repairs_malformed_scoped_counters():
    perf = _make_perf_state()
    latency = perf["counters"]["search_latency_ms"]

    latency["by_scope"] = "unexpected"
    _observe_latency(perf, "search_latency_ms", 12.5, "all")
    assert latency["by_scope"]["all"]["count"] == 1
    assert latency["by_scope"]["all"]["avg"] == 12.5

    latency["by_scope"]["all"] = "unexpected"
    _observe_latency(perf, "search_latency_ms", 17.5, "all")
    assert latency["count"] == 2
    assert latency["avg"] == 15.0
    assert latency["by_scope"]["all"]["count"] == 1
    assert latency["by_scope"]["all"]["avg"] == 17.5


def test_graph_server_rejects_non_object_json_posts(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODE_INDEX_GRAPH_TOKEN", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _request_raw_status(
            f"{base_url}/api/agent-events",
            b"[]",
            {"Content-Type": "application/json"},
        )
        assert status == 400
        assert json.loads(body)["error"] == "JSON body must be an object"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_get_agent_run_returns_transcript(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Inspect transcript endpoint.",
                    "selected_paths": ["pkg/a.py"],
                },
            ),
        )
        run_id = task["run"]["run_id"]
        decision = _request_json(
            f"{base_url}/api/agent-events",
            {
                "run_id": run_id,
                "agent_name": "Codex",
                "event_type": "decision",
                "message": "Expose run transcript over GET.",
                "payload": {
                    "decision": "Expose run transcript over GET.",
                    "rationale": "The graph UI needs a read-only inspector.",
                    "status": "accepted",
                },
            },
        )
        assert decision["ok"] is True

        transcript = _request_json(f"{base_url}/api/agent-runs/{run_id}")
        assert transcript["run"]["run_id"] == run_id
        assert [event["event_type"] for event in transcript["events"]] == [
            "task",
            "decision",
        ]
        assert transcript["active_files"] == ["pkg/a.py"]
        assert transcript["decisions"][0]["payload"]["status"] == "accepted"
        assert transcript["summary"]["decision_count"] == 1
        assert _request_get_status(f"{base_url}/api/agent-runs/unknown-run") == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_posts_followup_message_to_existing_agent_run(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(
            conn,
            agent_name="OpenCode",
            prompt="Initial agent turn.",
            selected_nodes=["file:pkg/a.py"],
            metadata={"provider": "opencode", "selected_paths": ["pkg/a.py"]},
            status="completed",
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = _preflight_task_payload(
            base_url,
            {
                "provider": "opencode",
                "agent_name": "OpenCode",
                "message": "Continue in this same session.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
                "parent_run_id": run["run_id"],
            },
        )
        result = _request_json(
            f"{base_url}/api/agent-runs/{run['run_id']}/messages",
            payload,
        )
        assert result["ok"] is True
        assert result["same_run"] is True
        assert result["run"]["run_id"] == run["run_id"]
        assert result["run"]["status"] == "working"
        assert result["event"]["run_id"] == run["run_id"]
        assert result["event"]["message"] == "Continue in this same session."
        assert result["task"]["run_id"] == run["run_id"]
        assert result["task"]["kind"] == "code_index_agent_run_message"
        assert result["task"]["provider"] == "opencode"

        transcript = _request_json(f"{base_url}/api/agent-runs/{run['run_id']}")
        assert transcript["run"]["run_id"] == run["run_id"]
        assert transcript["run"]["status"] == "working"
        assert [event["message"] for event in transcript["events"]] == [
            "Continue in this same session."
        ]
        assert transcript["active_files"] == ["pkg/a.py"]

        conn = db_mod.connect(config.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        finally:
            db_mod.close(conn)
        assert count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_requires_and_consumes_preflight_for_graph_tasks(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {
            "agent_name": "Codex",
            "message": "Preflight this task.",
            "selected_nodes": ["file:pkg/a.py"],
            "selected_paths": ["pkg/a.py"],
            "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
        }
        assert _request_status(f"{base_url}/api/agent-runs", payload) == 428
        preflighted = _preflight_task_payload(base_url, payload)
        first = _request_json(f"{base_url}/api/agent-runs", preflighted)
        assert first["ok"] is True
        assert _request_status(f"{base_url}/api/agent-runs", preflighted) == 409

        perf = _request_json(f"{base_url}/api/debug/perf")
        assert perf["counters"]["preflight_rejections"][
            "preflight_id is required for graph-scoped agent runs"
        ] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_rejects_preflight_when_execution_strategy_is_tampered(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {
            "provider": "custom",
            "agent_name": "Codex",
            "message": "Preflight this as a single task.",
            "selected_nodes": ["file:pkg/a.py"],
            "selected_paths": ["pkg/a.py"],
            "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
            "execution_strategy": "single",
        }
        preflighted = _preflight_task_payload(base_url, payload)
        tampered = dict(preflighted)
        tampered["execution_strategy"] = "agent_swarm"
        tampered["swarm"] = {
            "enabled": True,
            "provider": "custom",
            "size": 2,
            "roles": ["coordinator", "tester"],
        }

        assert _request_status(f"{base_url}/api/agent-runs", tampered) == 412
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_rejects_preflight_when_swarm_shape_is_tampered(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {
            "provider": "custom",
            "agent_name": "Codex",
            "message": "Preflight this as a swarm task.",
            "selected_nodes": ["file:pkg/a.py"],
            "selected_paths": ["pkg/a.py"],
            "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
            "execution_strategy": "agent_swarm",
            "swarm": {
                "enabled": True,
                "provider": "custom",
                "size": 2,
                "roles": ["coordinator", "tester"],
            },
        }
        preflighted = _preflight_task_payload(base_url, payload)
        tampered = dict(preflighted)
        tampered["swarm"] = {
            "enabled": True,
            "provider": "custom",
            "size": 3,
            "roles": ["coordinator", "implementer", "tester"],
        }

        assert _request_status(f"{base_url}/api/agent-runs", tampered) == 412
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_mcp_graph_context_matches_http_handles_and_enforces_budgets(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text(
        "def helper() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "a.py").write_text(
        "from pkg.b import helper\n\n\ndef value() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        http_preflight = _request_json(
            f"{base_url}/api/agent-task-preflight",
            {
                "agent_name": "Codex",
                "message": "Use graph context.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
            },
        )
        http_context = http_preflight["draft"]["graph_context"]
        mcp_context = mcp_tool_impl._tool_graph_context(
            config,
            selected_nodes=["file:pkg/a.py"],
            selected_paths=["pkg/a.py"],
            node={"id": "file:pkg/a.py", "path": "pkg/a.py", "kind": "file"},
            agent_name="Codex",
        )

        assert http_context["kind"] == "code_index_graph_context"
        assert mcp_context["kind"] == "code_index_graph_context"
        assert [n["stable_id"] for n in http_context["selected_nodes"]] == [
            n["stable_id"] for n in mcp_context["selected_nodes"]
        ]
        assert [n["stable_id"] for n in http_context["related_nodes"]] == [
            n["stable_id"] for n in mcp_context["related_nodes"]
        ]

        allowed_why = set(mcp_context["why_included_values"])
        assert allowed_why
        for item in mcp_context["nodes"]:
            assert item["stable_id"]
            assert item["layer"] in {"selected", "related"}
            assert isinstance(item["distance"], int)
            assert isinstance(item["relation_path"], list)
            assert item["risk"]["level"]
            assert item["byte_cost"] > 0
            assert item["why_included"] in allowed_why

        node_limited = mcp_tool_impl._tool_graph_context(
            config,
            selected_nodes=["file:pkg/a.py"],
            selected_paths=["pkg/a.py"],
            agent_name="Codex",
            max_nodes=1,
            max_bytes=24_000,
        )
        assert node_limited["budget"]["used_nodes"] <= 1
        assert len(node_limited["nodes"]) <= 1
        assert node_limited["budget"]["truncated_nodes"] is True

        byte_limited = mcp_tool_impl._tool_graph_context(
            config,
            selected_nodes=["file:pkg/a.py"],
            selected_paths=["pkg/a.py"],
            agent_name="Codex",
            max_nodes=24,
            max_bytes=0,
        )
        assert byte_limited["budget"]["used_bytes"] == 0
        assert byte_limited["nodes"] == []
        assert byte_limited["budget"]["truncated_bytes"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_keeps_blocked_task_on_kanban_board_until_blocker_completes(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        blocker = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Slice 1 tracer bullet",
            status="working",
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {
            "agent_name": "Claude",
            "message": "Slice 2 waits for slice 1.",
            "selected_paths": ["pkg/a.py"],
            "blocked_by_run_ids": [blocker["run_id"]],
            "slice": {"type": "AFK", "title": "Slice 2"},
        }
        preflighted = _preflight_task_payload(base_url, payload)
        assert preflighted["preflight"]["status"] == "blocked"
        assert preflighted["preflight"]["can_dispatch"] is False
        assert preflighted["preflight"]["blocking_runs"][0]["run_id"] == (
            blocker["run_id"]
        )

        task = _request_json(f"{base_url}/api/agent-runs", preflighted)
        assert task["ok"] is True
        assert task["run"]["status"] == "blocked"
        assert task["dispatch"]["status"] == "blocked"
        assert task["task"]["blocked_by_run_ids"] == [blocker["run_id"]]

        board = _request_json(f"{base_url}/api/agent-board")
        assert [run["run_id"] for run in board["columns"]["blocked"]["runs"]] == [
            task["run"]["run_id"]
        ]

        completed = _request_json(
            f"{base_url}/api/agent-events",
            {
                "run_id": blocker["run_id"],
                "agent_name": "Codex",
                "event_type": "status",
                "message": "Slice 1 passed.",
                "status": "completed",
            },
        )
        assert completed["ok"] is True

        board = _request_json(f"{base_url}/api/agent-board")
        ready_ids = {run["run_id"] for run in board["columns"]["ready"]["runs"]}
        assert task["run"]["run_id"] in ready_ids
        assert all(
            run["run_id"] != task["run"]["run_id"]
            for run in board["columns"]["blocked"]["runs"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_token_protects_get_routes_and_searches_files_and_transcripts(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("CODE_INDEX_GRAPH_TOKEN", "secret-token")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "def value():\n    return 'needle graph search'\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        run = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Investigate transcript needle",
            selected_nodes=["file:pkg/a.py"],
        )
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="tool",
            file_path="pkg/a.py",
            message="transcript needle found in adapter output",
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    auth = {"Authorization": "Bearer secret-token"}
    try:
        assert _request_get_status(f"{base_url}/repo-graph.json") == 401
        assert _request_get_status(f"{base_url}/api/debug") == 401
        assert _request_status(
            f"{base_url}/api/agent-task-preflight",
            {"message": "blocked without token"},
        ) == 401

        graph = _request_json(f"{base_url}/repo-graph.json", headers=auth)
        assert graph["kind"] == "code_index_graph"
        assert (
            _request_get_status(f"{base_url}/repo-graph.json?token=secret-token")
            == 401
        )
        auth_page = _request_text(f"{base_url}/repo-graph.html?token=secret-token")
        assert "Graph server token" in auth_page
        assert "graph-data" not in auth_page

        session, session_headers = _request_json_with_headers(
            f"{base_url}/api/auth/browser-session",
            {},
            headers=auth,
        )
        assert session["ok"] is True
        assert session["auth"] == "browser-session-cookie"
        cookie = session_headers["Set-Cookie"].split(";", 1)[0]
        cookie_auth = {"Cookie": cookie}

        html_status = _request_get_status(f"{base_url}/repo-graph.html", cookie_auth)
        assert html_status == 200

        debug = _request_json(f"{base_url}/api/debug", headers=cookie_auth)
        assert debug["kind"] == "code_index_graph_debug"

        search = _request_json(
            f"{base_url}/api/search?q=needle&scope=all&limit=5",
            headers=cookie_auth,
        )
        assert search["kind"] == "code_index_graph_search"
        assert any(result["file_path"] == "pkg/a.py" for result in search["files"])
        assert any(
            result["run_id"] == run["run_id"]
            and "transcript needle" in result["message"]
            for result in search["transcripts"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_debug_ops_snapshot_is_sanitized_and_actionable(
    tmp_path: Path, capsys, monkeypatch
):
    graph_token = "secret-token-debug-ops"
    preflight_secret = "secret-preflight-debug-ops"
    webhook_secret = "https://ops.example.test/hook?secret=debug-webhook-secret"
    command_secret = "runner --api-key debug-command-secret"
    monkeypatch.setenv("CODE_INDEX_GRAPH_TOKEN", graph_token)
    monkeypatch.setenv("CODE_INDEX_GRAPH_PREFLIGHT_SECRET", preflight_secret)
    monkeypatch.setenv("CODE_INDEX_AGENT_WEBHOOK_URL", webhook_secret)
    monkeypatch.setenv("CODE_INDEX_AGENT_COMMAND", command_secret)
    monkeypatch.setenv("CODE_INDEX_GRAPH_STALE_RUN_SECONDS", "1")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "def value():\n    return 'debug ops needle'\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        holder = agent_activity.start_run(
            conn,
            agent_name="Codex",
            prompt="Hold a claim for sanitized debug.",
            status="working",
        )
        claim = agent_activity.claim_file(
            conn,
            run_id=holder["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="Claim should be visible without its fence token.",
            metadata={"lease_token": "debug-lease-token-secret"},
        )
        fence_secret = 987654321
        conn.execute(
            "UPDATE agent_file_claims SET fence_token = ? WHERE claim_id = ?",
            (fence_secret, claim["claim_id"]),
        )
        contender = agent_activity.start_run(
            conn,
            agent_name="Claude",
            prompt="Conflict with the held claim.",
            status="working",
        )
        stale_at = "2001-01-01T00:00:00.000+00:00"
        conn.execute(
            "UPDATE agent_runs SET updated_at = ?, started_at = ? WHERE run_id = ?",
            (stale_at, stale_at, holder["run_id"]),
        )
    finally:
        db_mod.close(conn)

    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    auth = {"Authorization": f"Bearer {graph_token}"}
    try:
        assert _request_get_status(f"{base_url}/api/debug") == 401
        assert (
            _request_status(
                f"{base_url}/api/agent-runs",
                {"message": "missing preflight", "selected_paths": ["pkg/a.py"]},
                headers=auth,
            )
            == 428
        )
        assert (
            _request_status(
                f"{base_url}/api/file-claims",
                {
                    "run_id": contender["run_id"],
                    "file_paths": ["pkg/a.py"],
                    "mode": "edit",
                },
                headers=auth,
            )
            == 409
        )
        search = _request_json(
            f"{base_url}/api/search?q=needle&scope=all&limit=5",
            headers=auth,
        )
        assert search["ok"] is True

        debug = _request_json(f"{base_url}/api/debug", headers=auth)
        assert debug["ops"]["auth"]["failures"]["/api/debug"] == 1
        assert debug["ops"]["preflight"]["rejections"][
            "preflight_id is required for graph-scoped agent runs"
        ] == 1
        assert debug["ops"]["claims"]["conflict_count"] == 1
        assert debug["ops"]["claims"]["active"][0]["file_path"] == "pkg/a.py"
        assert debug["ops"]["runs"]["stale_count"] == 1
        assert debug["perf"]["counters"]["stale_runs"] == 1
        assert debug["ops"]["search"]["latency_ms"]["count"] == 1
        assert debug["ops"]["retrieval_budget"]["placeholder"] is False
        assert debug["ops"]["retrieval_budget"]["broker_configured"] is True
        assert debug["ops"]["retrieval_budget"]["requests"] == 1

        perf = _request_json(f"{base_url}/api/debug/perf", headers=auth)
        assert perf["counters"]["auth_failures"]["/api/debug"] == 1
        assert perf["counters"]["claim_conflicts"] == 1
        assert perf["counters"]["search_latency_ms"]["count"] == 1
        assert perf["counters"]["retrieval_budget"]["broker_configured"] is True
        assert perf["counters"]["retrieval_budget"]["requests"] == 1

        serialized = json.dumps(debug, sort_keys=True)
        for secret in {
            graph_token,
            preflight_secret,
            webhook_secret,
            command_secret,
            "debug-webhook-secret",
            "debug-command-secret",
            "debug-lease-token-secret",
            str(fence_secret),
        }:
            assert secret not in serialized
        assert "fence_token" not in serialized
        assert "lease_token" not in serialized
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_adapter_dry_run_posts_lifecycle_events(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task_response = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Adapter dry run.",
                    "selected_nodes": ["file:pkg/a.py"],
                    "selected_paths": ["pkg/a.py"],
                    "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
                },
            ),
        )
        task_path = tmp_path / "task.json"
        task_path.write_text(json.dumps(task_response["task"]), encoding="utf-8")
        assert main(["agent-adapter", "--root", str(tmp_path), "--task-json", str(task_path), "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "completed"
        assert out["events_sent"] == 4
        conn = db_mod.connect(config.db_path)
        try:
            run = agent_activity.get_run(conn, task_response["run"]["run_id"])
            recent = agent_activity.recent_events(conn, limit=5)
        finally:
            db_mod.close(conn)
        assert run is not None
        assert run["status"] == "completed"
        assert any(event["event_type"] == "read" for event in recent)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_adapter_command_mode_posts_output_and_status(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task_response = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Adapter command run.",
                    "selected_nodes": ["file:pkg/a.py"],
                    "selected_paths": ["pkg/a.py"],
                    "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
                },
            ),
        )
        task_path = tmp_path / "task.json"
        task_path.write_text(json.dumps(task_response["task"]), encoding="utf-8")
        script_path = tmp_path / "adapter_command.py"
        script_path.write_text(
            textwrap.dedent(
                """
                import json
                import sys

                with open(sys.argv[1], encoding="utf-8") as handle:
                    task = json.load(handle)
                with open("pkg/a.py", "a", encoding="utf-8") as handle:
                    handle.write("\\n# touched by adapter\\n")
                with open(sys.argv[2], "w", encoding="utf-8") as handle:
                    handle.write("Final answer: changed pkg/a.py")
                print("agent output: " + task["message"])
                """
            ).lstrip(),
            encoding="utf-8",
        )
        command = f'"{sys.executable}" "{script_path}" {{task_json}} {{last_message}}'
        assert (
            main(
                [
                    "agent-adapter",
                    "--root",
                    str(tmp_path),
                    "--mode",
                    "command",
                    "--task-json",
                    str(task_path),
                    "--command",
                    command,
                    "--json",
                ]
            )
            == 0
        )
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "completed"
        assert out["process_exit_code"] == 0
        assert out["changed_files"] == ["pkg/a.py"]
        conn = db_mod.connect(config.db_path)
        try:
            run = agent_activity.get_run(conn, task_response["run"]["run_id"])
            recent = agent_activity.recent_events(conn, limit=20)
        finally:
            db_mod.close(conn)
        assert run is not None
        assert run["status"] == "completed"
        assert any(
            event["event_type"] == "tool"
            and "agent output: Adapter command run." in event["message"]
            for event in recent
        )
        assert any(
            event["event_type"] == "edit"
            and event["file_path"] == "pkg/a.py"
            for event in recent
        )
        assert any(
            event["event_type"] == "decision"
            and "Final answer: changed pkg/a.py" in event["message"]
            for event in recent
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_adapter_command_mode_failed_exit_marks_run_failed(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task_response = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Adapter command failure.",
                    "selected_paths": ["pkg/a.py"],
                },
            ),
        )
        task_path = tmp_path / "task.json"
        task_path.write_text(json.dumps(task_response["task"]), encoding="utf-8")
        script_path = tmp_path / "adapter_fail.py"
        script_path.write_text(
            'import sys\nprint("agent failed", file=sys.stderr)\nsys.exit(7)\n',
            encoding="utf-8",
        )
        command = f'"{sys.executable}" "{script_path}"'
        assert (
            main(
                [
                    "agent-adapter",
                    "--root",
                    str(tmp_path),
                    "--mode",
                    "command",
                    "--task-json",
                    str(task_path),
                    "--command",
                    command,
                    "--json",
                ]
            )
            == 1
        )
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "failed"
        assert out["process_exit_code"] == 7
        conn = db_mod.connect(config.db_path)
        try:
            run = agent_activity.get_run(conn, task_response["run"]["run_id"])
            recent = agent_activity.recent_events(conn, limit=10)
        finally:
            db_mod.close(conn)
        assert run is not None
        assert run["status"] == "failed"
        assert any("agent failed" in event["message"] for event in recent)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_dispatches_local_command_adapter(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    script_path = tmp_path / "local_adapter.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            with open(sys.argv[1], encoding="utf-8") as handle:
                task = json.load(handle)
            print("local dispatch: " + task["message"])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "CODE_INDEX_AGENT_COMMAND",
        f'"{sys.executable}" "{script_path}" {{task_json}}',
    )

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task_response = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Run from graph UI.",
                    "selected_nodes": ["file:pkg/a.py"],
                    "selected_paths": ["pkg/a.py"],
                    "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
                },
            ),
        )
        assert task_response["dispatch"]["configured"] is True
        assert task_response["dispatch"]["status"] == "started"
        assert task_response["dispatch"]["transport"] == "local-command"
        run = _wait_for_run_status(
            config, task_response["run"]["run_id"], "completed"
        )
        assert run["status"] == "completed"
        conn = db_mod.connect(config.db_path)
        try:
            recent = agent_activity.recent_events(conn, limit=12)
        finally:
            db_mod.close(conn)
        assert any("local dispatch: Run from graph UI." in event["message"] for event in recent)
        process = _wait_for_process_row(config, task_response["run"]["run_id"])
        assert process["transport"] == "local-command"
        assert process["provider"] == task_response["dispatch"]["provider"]
        assert process["status"] == "completed"
        assert process["exit_code"] == 0
        assert isinstance(process["pid"], int)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_routes_payload_provider_to_local_adapter(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    script_path = tmp_path / "provider_adapter.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            with open(sys.argv[1], encoding="utf-8") as handle:
                task = json.load(handle)
            print(f"provider dispatch: {task['provider']} {task['message']}")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setitem(
        agent_adapter_cmd.PROVIDER_COMMANDS,
        "claude",
        f'"{sys.executable}" "{script_path}" {{task_json}}',
    )

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task_response = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "provider": "claude",
                    "message": "Run through selected provider.",
                    "selected_nodes": ["file:pkg/a.py"],
                    "selected_paths": ["pkg/a.py"],
                    "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
                },
            ),
        )
        assert task_response["task"]["provider"] == "claude"
        assert task_response["task"]["agent_name"] == "Claude"
        assert task_response["dispatch"]["provider"] == "claude"
        run = _wait_for_run_status(
            config, task_response["run"]["run_id"], "completed"
        )
        assert run["agent_name"] == "Claude"
        conn = db_mod.connect(config.db_path)
        try:
            recent = agent_activity.recent_events(conn, limit=12)
        finally:
            db_mod.close(conn)
        assert any(
            "provider dispatch: claude Run through selected provider." in event["message"]
            for event in recent
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_cancel_interrupts_local_command_adapter_process(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    script_path = tmp_path / "sleeping_adapter.py"
    started_path = tmp_path / "adapter-started.txt"
    finished_path = tmp_path / "adapter-finished.txt"
    script_path.write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            import json
            import sys
            import time

            with open(sys.argv[1], encoding="utf-8") as handle:
                json.load(handle)
            Path(sys.argv[2]).write_text("started", encoding="utf-8")
            print("adapter sleeping", flush=True)
            time.sleep(2.5)
            Path(sys.argv[3]).write_text("finished", encoding="utf-8")
            print("adapter finished", flush=True)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "CODE_INDEX_AGENT_COMMAND",
        f'"{sys.executable}" "{script_path}" {{task_json}} "{started_path}" "{finished_path}"',
    )

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        task_response = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {
                    "agent_name": "Codex",
                    "message": "Cancel this local adapter.",
                    "selected_paths": ["pkg/a.py"],
                },
            ),
        )
        run_id = task_response["run"]["run_id"]
        _wait_for_path(started_path)
        cancelled = _request_json(
            f"{base_url}/api/agent-runs/{run_id}/cancel",
            {},
        )
        assert cancelled["ok"] is True
        assert cancelled["local_cancel_requested"] is True
        assert cancelled["run"]["status"] == "cancelled"
        _wait_for_event_message(
            config,
            run_id,
            "Command adapter cancelled task and interrupted the process.",
        )
        time.sleep(3.0)
        assert not finished_path.exists()
        run = _wait_for_run_status(config, run_id, "cancelled")
        assert run["status"] == "cancelled"
        process = _wait_for_process_row(config, run_id)
        assert process["status"] == "cancelled"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_graph_server_post_auth_when_token_set(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setenv("CODE_INDEX_GRAPH_TOKEN", "secret-token")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=[],
        agent_name="Codex",
        event_interval=1.0,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _request_status(
            f"{base_url}/api/agent-runs",
            {"message": "auth check", "selected_paths": ["pkg/a.py"]},
        ) == 401
        result = _request_json(
            f"{base_url}/api/agent-runs",
            _preflight_task_payload(
                base_url,
                {"message": "auth check", "selected_paths": ["pkg/a.py"]},
                headers={"Authorization": "Bearer secret-token"},
            ),
            headers={"Authorization": "Bearer secret-token"},
        )
        assert result["ok"] is True
        assert result["run"]["status"] == "queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
