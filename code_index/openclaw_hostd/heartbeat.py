"""Heartbeat payload generation for the OpenClaw host daemon."""

from __future__ import annotations

import platform
import socket
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from code_index import agent_providers
from code_index.openclaw_hostd.config import HostDaemonConfig
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.logging import redact_url


GraphServerProbe = Callable[[str], bool]


def check_graph_server_available(url: str, *, timeout: float = 0.2) -> bool:
    try:
        request = Request(url, method="GET")
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, URLError, ValueError):
        return False


def _repo_root_payload(path: Path) -> dict[str, object]:
    root = path.expanduser().resolve()
    return {
        "path": str(root),
        "exists": root.exists(),
        "code_index_config": (root / ".code_index").is_dir(),
    }


def _provider_payload() -> list[dict[str, object]]:
    providers: list[dict[str, object]] = []
    for provider in agent_providers.provider_registry_payload():
        providers.append(
            {
                "id": provider["id"],
                "display_name": provider["display_name"],
                "capabilities": provider["capabilities"],
                "command_preset": bool(provider.get("command_preset")),
            }
        )
    return providers


def _ssh_hostname(config: HostDaemonConfig) -> str:
    configured = (config.ssh_hostname or "").strip()
    if configured:
        return configured
    return socket.gethostname()


def detect_capabilities(
    config: HostDaemonConfig,
    *,
    graph_server_probe: GraphServerProbe | None = None,
    probe_graph_server: bool = False,
) -> dict[str, Any]:
    graph_server_checked = False
    graph_server_available: bool | None = None
    if config.graph_server_url and (probe_graph_server or graph_server_probe):
        probe = graph_server_probe or check_graph_server_available
        graph_server_checked = True
        graph_server_available = bool(probe(config.graph_server_url))

    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "repo_roots": [_repo_root_payload(path) for path in config.repo_roots],
        "providers": _provider_payload(),
        "graph_server": {
            "url": redact_url(config.graph_server_url),
            "checked": graph_server_checked,
            "available": graph_server_available,
        },
    }


def build_heartbeat_payload(
    config: HostDaemonConfig,
    identity: HostIdentity,
    *,
    now: datetime | None = None,
    graph_server_probe: GraphServerProbe | None = None,
    probe_graph_server: bool = False,
) -> dict[str, Any]:
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)

    return {
        "kind": "openclaw.host_heartbeat",
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "host_id": identity.host_id,
        "ssh_hostname": _ssh_hostname(config),
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        "capabilities": detect_capabilities(
            config,
            graph_server_probe=graph_server_probe,
            probe_graph_server=probe_graph_server,
        ),
    }
