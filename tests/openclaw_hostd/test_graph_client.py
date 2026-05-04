from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from pathlib import Path
from typing import Any

from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.graph_client import GraphServerClient
from code_index.openclaw_hostd import service


class FakeGraphServerState:
    def __init__(self) -> None:
        self.provider_status = HTTPStatus.OK
        self.provider_payload: dict[str, Any] = {
            "ok": True,
            "kind": "code_index_agent_provider_registry",
            "providers": [{"id": "codex", "display_name": "Codex"}],
            "runtime": {"available": True},
        }
        self.agent_run_status = HTTPStatus.OK
        self.agent_run_payload: dict[str, Any] = {
            "ok": True,
            "run": {"run_id": "local-run-1", "status": "queued"},
        }
        self.run_status_payloads: list[dict[str, Any]] = [
            {"run": {"run_id": "local-run-1", "status": "queued"}}
        ]
        self.cancel_payload: dict[str, Any] = {
            "ok": True,
            "run": {"run_id": "local-run-1", "status": "cancelled"},
            "local_cancel_requested": True,
        }
        self.requests: list[dict[str, Any]] = []


class FakeGraphServerHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def state(self) -> FakeGraphServerState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self.state.requests.append({"method": "GET", "path": self.path})
        if self.path == "/api/agent-providers":
            self._send_json(self.state.provider_status, self.state.provider_payload)
            return
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path.startswith("/api/agent-runs/"):
            index = min(
                self._run_status_request_count(),
                len(self.state.run_status_payloads) - 1,
            )
            self._send_json(HTTPStatus.OK, self.state.run_status_payloads[index])
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        payload = self._read_json()
        self.state.requests.append(
            {"method": "POST", "path": self.path, "payload": payload}
        )
        if self.path == "/api/agent-runs":
            self._send_json(self.state.agent_run_status, self.state.agent_run_payload)
            return
        if self.path.startswith("/api/agent-runs/") and self.path.endswith("/cancel"):
            self._send_json(HTTPStatus.OK, self.state.cancel_payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8") or "{}")
        assert isinstance(payload, dict)
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_status_request_count(self) -> int:
        return sum(
            1
            for request in self.state.requests
            if request["method"] == "GET"
            and str(request["path"]).startswith("/api/agent-runs/")
        ) - 1


class RunningFakeGraphServer:
    def __init__(self) -> None:
        self.state = FakeGraphServerState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGraphServerHandler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> RunningFakeGraphServer:
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_graph_server_health_reports_provider_registry_available() -> None:
    with RunningFakeGraphServer() as fake:
        health = GraphServerClient(fake.base_url).health()

    assert health.available is True
    assert health.status_code == HTTPStatus.OK
    assert health.providers == [{"id": "codex", "display_name": "Codex"}]
    assert fake.state.requests == [{"method": "GET", "path": "/api/agent-providers"}]


def test_host_daemon_probe_reports_provider_registry_unavailable_without_crashing(
    tmp_path: Path,
) -> None:
    with RunningFakeGraphServer() as fake:
        fake.state.provider_status = HTTPStatus.INTERNAL_SERVER_ERROR
        fake.state.provider_payload = {"ok": False, "error": "not ready"}
        config = HostDaemonConfig(
            state_dir=tmp_path / "state",
            host_identity_path=tmp_path / "state" / "host-id.json",
            repo_roots=(tmp_path,),
            graph_server_url=f"{fake.base_url}/health",
        )

        payload = service.run_once(config, as_json=True, probe_graph_server=True)

    graph_server = payload["capabilities"]["graph_server"]
    assert graph_server["available"] is False
    assert fake.state.requests == [{"method": "GET", "path": "/api/agent-providers"}]


def test_submit_task_posts_openclaw_task_payload_to_agent_runs() -> None:
    with RunningFakeGraphServer() as fake:
        result = GraphServerClient(fake.base_url).submit_task(
            task_id="task-123",
            host_id="host_0123456789abcdef0123456789abcdef",
            message="Inspect selected files.",
            selected_paths=("code_index/openclaw_hostd/service.py", "tests/x.py"),
            provider="codex",
        )

    assert result.ok is True
    assert result.payload["run"]["run_id"] == "local-run-1"
    assert fake.state.requests == [
        {
            "method": "POST",
            "path": "/api/agent-runs",
            "payload": {
                "task_id": "task-123",
                "host_id": "host_0123456789abcdef0123456789abcdef",
                "message": "Inspect selected files.",
                "selected_paths": [
                    "code_index/openclaw_hostd/service.py",
                    "tests/x.py",
                ],
                "provider": "codex",
            },
        }
    ]


def test_poll_run_status_fetches_until_stopped_status() -> None:
    with RunningFakeGraphServer() as fake:
        fake.state.run_status_payloads = [
            {"run": {"run_id": "local-run-1", "status": "queued"}},
            {"run": {"run_id": "local-run-1", "status": "completed"}},
        ]

        result = GraphServerClient(fake.base_url).poll_run_status(
            "local-run-1",
            interval_seconds=0,
            timeout_seconds=1,
        )

    assert result.ok is True
    assert result.payload["run"]["status"] == "completed"
    assert fake.state.requests == [
        {"method": "GET", "path": "/api/agent-runs/local-run-1"},
        {"method": "GET", "path": "/api/agent-runs/local-run-1"},
    ]


def test_cancel_run_forwards_to_graph_server_cancel_route() -> None:
    with RunningFakeGraphServer() as fake:
        result = GraphServerClient(fake.base_url).cancel_run("local-run-1")

    assert result.ok is True
    assert result.payload["run"]["status"] == "cancelled"
    assert fake.state.requests == [
        {
            "method": "POST",
            "path": "/api/agent-runs/local-run-1/cancel",
            "payload": {},
        }
    ]
