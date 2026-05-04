#!/usr/bin/env python3
"""Install the OpenClaw M1 runtime as launchd services on a local macOS host."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import re
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_index.openclaw_hostd.config import normalize_host_aliases
from code_index.openclaw_hostd.identity import load_or_create_host_identity


STREAMS = {
    "OPENCLAW_TASKS": ["openclaw.task.*.assigned", "openclaw.task.*.ack"],
    "OPENCLAW_RUN_EVENTS": [
        "openclaw.run.*.*.events",
        "openclaw.run.*.*.status",
        "openclaw.run.*.*.verification",
    ],
    "OPENCLAW_AUDIT": ["openclaw.audit.*", "openclaw.context.audit"],
    "OPENCLAW_MESSAGES": [
        "openclaw.message.>",
        "openclaw.room.*.events",
        "openclaw.host.*.inbox",
        "openclaw.host.*.messages.ack",
    ],
    "OPENCLAW_CONTEXT": [
        "openclaw.context.*.*.metrics",
        "openclaw.context.*.*.health",
        "openclaw.context.*.*.manifest.*",
        "openclaw.context.*.*.handoff.*",
    ],
}

KV_BUCKETS = {
    "openclaw_hosts": None,
    "openclaw_leases": None,
    "openclaw_provider_caps": None,
    "openclaw_controller_config": None,
    "openclaw_message_routes": None,
    "openclaw_messaging_adapters": None,
    "openclaw_platform_room_mappings": None,
    "openclaw_context_policy": None,
    "openclaw_context_leases": None,
    "openclaw_agent_states": 90.0,
    "openclaw_mcp_clients": None,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Graph Agent Companion + OpenClaw M1 launchd services."
    )
    parser.add_argument("--repo", default=".", help="Deployed code_index repo root.")
    parser.add_argument(
        "--host-display-name",
        default="rosie",
        help="Human fleet label and SSH hostname to report.",
    )
    parser.add_argument(
        "--host-alias",
        help="Optional fleet routing alias to publish in host heartbeats.",
    )
    parser.add_argument(
        "--nats-conf",
        default=str(Path.home() / ".openclaw/workspace/infra/nats/nats-server.conf"),
        help="Existing local NATS server config containing token auth.",
    )
    parser.add_argument(
        "--nats-url",
        default=os.environ.get("OPENCLAW_NATS_URL"),
        help=(
            "Shared OpenClaw NATS URL, including token auth. "
            "May also be supplied as OPENCLAW_NATS_URL. Overrides --nats-conf."
        ),
    )
    parser.add_argument("--graph-port", type=int, default=8767)
    parser.add_argument("--fleet-mcp-port", type=int, default=8766)
    parser.add_argument("--heartbeat-seconds", type=int, default=30)
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Write config and plist files without bootstrapping launchd services.",
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
    install = install_paths(repo)
    for directory in (
        install["state_root"],
        install["hostd_state"],
        install["config_dir"],
        install["logs_dir"],
        install["launch_agents"],
    ):
        directory.mkdir(parents=True, exist_ok=True)

    nats_url = str(args.nats_url or "").strip()
    if not nats_url:
        nats_token = read_nats_token(Path(args.nats_conf).expanduser())
        nats_url = f"nats://{nats_token}@127.0.0.1:4222"
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
    services = write_launchd_plists(
        install=install,
        repo=repo,
        config_path=config_path,
        graph_port=args.graph_port,
        fleet_mcp_port=args.fleet_mcp_port,
    )
    if not args.no_start:
        bootstrap_services(services, install=install)
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
        "launch_agents": root / "Library" / "LaunchAgents",
        "venv_python": repo / ".venv" / "bin" / "python",
        "hostd_bin": repo / ".venv" / "bin" / "code-index-openclaw-hostd",
    }


def read_nats_token(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'token:\s*"([^"]+)"', text)
    if not match:
        raise ValueError(f"NATS token not found in {path}")
    return match.group(1)


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


async def provision_nats(
    *,
    nats_url: str,
    host_id: str,
    host_display_name: str,
    repo: Path,
) -> None:
    import nats
    from nats.js.api import ConsumerConfig
    from nats.js.api import KeyValueConfig
    from nats.js.api import RetentionPolicy
    from nats.js.api import StorageType
    from nats.js.api import StreamConfig

    nc = await nats.connect(
        servers=[nats_url],
        connect_timeout=2,
        allow_reconnect=False,
        max_reconnect_attempts=0,
    )
    js = nc.jetstream()
    try:
        for name, subjects in STREAMS.items():
            await ensure_stream(
                js,
                name,
                subjects,
                stream_config_factory=StreamConfig,
                storage_type=StorageType,
                retention_policy=RetentionPolicy,
            )
        for bucket, ttl in KV_BUCKETS.items():
            await ensure_kv(
                js,
                bucket,
                ttl,
                key_value_config_factory=KeyValueConfig,
                storage_type=StorageType,
            )
        await ensure_host_consumer(
            js,
            host_id,
            consumer_config_factory=ConsumerConfig,
        )
        hosts = await js.key_value("openclaw_hosts")
        await hosts.put(
            host_id,
            json.dumps(
                {
                    "host_id": host_id,
                    "display_name": host_display_name,
                    "repo_root": str(repo),
                },
                sort_keys=True,
            ).encode("utf-8"),
        )
    finally:
        await nc.close()


async def ensure_stream(
    js: Any,
    name: str,
    subjects: list[str],
    *,
    stream_config_factory: Any,
    storage_type: Any,
    retention_policy: Any,
) -> None:
    cfg = stream_config_factory(
        name=name,
        subjects=subjects,
        storage=storage_type.FILE,
        retention=retention_policy.LIMITS,
    )
    try:
        info = await js.stream_info(name)
    except Exception as exc:
        if not looks_missing(exc):
            raise
        await js.add_stream(cfg)
        print(f"created stream {name}")
        return
    if set(info.config.subjects or []) != set(subjects):
        info.config.subjects = subjects
        info.config.storage = storage_type.FILE
        info.config.retention = retention_policy.LIMITS
        await js.update_stream(info.config)
        print(f"updated stream {name}")
        return
    print(f"stream ok {name}")


async def ensure_kv(
    js: Any,
    bucket: str,
    ttl: float | None,
    *,
    key_value_config_factory: Any,
    storage_type: Any,
) -> None:
    try:
        kv = await js.key_value(bucket)
        status = await kv.status()
    except Exception as exc:
        if not looks_missing(exc):
            raise
        await js.create_key_value(
            config=key_value_config_factory(
                bucket=bucket,
                storage=storage_type.FILE,
                ttl=ttl,
            )
        )
        print(f"created kv {bucket}")
        return
    actual_ttl = _status_ttl(status)
    if ttl is not None and actual_ttl is not None and abs(actual_ttl - ttl) > 0.001:
        info = await js.stream_info(f"KV_{bucket}")
        info.config.max_age = ttl
        await js.update_stream(info.config)
        print(f"updated kv ttl {bucket}")
        return
    print(f"kv ok {bucket}")


async def ensure_host_consumer(
    js: Any,
    host_id: str,
    *,
    consumer_config_factory: Any,
) -> None:
    durable = f"HOST_{host_id}"
    try:
        await js.consumer_info("OPENCLAW_TASKS", durable)
    except Exception as exc:
        if not looks_missing(exc):
            raise
        await js.add_consumer(
            "OPENCLAW_TASKS",
            config=consumer_config_factory(
                durable_name=durable,
                deliver_subject=f"openclaw.deliver.{host_id}.tasks",
                filter_subject=f"openclaw.task.{host_id}.assigned",
            ),
        )
        print(f"created consumer {durable}")
        return
    print(f"consumer ok {durable}")


def looks_missing(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "not found" in text or "notfound" in text or "not_found" in text


def _status_ttl(status: Any) -> float | None:
    for source in (status, getattr(status, "config", None)):
        for attr in ("ttl", "max_age"):
            value = getattr(source, attr, None)
            if value is not None:
                return float(value)
    return None


def write_launchd_plists(
    *,
    install: dict[str, Path],
    repo: Path,
    config_path: Path,
    graph_port: int,
    fleet_mcp_port: int,
) -> list[str]:
    labels = {
        "ai.openclaw.memory-claude-m1.graph-server": [
            str(install["venv_python"]),
            "-m",
            "code_index",
            "graph-server",
            "--root",
            str(repo),
            "--host",
            "127.0.0.1",
            "--port",
            str(graph_port),
            "--quiet",
        ],
        "ai.openclaw.memory-claude-m1.hostd": [
            str(install["hostd_bin"]),
            "--config",
            str(config_path),
            "--json",
            "--probe-graph-server",
            "--probe-context",
        ],
        "ai.openclaw.memory-claude-m1.fleet-mcp": [
            str(install["venv_python"]),
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
            str(install["context_store"]),
        ],
    }
    for label, args in labels.items():
        path = install["launch_agents"] / f"{label}.plist"
        with path.open("wb") as fh:
            plistlib.dump(
                launchd_payload(
                    label,
                    args,
                    stdout_path=install["logs_dir"] / f"{label}.log",
                    stderr_path=install["logs_dir"] / f"{label}.err.log",
                    repo=repo,
                ),
                fh,
                sort_keys=False,
            )
        os.chmod(path, 0o600)
    return list(labels)


def bootstrap_services(services: list[str], *, install: dict[str, Path]) -> None:
    domain = _launchd_domain()
    for label in services:
        plist_path = install["launch_agents"] / f"{label}.plist"
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
            check=False,
        )


def _launchd_domain() -> str:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise RuntimeError("launchd bootstrap requires os.getuid()")
    return f"gui/{getuid()}"


def launchd_payload(
    label: str,
    args: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    repo: Path,
) -> dict[str, Any]:
    return {
        "Label": label,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": (
                "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:"
                "/usr/bin:/bin:/usr/sbin:/sbin"
            ),
        },
        "ProgramArguments": args,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


if __name__ == "__main__":
    raise SystemExit(main())
