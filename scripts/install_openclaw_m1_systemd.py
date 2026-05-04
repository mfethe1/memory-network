#!/usr/bin/env python3
"""Install the OpenClaw M1 runtime as user systemd services on Linux."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from install_openclaw_m1_launchd import provision_nats

from code_index.openclaw_hostd.config import normalize_host_aliases
from code_index.openclaw_hostd.identity import load_or_create_host_identity


SERVICE_BASENAME = "ai.openclaw.memory-claude-m1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Graph Agent Companion + OpenClaw M1 systemd services."
    )
    parser.add_argument("--repo", default=".", help="Deployed code_index repo root.")
    parser.add_argument(
        "--host-display-name",
        default="lenny",
        help="Human fleet label and SSH hostname to report.",
    )
    parser.add_argument(
        "--host-alias",
        help="Optional fleet routing alias to publish in host heartbeats.",
    )
    parser.add_argument(
        "--nats-url",
        default=os.environ.get("OPENCLAW_NATS_URL"),
        help=(
            "Shared OpenClaw NATS URL, including token auth. "
            "May also be supplied as OPENCLAW_NATS_URL."
        ),
    )
    parser.add_argument("--graph-port", type=int, default=8767)
    parser.add_argument("--fleet-mcp-port", type=int, default=8766)
    parser.add_argument("--heartbeat-seconds", type=int, default=30)
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Write config and unit files without starting systemd services.",
    )
    parser.add_argument(
        "--provision-broker",
        action="store_true",
        help=(
            "Provision shared NATS streams, KV buckets, and this host consumer. "
            "Use only from an admin deployment context, not routine host install."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / "pyproject.toml").is_file():
        raise SystemExit(f"repo does not look like code_index: {repo}")
    nats_url = str(args.nats_url or "").strip()
    if not nats_url:
        raise SystemExit("--nats-url or OPENCLAW_NATS_URL is required")

    install = install_paths(repo)
    for directory in (
        install["state_root"],
        install["hostd_state"],
        install["config_dir"],
        install["logs_dir"],
        install["systemd_user"],
    ):
        directory.mkdir(parents=True, exist_ok=True)

    identity_path = install["hostd_state"] / "host-identity.json"
    identity = load_or_create_host_identity(identity_path)
    config_path = write_hostd_config(
        install=install,
        repo=repo,
        identity_path=identity_path,
        nats_url=nats_url,
        host_display_name=args.host_display_name,
        host_alias=args.host_alias,
        graph_port=args.graph_port,
        heartbeat_seconds=args.heartbeat_seconds,
    )
    if args.provision_broker:
        asyncio.run(
            provision_nats(
                nats_url=nats_url,
                host_id=identity.host_id,
                host_display_name=args.host_display_name,
                repo=repo,
            )
        )
    services = write_systemd_units(
        install=install,
        repo=repo,
        config_path=config_path,
        graph_port=args.graph_port,
        fleet_mcp_port=args.fleet_mcp_port,
    )
    if not args.no_start:
        reload_and_start_services(services)
    print(
        json.dumps(
            {
                "host_id": identity.host_id,
                "hostd_config": str(config_path),
                "services": services,
                "started": not args.no_start,
                "broker_provisioned": bool(args.provision_broker),
            },
            sort_keys=True,
        )
    )
    return 0


def install_paths(repo: Path, *, home: Path | None = None) -> dict[str, Path]:
    root = (home or Path.home()).expanduser()
    state_root = root / ".openclaw" / "state" / "memory-claude-openclaw-m1"
    return {
        "state_root": state_root,
        "hostd_state": state_root / "hostd",
        "context_store": state_root / "context-store.db",
        "config_dir": root / ".openclaw" / "config",
        "logs_dir": root / ".openclaw" / "logs",
        "systemd_user": root / ".config" / "systemd" / "user",
        "venv_python": repo / ".venv" / "bin" / "python",
        "hostd_bin": repo / ".venv" / "bin" / "code-index-openclaw-hostd",
    }


def write_hostd_config(
    *,
    install: dict[str, Path],
    repo: Path,
    identity_path: Path,
    nats_url: str,
    host_display_name: str,
    host_alias: str | None = None,
    graph_port: int,
    heartbeat_seconds: int,
) -> Path:
    config_path = install["config_dir"] / "memory-claude-openclaw-m1-hostd.json"
    payload = {
        "state_dir": str(install["hostd_state"]),
        "host_identity_path": str(identity_path),
        "repo_roots": [str(repo)],
        "graph_server_url": f"http://127.0.0.1:{graph_port}",
        "ssh_hostname": host_display_name,
        "heartbeat_interval_seconds": heartbeat_seconds,
        "nats_url": str(nats_url).strip(),
        "fleet_lease_store_path": str(install["hostd_state"] / "fleet-leases.db"),
        "context_store_path": str(install["context_store"]),
    }
    aliases = normalize_host_aliases(host_alias)
    if aliases:
        payload["host_aliases"] = list(aliases)
    config_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    return config_path


def write_systemd_units(
    *,
    install: dict[str, Path],
    repo: Path,
    config_path: Path,
    graph_port: int,
    fleet_mcp_port: int,
) -> list[str]:
    services = {
        f"{SERVICE_BASENAME}.graph-server.service": (
            "OpenClaw Memory Claude M1 graph server",
            [
                install["venv_python"],
                "-m",
                "code_index",
                "graph-server",
                "--root",
                repo,
                "--host",
                "127.0.0.1",
                "--port",
                str(graph_port),
                "--quiet",
            ],
        ),
        f"{SERVICE_BASENAME}.hostd.service": (
            "OpenClaw Memory Claude M1 host daemon",
            [
                install["hostd_bin"],
                "--config",
                config_path,
                "--json",
                "--probe-graph-server",
                "--probe-context",
            ],
        ),
        f"{SERVICE_BASENAME}.fleet-mcp.service": (
            "OpenClaw Memory Claude M1 fleet MCP server",
            [
                install["venv_python"],
                "-m",
                "code_index",
                "fleet-mcp-serve",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(fleet_mcp_port),
                "--db",
                install["context_store"],
            ],
        ),
    }
    for service, (description, command) in services.items():
        path = install["systemd_user"] / service
        path.write_text(
            systemd_unit(
                description=description,
                command=[str(part) for part in command],
                stdout_path=install["logs_dir"] / f"{service}.log",
                stderr_path=install["logs_dir"] / f"{service}.err.log",
                repo=repo,
                home=install["state_root"].parents[2],
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o644)
    return list(services)


def systemd_unit(
    *,
    description: str,
    command: list[str],
    stdout_path: Path,
    stderr_path: Path,
    repo: Path,
    home: Path,
) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description={description}",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={repo}",
            "Environment=PYTHONUNBUFFERED=1",
            f"Environment=PATH={linux_service_path(home)}",
            f"ExecStart={' '.join(command)}",
            "Restart=always",
            "RestartSec=5",
            f"StandardOutput=append:{stdout_path}",
            f"StandardError=append:{stderr_path}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def linux_service_path(home: Path) -> str:
    return (
        f"{home}/.local/bin:"
        "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
    )


def reload_and_start_services(services: list[str]) -> None:
    run_systemctl(["daemon-reload"])
    for service in services:
        run_systemctl(["enable", "--now", service])


def run_systemctl(args: list[str]) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


if __name__ == "__main__":
    raise SystemExit(main())
