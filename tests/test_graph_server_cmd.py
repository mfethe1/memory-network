"""HTTP coverage for the live graph server."""

from __future__ import annotations

import argparse
import json
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
from code_index.cli import main
from code_index.commands import agent_adapter_cmd
from code_index.commands.graph_notes import graph_notes_block
from code_index.commands.graph_server_cmd import _agent_stream_payload, _make_handler


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


def _request_status(url: str, payload: dict | None = None) -> int:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _request_get_status(url: str, headers: dict | None = None) -> int:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


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


def test_graph_server_serves_graph_and_records_notes_and_events(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
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
        assert graph["agent"]["active_files"] == ["pkg/a.py"]
        assert "file:pkg/a.py" in {node["id"] for node in graph["nodes"]}

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
            {
                "agent_name": "Codex",
                "message": "Update the value function.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
                "parent_run_id": event["run"]["run_id"],
                "run_context": {"recent_events": [{"event_type": "edit"}]},
            },
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
            {
                "agent_name": "Codex",
                "message": "Inspect transcript endpoint.",
                "selected_paths": ["pkg/a.py"],
            },
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
        html_status = _request_get_status(
            f"{base_url}/repo-graph.html?token=secret-token"
        )
        assert html_status == 200

        debug = _request_json(f"{base_url}/api/debug?token=secret-token")
        assert debug["kind"] == "code_index_graph_debug"

        search = _request_json(
            f"{base_url}/api/search?q=needle&scope=all&limit=5",
            headers=auth,
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
            {
                "agent_name": "Codex",
                "message": "Adapter dry run.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
            },
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
            {
                "agent_name": "Codex",
                "message": "Adapter command run.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
            },
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
            {
                "agent_name": "Codex",
                "message": "Adapter command failure.",
                "selected_paths": ["pkg/a.py"],
            },
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
            {
                "agent_name": "Codex",
                "message": "Run from graph UI.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
            },
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
            {
                "provider": "claude",
                "message": "Run through selected provider.",
                "selected_nodes": ["file:pkg/a.py"],
                "selected_paths": ["pkg/a.py"],
                "node": {"id": "file:pkg/a.py", "path": "pkg/a.py"},
            },
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
            {
                "agent_name": "Codex",
                "message": "Cancel this local adapter.",
                "selected_paths": ["pkg/a.py"],
            },
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
            {"message": "auth check", "selected_paths": ["pkg/a.py"]},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert result["ok"] is True
        assert result["run"]["status"] == "queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
