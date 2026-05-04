from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_index.openclaw_hostd import heartbeat
from code_index.openclaw_hostd.config import (
    GRAPH_SERVER_URL_ENV,
    HOST_IDENTITY_PATH_ENV,
    HostDaemonConfig,
    REPO_ROOTS_ENV,
    load_config,
)
from code_index.openclaw_hostd.heartbeat import build_heartbeat_payload
from code_index.openclaw_hostd.identity import HostIdentity


def test_default_heartbeat_does_not_probe_graph_server_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("default heartbeat generation must not use network")

    monkeypatch.setattr(heartbeat, "urlopen", fail_urlopen)
    identity = HostIdentity(host_id="host_0123456789abcdef0123456789abcdef")
    config = HostDaemonConfig(
        state_dir=tmp_path / "state",
        host_identity_path=tmp_path / "state" / "host-id.json",
        repo_roots=(tmp_path,),
        graph_server_url="http://127.0.0.1:8767/health",
    )

    payload = build_heartbeat_payload(config, identity)

    graph_server = payload["capabilities"]["graph_server"]
    assert graph_server["available"] is None
    assert graph_server["checked"] is False


def test_heartbeat_reports_host_capabilities_without_secrets(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".code_index").mkdir()
    identity = HostIdentity(host_id="host_0123456789abcdef0123456789abcdef")
    config = HostDaemonConfig(
        state_dir=tmp_path / "state",
        host_identity_path=tmp_path / "state" / "host-id.json",
        repo_roots=(root,),
        graph_server_url="http://user:super-secret@127.0.0.1:8767/health?token=super-secret",
        ssh_hostname="openclaw-test-host",
        heartbeat_interval_seconds=30,
    )

    payload = build_heartbeat_payload(
        config,
        identity,
        now=datetime(2026, 5, 3, 1, 2, 3, tzinfo=timezone.utc),
        graph_server_probe=lambda url: False,
    )

    assert payload["kind"] == "openclaw.host_heartbeat"
    assert payload["schema_version"] == 1
    assert payload["generated_at"] == "2026-05-03T01:02:03+00:00"
    assert payload["host_id"] == identity.host_id
    assert payload["ssh_hostname"] == "openclaw-test-host"
    assert payload["capabilities"]["graph_server"]["available"] is False
    assert payload["capabilities"]["graph_server"]["checked"] is True
    assert payload["capabilities"]["repo_roots"] == [
        {
            "path": str(root.resolve()),
            "exists": True,
            "code_index_config": True,
        }
    ]
    provider_ids = {
        provider["id"] for provider in payload["capabilities"]["providers"]
    }
    assert {"custom", "claude", "codex", "kimi", "opencode"} <= provider_ids

    rendered = json.dumps(payload)
    assert "super-secret" not in rendered
    assert "user:" not in rendered


def test_config_loads_json_file_with_environment_overrides(tmp_path: Path) -> None:
    file_root = tmp_path / "file-root"
    env_root = tmp_path / "env-root"
    file_root.mkdir()
    env_root.mkdir()
    config_path = tmp_path / "openclaw-hostd.json"
    config_path.write_text(
        json.dumps(
            {
                "state_dir": str(tmp_path / "state-from-file"),
                "repo_roots": [str(file_root)],
                "graph_server_url": "http://127.0.0.1:1111/health",
                "ssh_hostname": "file-host",
                "heartbeat_interval_seconds": 45,
            }
        ),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            REPO_ROOTS_ENV: str(env_root),
            GRAPH_SERVER_URL_ENV: "",
            HOST_IDENTITY_PATH_ENV: str(tmp_path / "identity-from-env.json"),
        },
        cwd=tmp_path,
    )

    assert config.config_path == config_path
    assert config.state_dir == tmp_path / "state-from-file"
    assert config.host_identity_path == tmp_path / "identity-from-env.json"
    assert config.repo_roots == (env_root,)
    assert config.graph_server_url is None
    assert config.ssh_hostname == "file-host"
    assert config.heartbeat_interval_seconds == 45
