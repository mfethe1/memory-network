"""Browser coverage for graph UI agent submission and stream updates."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import textwrap
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from code_index import agent_activity
from code_index import agent_providers
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.cli import main
from code_index.commands import agent_adapter_cmd
from code_index.commands.graph_server_cmd import _make_handler

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright


def _wait_for_run_status(
    config: cfg_mod.Config,
    run_id: str,
    expected: str,
    *,
    timeout: float = 8.0,
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


def test_graph_ui_submits_agent_task_and_streams_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("CODE_INDEX_AGENT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text(
        "from .b import helper\n\n"
        "def value() -> int:\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "b.py").write_text(
        "def helper() -> int:\n"
        "    return 42\n",
        encoding="utf-8",
    )
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    seen_task_path = tmp_path / "seen-task.json"
    adapter_path = tmp_path / "fake_agent_adapter.py"
    adapter_path.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time
            from pathlib import Path

            with open(sys.argv[1], encoding="utf-8") as handle:
                task = json.load(handle)
            Path(sys.argv[2]).write_text(json.dumps(task), encoding="utf-8")

            graph_context = task.get("graph_context") or {}
            selected_paths = {
                node.get("path")
                for node in graph_context.get("selected_nodes", [])
                if isinstance(node, dict)
            }
            if graph_context.get("kind") != "code_index_graph_context":
                print("STATUS failed missing graph context", flush=True)
                sys.exit(3)
            if "pkg/a.py" not in selected_paths:
                print("STATUS failed missing selected graph node", flush=True)
                sys.exit(4)

            print("READ pkg/a.py - reading selected graph node", flush=True)
            print("GRAPH_CONTEXT_OK pkg/a.py", flush=True)
            for idx in range(40):
                print(f"terminal output line {idx}", flush=True)
            print("AuthRequired no access token", file=sys.stderr, flush=True)
            print(
                "CODE_INDEX_EVENT "
                + json.dumps(
                    {
                        "event_type": "status",
                        "message": "fake adapter completed",
                        "status": "completed",
                    }
                ),
                flush=True,
            )
            time.sleep(0.1)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    adapter_command = f'"{sys.executable}" "{adapter_path}" {{task_json}} "{seen_task_path}"'
    monkeypatch.setitem(agent_adapter_cmd.PROVIDER_COMMANDS, "codex", adapter_command)
    monkeypatch.setenv("CODE_INDEX_AGENT_COMMAND", adapter_command)

    config = cfg_mod.load(tmp_path)
    args = argparse.Namespace(
        no_code=False,
        max_code_bytes=200_000,
        focus=["pkg/a.py"],
        agent_name="Codex",
        event_interval=0.1,
        quiet=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config, args))
    server.quiet = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - local browser install guard.
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            pytest.skip(f"Playwright Chromium is not installed or not launchable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"{base_url}/repo-graph.html", wait_until="domcontentloaded")
            nav_box = page.locator(".navigator").bounding_box()
            nav_resizer_box = page.locator("#nav-resizer").bounding_box()
            assert nav_box is not None
            assert nav_resizer_box is not None
            page.mouse.move(
                nav_resizer_box["x"] + nav_resizer_box["width"] / 2,
                nav_resizer_box["y"] + 80,
            )
            page.mouse.down()
            page.mouse.move(nav_resizer_box["x"] + 92, nav_resizer_box["y"] + 80)
            page.mouse.up()
            resized_nav_box = page.locator(".navigator").bounding_box()
            assert resized_nav_box is not None
            assert resized_nav_box["width"] > nav_box["width"] + 45
            assert page.evaluate(
                "() => { const root = JSON.parse(document.getElementById('graph-data').textContent).root; return localStorage.getItem(`code_index_graph_nav_width:${root}`); }"
            )
            page.locator("#layer-mode").select_option("communities")
            page.locator(".community-label").first.wait_for(timeout=10000)
            page.evaluate(
                """() => handleConnectionSnapshot({
                    derived_relationships: [
                        {
                            source: "pkg/a.py",
                            target: "pkg/b.py",
                            observations: 2,
                            confidence: 1,
                            rationale: "browser test"
                        }
                    ]
                })"""
            )
            page.locator(".edge.agent_derived").first.wait_for(timeout=10000)
            page.locator('[data-nav-node="file:pkg/a.py"]').first.click()
            page.get_by_role("button", name="Chat").click()
            assert page.locator("#agent-provider").input_value() == "configured"
            page.locator("#agent-chat-message").fill("browser stream check")
            page.locator("#agent-chat-message").press("Enter")

            page.locator("#terminal-stream-body").wait_for(timeout=10000)
            assert page.locator("#panel-body").evaluate(
                "(el) => el.classList.contains('terminal-view')"
            )
            page.locator("pre", has_text="GRAPH_CONTEXT_OK pkg/a.py").first.wait_for(
                timeout=10000
            )
            page.locator("#terminal-stream-body", has_text="terminal output line 39").wait_for(
                timeout=10000
            )
            page.locator(".terminal-body .stream-stderr", has_text="AuthRequired").wait_for(
                timeout=10000
            )
            assert page.locator("#terminal-stream-body").evaluate(
                "(el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 4"
            )
            body_box = page.locator("#terminal-stream-body").bounding_box()
            composer_box = page.locator(".terminal-composer").bounding_box()
            assert body_box is not None
            assert composer_box is not None
            assert composer_box["y"] > body_box["y"]
            assert page.locator("#run-followup-message").is_visible()
            task = json.loads(seen_task_path.read_text(encoding="utf-8"))
            run = _wait_for_run_status(config, task["run_id"], "completed")
            assert run["status"] == "completed"
            page.locator("#agent-runs", has_text="No queued or active runs.").wait_for(
                timeout=10000
            )
            page.locator(".terminal-run-indicator.is-completed", has_text="Done").wait_for(
                timeout=10000
            )
            assert page.locator(".terminal-cursor").count() == 0
            page.locator("#agent-runs .past-runs summary", has_text="Past runs (1)").click()
            page.locator("#agent-runs").get_by_role("button", name="Stream").first.wait_for(
                timeout=10000
            )
            page.locator('[data-nav-node="file:pkg/b.py"]').first.click()
            page.locator("#agent-runs .past-runs summary", has_text="Past runs (1)").click()
            page.locator("#agent-runs .run-select").first.click()
            page.locator("#terminal-stream-body").wait_for(timeout=10000)

            assert task["graph_context"]["kind"] == "code_index_graph_context"
            assert task["context_packet"]["graph_context"] == task["graph_context"]
            assert task["collaboration"]["kind"] == "code_index_agent_collaboration"
            assert task["graph_context"]["selected_nodes"][0]["path"] == "pkg/a.py"
            page.locator("#agent-runs .past-runs summary", has_text="Past runs (1)").click()
            page.locator("#agent-runs").get_by_role("button", name="Archive").first.click()
            page.locator("#agent-runs", has_text="No queued or active runs.").wait_for(
                timeout=10000
            )
            page.locator("#agent-runs .past-runs").wait_for(state="detached", timeout=10000)
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_graph_ui_shows_provider_neutral_agent_work_bubbles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
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
            prompt="Refactor active file.",
            selected_nodes=["file:pkg/a.py"],
            metadata={"provider": "opencode", "selected_paths": ["pkg/a.py"]},
            status="working",
        )
        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="pkg/a.py",
            message="Updating helper contract",
            payload={"status": "working"},
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

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - local browser install guard.
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            pytest.skip(f"Playwright Chromium is not installed or not launchable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 760})
            page.goto(f"{base_url}/repo-graph.html", wait_until="domcontentloaded")
            bubble = page.locator(
                f'.agent-work-bubble[data-run-details="{run["run_id"]}"]'
            ).first
            bubble.wait_for(timeout=10000)
            bubble.locator(".agent-work-pulse").wait_for(timeout=10000)
            title_text = bubble.locator("title").text_content() or ""
            assert "OpenCode" in title_text
            assert "Editing" in title_text
            assert "Updating helper contract" in title_text
            page.evaluate(
                '(runId) => document.querySelector(\'.agent-work-bubble[data-run-details="\' + runId + \'"]\')?.dispatchEvent(new MouseEvent("click", { bubbles: true }))',
                run["run_id"],
            )
            page.locator("#terminal-stream-body").wait_for(timeout=10000)
            page.locator("#run-followup-message").wait_for(timeout=10000)
            assert page.locator("#run-followup-provider").input_value() == "opencode"
            assert page.locator("#node-kind").text_content() == "Agent Run"
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_graph_ui_refreshes_provider_registry_without_touching_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    assert main(["init", "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    monkeypatch.delenv("CODE_INDEX_AGENT_PROVIDER", raising=False)

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

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - local browser install guard.
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            pytest.skip(f"Playwright Chromium is not installed or not launchable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 760})
            page.goto(f"{base_url}/repo-graph.html", wait_until="domcontentloaded")
            page.get_by_role("button", name="Chat").click()
            assert page.locator("#agent-provider").input_value() == "codex"
            assert page.locator('#agent-provider option[value="opencode"]').count() == 0

            original_payload = agent_providers.provider_registry_payload

            def provider_registry_with_opencode() -> list[dict[str, object]]:
                return original_payload() + [
                    {
                        "id": "opencode",
                        "display_name": "OpenCode",
                        "command_preset": "opencode run {provider_prompt_file}",
                        "capabilities": ["command_preset", "provider_prompt_file"],
                    }
                ]

            monkeypatch.setattr(
                agent_providers,
                "provider_registry_payload",
                provider_registry_with_opencode,
            )

            page.evaluate("() => refreshAgentProviders({ force: true })")

            page.locator('#agent-provider option[value="opencode"]').wait_for(
                state="attached",
                timeout=10000,
            )
            assert "Provider presets ready" in (
                page.locator("#agent-runtime-status").text_content() or ""
            )
            conn = db_mod.connect(config.db_path)
            try:
                run_count = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
            finally:
                db_mod.close(conn)
            assert run_count == 0
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_graph_ui_migrates_stale_directory_state_to_expanded_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (tmp_path / "pkg" / "sub" / "inner").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "inner" / "leaf.py").write_text(
        "def leaf() -> int:\n    return 1\n",
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

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - local browser install guard.
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            pytest.skip(f"Playwright Chromium is not installed or not launchable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 820})
            page.goto(f"{base_url}/repo-graph.html", wait_until="domcontentloaded")
            root = page.evaluate(
                "() => JSON.parse(document.getElementById('graph-data').textContent).root"
            )
            page.evaluate(
                """root => {
                  localStorage.setItem(
                    `code_index_graph_view:${root}`,
                    JSON.stringify({
                      directoryExpansionDefaultVersion: 1,
                      expandedDirs: ["dir:."]
                    })
                  );
                }""",
                root,
            )
            page.reload(wait_until="domcontentloaded")

            page.locator('[data-nav-node="dir:pkg"]').wait_for(timeout=10000)
            page.locator('[data-nav-node="dir:pkg/sub"]').wait_for(timeout=10000)
            page.locator('[data-nav-node="dir:pkg/sub/inner"]').wait_for(timeout=10000)
            page.locator(
                '[data-nav-tree="true"][data-nav-node="file:pkg/sub/inner/leaf.py"]'
            ).wait_for(timeout=10000)
            page.wait_for_function(
                """root => {
                  const raw = localStorage.getItem(`code_index_graph_view:${root}`);
                  if (!raw) return false;
                  const saved = JSON.parse(raw);
                  return saved.directoryExpansionDefaultVersion === 2;
                }""",
                arg=root,
                timeout=10000,
            )
            saved = page.evaluate(
                """root => JSON.parse(localStorage.getItem(`code_index_graph_view:${root}`))""",
                root,
            )
            assert saved["directoryExpansionDefaultVersion"] == 2
            assert saved["directoryExpansionMode"] == "all"
            assert "dir:pkg/sub/inner" in saved["expandedDirs"]
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
