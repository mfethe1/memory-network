from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_index.openclaw_hostd import heartbeat
from code_index.openclaw_hostd import service
from code_index.openclaw_hostd.config import (
    CONTEXT_STORE_PATH_ENV,
    GRAPH_SERVER_TOKEN_ENV,
    GRAPH_SERVER_URL_ENV,
    FLEET_LEASE_STORE_PATH_ENV,
    HOST_ALIASES_ENV,
    HOST_IDENTITY_PATH_ENV,
    HostDaemonConfig,
    NATS_URL_FILE_ENV,
    NATS_URL_ENV,
    REPO_ROOTS_ENV,
    load_config,
)
from code_index.openclaw_hostd.heartbeat import build_heartbeat_payload
from code_index.openclaw_hostd.identity import HostIdentity
from code_index.openclaw_hostd.logging import REDACTED, redact_url


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


def test_default_heartbeat_uses_plain_hostname_without_fqdn_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_getfqdn(*args: object, **kwargs: object) -> None:
        raise AssertionError("default heartbeat generation must not use FQDN lookup")

    monkeypatch.setattr(heartbeat.socket, "getfqdn", fail_getfqdn)
    monkeypatch.setattr(heartbeat.socket, "gethostname", lambda: "plain-hostname")
    identity = HostIdentity(host_id="host_0123456789abcdef0123456789abcdef")
    config = HostDaemonConfig(
        state_dir=tmp_path / "state",
        host_identity_path=tmp_path / "state" / "host-id.json",
        repo_roots=(tmp_path,),
        graph_server_url=None,
    )

    payload = build_heartbeat_payload(config, identity)

    assert payload["ssh_hostname"] == "plain-hostname"


def test_redact_url_fails_closed_for_missing_scheme_query_secret() -> None:
    assert redact_url("127.0.0.1:8767/health?token=super-secret") == REDACTED


def test_redact_url_strips_path_embedded_secret_from_absolute_url() -> None:
    assert (
        redact_url("http://127.0.0.1/secret/super-secret?token=x")
        == "http://127.0.0.1"
    )


def test_cli_once_json_redacts_graph_server_url_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENCLAW_HOSTD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCLAW_HOSTD_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "OPENCLAW_HOSTD_GRAPH_SERVER_URL",
        "127.0.0.1:8767/health?token=cli-secret",
    )
    monkeypatch.setenv("OPENCLAW_HOSTD_HOST_ALIASES", "Rosie, lenny ,, ROSIE")

    rc = service.main(["--once", "--json"])
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert rc == 0
    assert payload["host_aliases"] == ["rosie", "lenny"]
    assert payload["capabilities"]["graph_server"]["url"] == REDACTED
    assert "cli-secret" not in rendered


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
        host_aliases=("lenny", "rosie"),
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
    assert payload["host_aliases"] == ["lenny", "rosie"]
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
    assert {
        "custom",
        "claude",
        "codex",
        "kimi",
        "opencode",
        "cursor",
        "goose",
        "aider",
        "openhands",
    } <= provider_ids

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
                "graph_server_token": "file-token",
                "host_aliases": ["Lenny", "  rosie  ", "", "lenny"],
                "fleet_lease_store_path": str(tmp_path / "leases-from-file.db"),
                "context_store_path": str(tmp_path / "context-from-file.db"),
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
            GRAPH_SERVER_TOKEN_ENV: "env-token",
            HOST_ALIASES_ENV: " ROSIE, lenny ,, rosie ",
            NATS_URL_ENV: "nats://user:nats-secret@example.invalid:4222",
            FLEET_LEASE_STORE_PATH_ENV: str(tmp_path / "leases-from-env.db"),
            CONTEXT_STORE_PATH_ENV: str(tmp_path / "context-from-env.db"),
            HOST_IDENTITY_PATH_ENV: str(tmp_path / "identity-from-env.json"),
        },
        cwd=tmp_path,
    )

    assert config.config_path == config_path
    assert config.state_dir == tmp_path / "state-from-file"
    assert config.host_identity_path == tmp_path / "identity-from-env.json"
    assert config.repo_roots == (env_root,)
    assert config.host_aliases == ("rosie", "lenny")
    assert config.graph_server_url is None
    assert config.graph_server_token == "env-token"
    assert "env-token" not in repr(config)
    assert config.nats_url == "nats://user:nats-secret@example.invalid:4222"
    assert "nats-secret" not in repr(config)
    assert config.fleet_lease_store_path == tmp_path / "leases-from-env.db"
    assert config.context_store_path == tmp_path / "context-from-env.db"
    assert config.ssh_hostname == "file-host"
    assert config.heartbeat_interval_seconds == 45


def test_config_loads_nats_url_from_protected_file(tmp_path: Path) -> None:
    nats_url_file = tmp_path / "nats-url"
    nats_url_file.write_text(
        "nats://user:nats-secret@example.invalid:4222\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "openclaw-hostd.json"
    config_path.write_text(
        json.dumps(
            {
                "repo_roots": [str(tmp_path)],
                "nats_url_file": str(nats_url_file),
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, env={}, cwd=tmp_path)

    assert config.nats_url == "nats://user:nats-secret@example.invalid:4222"
    assert "nats-secret" not in repr(config)


def test_config_env_nats_url_overrides_nats_url_file(tmp_path: Path) -> None:
    nats_url_file = tmp_path / "nats-url"
    nats_url_file.write_text(
        "nats://user:file-secret@example.invalid:4222\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "openclaw-hostd.json"
    config_path.write_text(
        json.dumps(
            {
                "repo_roots": [str(tmp_path)],
                "nats_url_file": str(nats_url_file),
            }
        ),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            NATS_URL_ENV: "nats://user:env-secret@example.invalid:4222",
            NATS_URL_FILE_ENV: str(nats_url_file),
        },
        cwd=tmp_path,
    )

    assert config.nats_url == "nats://user:env-secret@example.invalid:4222"
    assert "file-secret" not in repr(config)
    assert "env-secret" not in repr(config)


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("json", ["lenny/dev"]),
        ("json", ["lenny rosie"]),
        ("json", ["lenny*"]),
        ("env", "lenny@ops"),
        ("env", "rosie,invalid>alias"),
    ],
)
def test_config_rejects_invalid_host_aliases(
    tmp_path: Path,
    source: str,
    value: object,
) -> None:
    config_path = tmp_path / "openclaw-hostd.json"
    payload = {"repo_roots": [str(tmp_path)]}
    env: dict[str, str] = {}
    if source == "json":
        payload["host_aliases"] = value
    else:
        env[HOST_ALIASES_ENV] = str(value)
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="host_aliases|OPENCLAW_HOSTD_HOST_ALIASES"):
        load_config(config_path, env=env, cwd=tmp_path)
