"""HTTP adapter for a local graph-server instance."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


_GRAPH_SERVER_ENDPOINT_PREFIXES = (
    "/api/agent-providers",
    "/api/agent-runs",
    "/api/agent-task-preflight",
    "/health",
)
_STOPPED_RUN_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "canceled",
        "review",
        "needs_review",
        "needs-review",
    }
)


@dataclass(frozen=True)
class GraphServerResponse:
    ok: bool
    status_code: int | None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class GraphServerHealth:
    available: bool
    status_code: int | None
    providers: list[dict[str, Any]] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class GraphServerClient:
    """Small stdlib-HTTP client for the local graph-server API."""

    def __init__(self, base_url: str, *, timeout: float = 2.0) -> None:
        self.base_url = _normalise_base_url(base_url)
        self.timeout = timeout

    def health(self) -> GraphServerHealth:
        response = self._request_json("GET", "/api/agent-providers")
        providers = _dict_list(response.payload.get("providers"))
        runtime = (
            dict(response.payload["runtime"])
            if isinstance(response.payload.get("runtime"), dict)
            else {}
        )
        available = response.ok and bool(response.payload.get("ok"))
        error = response.error
        if response.status_code is not None and not available and error is None:
            error = str(
                response.payload.get("error") or "provider registry unavailable"
            )
        return GraphServerHealth(
            available=available,
            status_code=response.status_code,
            providers=providers,
            runtime=runtime,
            payload=response.payload,
            error=error,
        )

    def submit_task(
        self,
        *,
        task_id: str,
        host_id: str,
        message: str,
        selected_paths: Iterable[str] = (),
        provider: str | None = None,
        selected_nodes: Iterable[str] = (),
        node: dict[str, Any] | None = None,
        agent_name: str | None = None,
    ) -> GraphServerResponse:
        payload: dict[str, Any] = {
            "task_id": str(task_id).strip(),
            "host_id": str(host_id).strip(),
            "message": str(message).strip(),
            "selected_paths": _string_list(selected_paths),
        }
        provider_text = str(provider or "").strip().lower()
        if provider_text:
            payload["provider"] = provider_text
        selected_node_list = _string_list(selected_nodes)
        if selected_node_list:
            payload["selected_nodes"] = selected_node_list
        if node is not None:
            payload["node"] = dict(node)
        agent_name_text = str(agent_name or "").strip()
        if agent_name_text:
            payload["agent_name"] = agent_name_text
        return self._request_json("POST", "/api/agent-runs", payload)

    def get_run_status(self, run_id: str) -> GraphServerResponse:
        encoded_run_id = quote(str(run_id).strip(), safe="")
        return self._request_json("GET", f"/api/agent-runs/{encoded_run_id}")

    def cancel_run(self, run_id: str) -> GraphServerResponse:
        encoded_run_id = quote(str(run_id).strip(), safe="")
        return self._request_json(
            "POST",
            f"/api/agent-runs/{encoded_run_id}/cancel",
            {},
        )

    def poll_run_status(
        self,
        run_id: str,
        *,
        interval_seconds: float = 0.5,
        timeout_seconds: float = 30.0,
        stopped_statuses: Iterable[str] | None = None,
    ) -> GraphServerResponse:
        stopped = {
            str(status).strip().lower()
            for status in (stopped_statuses or _STOPPED_RUN_STATUSES)
            if str(status).strip()
        }
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            response = self.get_run_status(run_id)
            if not response.ok:
                return response
            status = _run_status(response.payload)
            if status in stopped:
                return response
            now = time.monotonic()
            if now >= deadline:
                return GraphServerResponse(
                    ok=False,
                    status_code=response.status_code,
                    payload=response.payload,
                    error=f"timed out waiting for graph-server run {run_id} status",
                )
            sleep_for = max(0.0, min(interval_seconds, deadline - now))
            if sleep_for:
                time.sleep(sleep_for)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> GraphServerResponse:
        data: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self._url(path),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return _json_response(
                    status_code=int(response.status),
                    body=response.read(),
                )
        except HTTPError as exc:
            return _json_response(
                status_code=int(exc.code),
                body=exc.read(),
                default_error=str(exc.reason or exc),
            )
        except (OSError, TimeoutError, URLError, ValueError) as exc:
            return GraphServerResponse(
                ok=False,
                status_code=None,
                error=str(exc),
            )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _normalise_base_url(raw_url: str) -> str:
    parsed = urlsplit(str(raw_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("graph-server URL must be absolute")
    path = parsed.path.rstrip("/")
    if any(
        path == endpoint or path.startswith(f"{endpoint}/")
        for endpoint in _GRAPH_SERVER_ENDPOINT_PREFIXES
    ):
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _json_response(
    *,
    status_code: int,
    body: bytes,
    default_error: str | None = None,
) -> GraphServerResponse:
    try:
        text = body.decode("utf-8")
        payload = json.loads(text or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return GraphServerResponse(
            ok=False,
            status_code=status_code,
            error=f"invalid graph-server JSON response: {exc}",
        )
    if not isinstance(payload, dict):
        return GraphServerResponse(
            ok=False,
            status_code=status_code,
            error="graph-server JSON response must be an object",
        )
    error = str(payload.get("error") or default_error or "") or None
    return GraphServerResponse(
        ok=200 <= status_code < 300 and payload.get("ok", True) is not False,
        status_code=status_code,
        payload=payload,
        error=error,
    )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_list(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        candidates: Iterable[object] = ()
    elif isinstance(values, str):
        candidates = (values,)
    else:
        candidates = values
    seen: set[str] = set()
    result: list[str] = []
    for value in candidates:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _run_status(payload: dict[str, Any]) -> str:
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    return str(run.get("status") or payload.get("status") or "").strip().lower()
