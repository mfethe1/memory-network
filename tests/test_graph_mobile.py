"""Contract tests for the future mobile graph page renderer."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from code_index import config as cfg_mod
from code_index.commands.graph_server_cmd import _make_handler


def _render_mobile_html(payload: dict[str, Any]) -> str:
    try:
        graph_mobile = importlib.import_module("code_index.commands.graph_mobile")
    except ModuleNotFoundError as exc:
        if exc.name == "code_index.commands.graph_mobile":
            pytest.fail(
                "expected code_index.commands.graph_mobile.render_mobile_html(payload)"
            )
        raise
    return graph_mobile.render_mobile_html(payload)


def _request_text(url: str, headers: dict[str, str] | None = None) -> str:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _payload() -> dict[str, Any]:
    return {
        "kind": "code_index_graph",
        "schema_version": 1,
        "root": "/repo",
        "generated_at": "2026-05-01T12:00:00+00:00",
        "summary": {"file_count": 1, "node_count": 1, "edge_count": 0},
        "nodes": [
            {
                "id": "file:pkg/app.py",
                "kind": "file",
                "path": "pkg/app.py",
                "label": "app.py",
            }
        ],
        "edges": [],
        "agent": {"name": "Codex", "active_files": [], "active_runs": []},
        "activity": {"agent_events": [], "agent_recent_files": []},
        "live": {
            "server": True,
            "desktop_graph_path": "/repo-graph.html",
            "graph_path": "/repo-graph.json",
            "mobile_path": "/mobile.html",
            "events_path": "/events",
            "debug_path": "/api/debug",
            "search_path": "/api/search",
            "symbols_path": "/api/symbols",
            "agent_board_path": "/api/agent-board",
            "agent_preflight_path": "/api/agent-task-preflight",
            "agent_runs_path": "/api/agent-runs",
            "agent_run_detail_path": "/api/agent-runs/{run_id}",
            "agent_run_messages_path": "/api/agent-runs/{run_id}/messages",
            "agent_run_cancel_path": "/api/agent-runs/{run_id}/cancel",
            "agent_run_accept_review_path": "/api/agent-runs/{run_id}/accept-review",
            "agent_run_archive_path": "/api/agent-runs/{run_id}/archive",
            "agent_providers_path": "/api/agent-providers",
            "file_claims_path": "/api/file-claims",
            "auth_browser_session_path": "/api/auth/browser-session",
        },
    }


def _embedded_graph_json(html: str) -> str:
    marker = '<script id="mobile-data" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    return html[start:end]


def _has_mobile_tab(html: str, tab: str) -> bool:
    return bool(
        re.search(
            rf"<button[^>]+data-mobile-tab=[\"']{re.escape(tab)}[\"']",
            html,
            re.I,
        )
    )


def _has_mobile_panel(html: str, tab: str) -> bool:
    return bool(
        re.search(
            rf"<section[^>]+id=[\"']panel-{re.escape(tab)}[\"']",
            html,
            re.I,
        )
    )


def test_mobile_graph_page_includes_mobile_viewport_and_primary_navigation():
    html = _render_mobile_html(_payload())

    viewport = re.search(r"<meta[^>]+name=[\"']viewport[\"'][^>]*>", html, re.I)
    assert viewport is not None
    assert "width=device-width" in viewport.group(0)
    assert "initial-scale=1" in viewport.group(0)
    assert re.search(r"<nav[^>]+aria-label=[\"']Mobile navigation[\"']", html, re.I)
    for tab in ("graph", "files", "runs", "debug"):
        assert _has_mobile_panel(html, tab)
        assert _has_mobile_tab(html, tab)
    assert any(_has_mobile_panel(html, tab) for tab in ("chat", "task"))
    assert any(_has_mobile_tab(html, tab) for tab in ("chat", "task"))


def test_mobile_graph_page_includes_graph_viewport_svg():
    html = _render_mobile_html(_payload())

    graph_panel = re.search(
        r"<section[^>]+id=[\"']panel-graph[\"'][\s\S]*?</section>",
        html,
        re.I,
    )
    assert graph_panel is not None
    assert re.search(
        r"<svg[^>]+(?:id|class|data-testid|aria-label)=[\"'][^\"']*graph[^\"']*[\"']",
        graph_panel.group(0),
        re.I,
    )


def test_mobile_graph_page_uses_dark_operational_theme():
    html = _render_mobile_html(_payload())

    assert '<meta name="theme-color"' in html
    assert "color-scheme: dark" in html
    assert re.search(r"--bg:\s*#0[0-9a-f]{5}", html, re.I)
    assert re.search(r"--panel:\s*#1[0-9a-f]{5}", html, re.I)
    assert "prefers-color-scheme" not in html


def test_mobile_graph_controls_are_accessible_and_consistent():
    html = _render_mobile_html(_payload())

    for control_id in ("graph-load", "graph-fit", "graph-zoom-out", "graph-zoom-in"):
        assert re.search(
            rf"<button[^>]+id=[\"']{control_id}[\"'][^>]+aria-label=[\"'][^\"']+[\"']",
            html,
            re.I,
        )
    for target in ("task", "files", "runs", "board"):
        assert f'data-open-view="{target}"' in html


def test_mobile_graph_page_has_two_pointer_pinch_zoom_hooks():
    html = _render_mobile_html(_payload())

    assert re.search(r"addEventListener\([\"']pointerdown[\"']", html)
    assert re.search(r"addEventListener\([\"']pointermove[\"']", html)
    assert re.search(
        r"(?:addEventListener\([^)]*pointer(?:up|cancel)|"
        r"pointer(?:up|cancel)[\s\S]{0,180}addEventListener)",
        html,
        re.I,
    )
    assert re.search(
        r"(?:pointerCache|activePointers|activePointerIds|graphPointers)",
        html,
        re.I,
    )
    assert re.search(r"(?:Math\.hypot|distanceBetween|pinchDistance)", html, re.I)
    assert re.search(r"(?:pinchScale|pinchZoom|scale\s*=|zoom\s*=)", html, re.I)


def test_mobile_orchestrator_chat_builds_targeted_agent_runs():
    html = _render_mobile_html(_payload())

    assert "Orchestrator" in html
    for intent in ("implement", "impact", "tests"):
        assert f'data-orchestrator-intent="{intent}"' in html
    assert "mobile-orchestrator" in html
    assert "targeted_run" in html
    assert "run_context" in html
    assert "api.runMessages" in html


def test_mobile_shell_has_collapsible_top_and_bottom_context():
    html = _render_mobile_html(_payload())

    assert re.search(
        r"<button[^>]+id=[\"']top-context-toggle[\"'][^>]+aria-expanded=[\"']false[\"']",
        html,
        re.I,
    )
    assert 'aria-controls="top-context-details"' in html
    assert 'id="top-context-details"' in html
    assert re.search(
        r"<button[^>]+id=[\"']bottom-context-toggle[\"'][^>]+aria-expanded=[\"']false[\"']",
        html,
        re.I,
    )
    assert 'aria-controls="bottom-nav-drawer"' in html
    assert 'id="bottom-nav-drawer"' in html
    assert "toggleContextPanel" in html


def test_mobile_bottom_dock_prioritizes_chat_and_file_selection():
    html = _render_mobile_html(_payload())

    dock_start = html.index('class="mobile-dock"')
    chat_button = html.index('id="dock-chat"', dock_start)
    files_button = html.index('id="dock-files"', dock_start)
    graph_button = html.index('id="dock-graph"', dock_start)

    assert chat_button < files_button < graph_button
    assert re.search(
        r"<button[^>]+id=[\"']dock-chat[\"'][^>]+class=[\"'][^\"']*primary[^\"']*[\"']",
        html,
        re.I,
    )
    assert 'data-mobile-tab="chat"' in html
    assert 'data-mobile-tab="files"' in html


def test_mobile_chat_starts_with_file_context_and_message_composer():
    html = _render_mobile_html(_payload())

    task_panel = re.search(
        r"<section[^>]+id=[\"']panel-task[\"'][\s\S]*?</section>",
        html,
        re.I,
    )
    assert task_panel is not None
    panel = task_panel.group(0)
    file_picker = panel.index('id="chat-file-picker"')
    message = panel.index('id="task-message"')
    advanced = panel.index('id="chat-advanced-controls"')

    assert file_picker < message < advanced
    assert 'id="chat-add-files"' in panel
    assert 'id="chat-selected-count"' in panel
    assert re.search(
        r"<details[^>]+id=[\"']chat-advanced-controls[\"']",
        panel,
        re.I,
    )


def test_mobile_chat_keeps_submitted_messages_reviewable():
    html = _render_mobile_html(_payload())

    assert 'id="chat-history"' in html
    assert 'aria-label="Mobile chat history"' in html
    assert "renderChatHistory" in html
    assert "appendChatMessage" in html

    submit_body = html.split("async function submitTask()", 1)[1].split(
        "function taskPayload()", 1
    )[0]
    assert "appendChatMessage" in submit_body
    assert 'setTab("runs")' not in submit_body
    assert 'setTab("task")' in submit_body


def test_mobile_chat_supports_find_command_and_symbol_context():
    html = _render_mobile_html(_payload())

    assert 'id="mobile-find-results"' in html
    assert "parseChatCommand" in html
    assert "handleFindCommand" in html
    assert "api.symbols" in html
    assert "symbol_definition" in html
    assert "hit.def_file" in html
    assert "selectPath(hit.def_file" in html
    assert "edit_policy" in html


def test_mobile_runs_surface_agent_streams_and_working_context():
    html = _render_mobile_html(_payload())

    runs_panel = re.search(
        r"<section[^>]+id=[\"']panel-runs[\"'][\s\S]*?</section>",
        html,
        re.I,
    )
    assert runs_panel is not None
    panel = runs_panel.group(0)

    assert 'id="agent-streams"' in panel
    assert 'aria-label="Agent streams and current work"' in panel
    assert "renderAgentStreams" in html
    assert "handleAgentSnapshot" in html
    assert "workingFilesForRun" in html
    assert "latestEventForRun" in html
    assert "Open chat" in html


def test_mobile_open_stream_surfaces_selected_run_timeline_before_run_list():
    html = _render_mobile_html(_payload())

    runs_panel = re.search(
        r"<section[^>]+id=[\"']panel-runs[\"'][\s\S]*?</section>",
        html,
        re.I,
    )
    assert runs_panel is not None
    panel = runs_panel.group(0)

    selected_stream = panel.index('id="selected-stream"')
    run_list = panel.index('id="runs-list"')
    assert selected_stream < run_list
    assert 'aria-live="polite"' in panel
    assert "renderSelectedStream" in html
    assert "focusRunStream" in html
    assert "openRun(run.run_id, { focusStream: true })" in html
    assert "stream-card selected" in html
    assert "Message run" in html


def test_mobile_chat_has_selected_run_reply_target():
    html = _render_mobile_html(_payload())

    task_panel = re.search(
        r"<section[^>]+id=[\"']panel-task[\"'][\s\S]*?</section>",
        html,
        re.I,
    )
    assert task_panel is not None
    panel = task_panel.group(0)

    run_context = panel.index('id="chat-run-context"')
    message = panel.index('id="task-message"')
    assert run_context < message
    assert 'id="chat-run-context-title"' in panel
    assert 'id="chat-run-context-stream"' in panel
    assert 'id="chat-run-context-clear"' in panel
    assert "updateComposerRunContext" in html
    assert "clearSelectedRun" in html
    assert "Send to run" in html


def test_mobile_file_context_chips_have_preview_actions():
    html = _render_mobile_html(_payload())

    files_panel = re.search(
        r"<section[^>]+id=[\"']panel-files[\"'][\s\S]*?</section>",
        html,
        re.I,
    )
    assert files_panel is not None
    panel = files_panel.group(0)

    selected_chips = panel.index('id="selected-file-chips"')
    context_preview = panel.index('id="file-context-preview"')
    assert selected_chips < context_preview
    assert 'aria-label="Selected file context preview"' in panel
    assert "renderFileContextPreview" in html
    assert "previewPath(path)" in html
    assert "fileNodeForPath" in html
    assert "node.code.content" in html


def test_mobile_graph_page_fetches_repo_graph_json_on_demand():
    html = _render_mobile_html(_payload())

    assert "/repo-graph.json" in html
    assert re.search(
        r"(?:fetch|requestJson)\(\s*(?:api\.graph|graphPath|graph_path|live\.graph_path|[\"']/repo-graph\.json[\"'])",
        html,
        re.I,
    )


def test_mobile_graph_page_exposes_phone_client_api_routes():
    payload = _payload()
    html = _render_mobile_html(payload)

    route_map = payload["live"]
    for key, path in route_map.items():
        if not key.endswith("_path"):
            continue
        assert f'"{key}"' in html
        assert path in html


def test_mobile_graph_page_safely_embeds_graph_json():
    payload = _payload()
    dangerous_label = '</script><script>alert("x")</script>&\u2028'
    payload["nodes"][0]["label"] = dangerous_label

    html = _render_mobile_html(payload)
    embedded = _embedded_graph_json(html)

    assert "</script>" not in embedded.lower()
    assert "<script>" not in embedded.lower()
    assert "\\u003c/script\\u003e" in embedded.lower()
    assert "\\u0026" in embedded
    assert "\\u2028" in embedded
    assert json.loads(embedded)["nodes"][0]["label"] == dangerous_label


def test_mobile_graph_page_does_not_use_query_token_auth():
    html = _render_mobile_html(_payload())

    assert re.search(r"[?&](?:access_)?token=", html, re.I) is None
    assert re.search(r"[?&](?:auth|graph)_token=", html, re.I) is None
    assert re.search(
        r"URLSearchParams\([^)]*location\.search[^)]*\)"
        r".*get\([\"'](?:token|access_token|auth_token|graph_token)",
        html,
        re.I | re.S,
    ) is None


def test_graph_server_serves_mobile_page(tmp_path: Path):
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
        html = _request_text(f"{base_url}/mobile.html")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert '<script id="mobile-data" type="application/json">' in html
    assert 'aria-label="Mobile navigation"' in html
    assert "/api/agent-board" in html


def test_graph_server_mobile_page_uses_browser_auth_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CODE_INDEX_GRAPH_TOKEN", "mobile-secret")
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
        auth_page = _request_text(f"{base_url}/mobile.html")
        mobile_html = _request_text(
            f"{base_url}/mobile.html",
            headers={"Authorization": "Bearer mobile-secret"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "Graph server token" in auth_page
    assert '<script id="mobile-data" type="application/json">' in mobile_html
