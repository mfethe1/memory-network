from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install_openclaw_m1_systemd.py"
LENNY_HOST_ID = "host_6a163e09f5744561a0827d30253b3ba8"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_openclaw_m1_systemd", INSTALLER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_host_identity(path: Path, host_id: str) -> None:
    path.write_text(json.dumps({"host_id": host_id}) + "\n", encoding="utf-8")


def test_systemd_installer_writes_lenny_host_config_for_rosie_broker(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    repo = tmp_path / "repo"
    repo.mkdir()
    install = installer.install_paths(repo, home=tmp_path / "home")
    for directory in (
        install["hostd_state"],
        install["config_dir"],
        install["logs_dir"],
        install["systemd_user"],
    ):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = installer.write_hostd_config(
        install=install,
        repo=repo,
        identity_path=install["hostd_state"] / "host-identity.json",
        nats_url="nats://openclaw-system-2026@100.72.176.67:4222",
        host_display_name="lenny",
        host_alias="lenny",
        graph_port=8767,
        heartbeat_seconds=30,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["host_aliases"] == ["lenny"]
    assert payload["ssh_hostname"] == "lenny"
    assert payload["repo_roots"] == [str(repo)]
    assert payload["graph_server_url"] == "http://127.0.0.1:8767"
    assert "nats_url" not in payload
    assert Path(payload["nats_url_file"]).read_text(encoding="utf-8").strip() == (
        "nats://openclaw-system-2026@100.72.176.67:4222"
    )
    assert payload["context_store_path"] == str(
        tmp_path / "home/.openclaw/state/memory-claude-openclaw-m1/context-store.db"
    )


def test_systemd_installer_does_not_provision_broker_by_default() -> None:
    installer = _load_installer()

    args = installer.build_parser().parse_args(["--nats-url", "nats://example:4222"])

    assert args.provision_broker is False
    assert args.no_start is False


def test_systemd_installer_resolves_nats_url_from_file(tmp_path: Path) -> None:
    installer = _load_installer()
    nats_url_file = tmp_path / "nats-url"
    nats_url_file.write_text("nats://file-token@example.invalid:4222\n", encoding="utf-8")

    args = installer.build_parser().parse_args(["--nats-url-file", str(nats_url_file)])

    assert installer.resolve_nats_url(args) == "nats://file-token@example.invalid:4222"


def test_systemd_installer_writes_user_units_for_graph_hostd_and_fleet_mcp(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    repo = tmp_path / "repo"
    repo.mkdir()
    install = installer.install_paths(repo, home=tmp_path / "home")
    for directory in (
        install["hostd_state"],
        install["config_dir"],
        install["logs_dir"],
        install["systemd_user"],
    ):
        directory.mkdir(parents=True, exist_ok=True)
    config_path = install["config_dir"] / "memory-claude-openclaw-m1-hostd.json"
    config_path.write_text("{}", encoding="utf-8")

    services = installer.write_systemd_units(
        install=install,
        repo=repo,
        config_path=config_path,
        graph_port=8767,
        fleet_mcp_port=8766,
    )

    assert services == [
        "ai.openclaw.memory-claude-m1.graph-server.service",
        "ai.openclaw.memory-claude-m1.hostd.service",
        "ai.openclaw.memory-claude-m1.fleet-mcp.service",
    ]
    graph_unit = (
        install["systemd_user"] / "ai.openclaw.memory-claude-m1.graph-server.service"
    ).read_text(encoding="utf-8")
    hostd_unit = (
        install["systemd_user"] / "ai.openclaw.memory-claude-m1.hostd.service"
    ).read_text(encoding="utf-8")
    fleet_unit = (
        install["systemd_user"] / "ai.openclaw.memory-claude-m1.fleet-mcp.service"
    ).read_text(encoding="utf-8")
    assert "ExecStart=" + str(repo / ".venv/bin/python") in graph_unit
    assert "code_index graph-server --root" in graph_unit
    assert "--host 127.0.0.1 --port 8767 --quiet" in graph_unit
    assert "ExecStart=" + str(repo / ".venv/bin/code-index-openclaw-hostd") in hostd_unit
    assert "--probe-graph-server --probe-context" in hostd_unit
    assert "fleet-mcp-serve --transport http --host 127.0.0.1 --port 8766" in fleet_unit
    assert "WantedBy=default.target" in graph_unit


def test_systemd_installer_main_preserves_lenny_identity_and_alias(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    installer = _load_installer()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'code-index'\n", encoding="utf-8")
    home_root = tmp_path / "home"
    install = installer.install_paths(repo, home=home_root)
    for directory in (
        install["state_root"],
        install["hostd_state"],
        install["config_dir"],
        install["logs_dir"],
        install["systemd_user"],
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _seed_host_identity(install["hostd_state"] / "host-identity.json", LENNY_HOST_ID)

    original_install_paths = installer.install_paths
    monkeypatch.setattr(
        installer,
        "install_paths",
        lambda repo_path, *, home=None: original_install_paths(repo_path, home=home_root),
    )

    exit_code = installer.main(
        [
            "--repo",
            str(repo),
            "--host-display-name",
            "lenny",
            "--host-alias",
            "lenny",
            "--nats-url",
            "nats://canonical-broker.internal:4222",
            "--no-start",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["host_id"] == LENNY_HOST_ID
    assert result["started"] is False

    payload = json.loads(
        (install["config_dir"] / "memory-claude-openclaw-m1-hostd.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["host_aliases"] == ["lenny"]
    assert "nats_url" not in payload
    assert Path(payload["nats_url_file"]).read_text(encoding="utf-8").strip() == (
        "nats://canonical-broker.internal:4222"
    )
    assert json.loads(
        (install["hostd_state"] / "host-identity.json").read_text(encoding="utf-8")
    ) == {"host_id": LENNY_HOST_ID}
