#!/usr/bin/env python3
"""Review one host's OpenClaw deployment readiness without printing secrets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_index.openclaw_controller.service_config import redact_nats_url
from code_index.openclaw_hostd.config import HostDaemonConfig, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review local OpenClaw hostd config for fleet cutover."
    )
    parser.add_argument(
        "--config",
        default=str(
            Path.home() / ".openclaw/config/memory-claude-openclaw-m1-hostd.json"
        ),
        help="Path to the host daemon JSON config.",
    )
    parser.add_argument(
        "--expected-host-id",
        help="Stable host_id this machine should keep after enrollment.",
    )
    parser.add_argument(
        "--expected-alias",
        help="Alias this host should advertise, such as rosie, lenny, or mack.",
    )
    parser.add_argument(
        "--min-recovery-targets",
        type=int,
        default=0,
        help="Minimum peer recovery targets expected for this host.",
    )
    return parser


def review_config(
    config_path: str | Path,
    *,
    expected_host_id: str | None = None,
    expected_alias: str | None = None,
    min_recovery_targets: int = 0,
) -> dict[str, Any]:
    config = load_config(config_path)
    issues: list[str] = []
    suggestions: list[str] = []
    checks = {
        "identity": _identity_check(config, issues),
        "repo_roots": _repo_root_checks(config, issues),
        "nats": _nats_check(config, issues),
        "graph_server": {"configured": bool(config.graph_server_url)},
        "capacity": {"max_parallel_runs": config.max_parallel_runs},
        "recovery_targets": _recovery_target_checks(config),
    }
    expected_id = _optional_text(expected_host_id)
    actual_id = _optional_text(checks["identity"].get("host_id"))
    if expected_id is not None and actual_id != expected_id:
        issues.append(f"host_id mismatch: expected {expected_id} got {actual_id}")
    expected = _optional_text(expected_alias)
    aliases = list(config.host_aliases)
    if expected is not None and expected.lower() not in aliases:
        issues.append(f"missing expected alias: {expected.lower()}")
    if config.max_parallel_runs < 2:
        suggestions.append("set max_parallel_runs to at least 2 for fleet balancing")
    if len(config.recovery_targets) < max(0, min_recovery_targets):
        suggestions.append(
            f"add at least {min_recovery_targets} peer recovery targets "
            "for a three-host mesh"
        )
    return {
        "ok": not issues,
        "host_id": checks["identity"].get("host_id"),
        "host_aliases": aliases,
        "checks": checks,
        "issues": issues,
        "suggestions": suggestions,
    }


def _identity_check(
    config: HostDaemonConfig,
    issues: list[str],
) -> dict[str, Any]:
    path = config.host_identity_path
    if not path.is_file():
        issues.append(f"missing host identity file: {path}")
        return {"path": str(path), "exists": False, "host_id": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        issues.append(f"invalid host identity JSON: {path}")
        return {"path": str(path), "exists": True, "host_id": None}
    host_id = _optional_text(payload.get("host_id")) if isinstance(payload, dict) else None
    if host_id is None:
        issues.append(f"host identity has no host_id: {path}")
    return {"path": str(path), "exists": True, "host_id": host_id}


def _repo_root_checks(
    config: HostDaemonConfig,
    issues: list[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for root in config.repo_roots:
        exists = root.exists()
        if not exists:
            issues.append(f"repo root does not exist: {root}")
        results.append({"path": str(root), "exists": exists})
    if not results:
        issues.append("no repo roots configured")
    return results


def _nats_check(
    config: HostDaemonConfig,
    issues: list[str],
) -> dict[str, Any]:
    url = _optional_text(config.nats_url)
    if url is None:
        issues.append("NATS URL is not configured")
        return {"configured": False, "authenticated": False, "url": None}
    parsed = urlsplit(url)
    authenticated = bool(parsed.username or parsed.password)
    if not authenticated:
        issues.append("NATS URL is not authenticated")
    return {
        "configured": True,
        "authenticated": authenticated,
        "url": redact_nats_url(url),
    }


def _recovery_target_checks(config: HostDaemonConfig) -> list[dict[str, Any]]:
    return [
        {
            "host_id": target.host_id,
            "platform": target.platform,
            "ssh_host": target.ssh_host,
            "repo_root": target.repo_root,
            "service_name": target.service_name,
        }
        for target in config.recovery_targets
    ]


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = review_config(
        args.config,
        expected_host_id=args.expected_host_id,
        expected_alias=args.expected_alias,
        min_recovery_targets=args.min_recovery_targets,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
