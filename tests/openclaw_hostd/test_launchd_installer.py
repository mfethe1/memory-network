from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install_openclaw_m1_launchd.py"
ROSIE_HOST_ID = "host_a23037f43daa41b19d1d441ec514af33"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_openclaw_m1_launchd", INSTALLER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_host_identity(path: Path, host_id: str) -> None:
    path.write_text(json.dumps({"host_id": host_id}) + "\n", encoding="utf-8")


def test_launchd_installer_writes_rosie_host_config_for_shared_broker(
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
        install["launch_agents"],
    ):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = installer.write_hostd_config(
        install=install,
        repo=repo,
        identity_path=install["hostd_state"] / "host-identity.json",
        nats_url="nats://openclaw-system-2026@openclaw-m1-broker-01.internal:4222",
        host_display_name="rosie",
        host_alias="Rosie",
        graph_port=8767,
        heartbeat_seconds=30,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["host_aliases"] == ["rosie"]
    assert payload["ssh_hostname"] == "rosie"
    assert payload["repo_roots"] == [str(repo)]
    assert payload["graph_server_url"] == "http://127.0.0.1:8767"
    assert payload["nats_url"] == (
        "nats://openclaw-system-2026@openclaw-m1-broker-01.internal:4222"
    )
    assert payload["context_store_path"] == str(
        tmp_path / "home/.openclaw/state/memory-claude-openclaw-m1/context-store.db"
    )


def test_launchd_installer_does_not_provision_broker_by_default() -> None:
    installer = _load_installer()

    args = installer.build_parser().parse_args(["--nats-url", "nats://example:4222"])

    assert args.provision_broker is False
    assert args.no_start is False


def test_launchd_bootstrap_services_uses_generated_plists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    installer = _load_installer()
    install = installer.install_paths(tmp_path / "repo", home=tmp_path / "home")
    install["launch_agents"].mkdir(parents=True, exist_ok=True)
    label = "ai.openclaw.memory-claude-m1.hostd"
    (install["launch_agents"] / f"{label}.plist").write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))

    monkeypatch.setattr(installer.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    installer.bootstrap_services([label], install=install)

    assert calls == [
        [
            "launchctl",
            "bootout",
            "gui/501",
            str(install["launch_agents"] / f"{label}.plist"),
        ],
        [
            "launchctl",
            "bootstrap",
            "gui/501",
            str(install["launch_agents"] / f"{label}.plist"),
        ],
        ["launchctl", "kickstart", "-k", f"gui/501/{label}"],
    ]


def test_launchd_installer_main_preserves_rosie_identity_and_alias(
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
        install["launch_agents"],
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _seed_host_identity(install["hostd_state"] / "host-identity.json", ROSIE_HOST_ID)

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
            "rosie",
            "--host-alias",
            "rosie",
            "--nats-url",
            "nats://canonical-broker.internal:4222",
            "--no-start",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["host_id"] == ROSIE_HOST_ID
    assert result["started"] is False

    payload = json.loads(
        (install["config_dir"] / "memory-claude-openclaw-m1-hostd.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["host_aliases"] == ["rosie"]
    assert payload["nats_url"] == "nats://canonical-broker.internal:4222"
    assert json.loads(
        (install["hostd_state"] / "host-identity.json").read_text(encoding="utf-8")
    ) == {"host_id": ROSIE_HOST_ID}
