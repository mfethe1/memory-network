"""Install repo-local Code Index Agent integration files."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

for _parent in Path(__file__).resolve().parents:
    if (_parent / "code_index").is_dir():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from code_index import agent_providers  # noqa: E402


def _source_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "code_index").is_dir():
            return parent
    return Path(__file__).resolve().parents[3]


def _mcp_server(root: Path | None = None) -> dict[str, Any]:
    server: dict[str, Any] = {
        "command": "python",
        "args": ["-m", "code_index", "mcp-serve", "--root", "."],
    }
    source_root = _source_root()
    if root is None or source_root != root.resolve():
        server["env"] = {"PYTHONPATH": str(source_root)}
    return server


MCP_SERVER = _mcp_server()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install repo-local Code Index Agent MCP and graph launcher config."
    )
    parser.add_argument("--root", default=".", help="repo root to configure")
    parser.add_argument("--host", default="127.0.0.1", help="graph-server host")
    parser.add_argument("--port", default="8767", help="graph-server port")
    parser.add_argument(
        "--provider",
        choices=agent_providers.provider_choices(),
        default="custom",
        help="local agent provider preset for browser-submitted tasks",
    )
    parser.add_argument(
        "--agent-command",
        help="custom local agent command template; overrides --provider",
    )
    parser.add_argument(
        "--no-claude-settings",
        action="store_true",
        help="do not write .claude/settings.local.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the files that would be written without changing them",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser.parse_args()


def install(
    root: Path,
    *,
    host: str = "127.0.0.1",
    port: str = "8767",
    provider: str = "custom",
    agent_command: str | None = None,
    write_claude_settings: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    provider = agent_providers.normalize_provider_id(provider)
    agent_providers.require_provider(provider)
    plugin_script = _repo_relative(
        root,
        Path(__file__).resolve().parent / "start_graph_server.py",
    )
    report: dict[str, Any] = {
        "root": str(root),
        "dry_run": bool(dry_run),
        "written": [],
        "would_write": [],
        "graph_url": f"http://{host}:{port}/repo-graph.html",
    }

    mcp_path = root / ".mcp.json"
    mcp_server = _mcp_server(root)
    mcp_payload = _merge_mcp(_read_json(mcp_path), mcp_server)
    _write_json(mcp_path, mcp_payload, report, dry_run=dry_run)

    if write_claude_settings:
        claude_path = root / ".claude" / "settings.local.json"
        claude_payload = _merge_mcp(_read_json(claude_path), mcp_server)
        _write_json(claude_path, claude_payload, report, dry_run=dry_run)

    plugin_config_path = root / ".code_index" / "agent-plugin.json"
    launcher_args = [
        "python",
        plugin_script,
        "--root",
        ".",
        "--host",
        host,
        "--port",
        str(port),
        "--ensure-index",
    ]
    if agent_command:
        launcher_args.extend(["--agent-command", agent_command])
    elif provider != "custom":
        launcher_args.extend(["--provider", provider])
    plugin_config = {
        "name": "code-index-agent",
        "mcp_server": mcp_server,
        "graph_server": {
            "host": host,
            "port": str(port),
            "provider": provider,
            "agent_command": agent_command,
            "url": f"http://{host}:{port}/repo-graph.html",
        },
        "commands": {
            "start_graph": " ".join(shlex.quote(part) for part in launcher_args),
            "context": "python -m code_index context \"<task>\" --json",
        },
    }
    _write_json(plugin_config_path, plugin_config, report, dry_run=dry_run)

    demo_task_path = root / ".code_index" / "demo-agent-task.json"
    demo_task = {
        "kind": "code_index_graph_agent_task",
        "message": "Review the repo graph, identify the main implementation files, and report affected tests before editing.",
        "selected_nodes": ["dir:."],
        "selected_paths": [],
        "callback": {"agent_events_url": f"http://{host}:{port}/api/agent-events"},
    }
    _write_json(demo_task_path, demo_task, report, dry_run=dry_run)

    ps1_path = root / ".code_index" / "start-code-index-agent.ps1"
    ps1 = "& " + " ".join(_powershell_quote(part) for part in launcher_args) + "\n"
    _write_text(ps1_path, ps1, report, dry_run=dry_run)

    sh_path = root / ".code_index" / "start-code-index-agent.sh"
    sh = "#!/usr/bin/env sh\nexec " + " ".join(shlex.quote(part) for part in launcher_args) + "\n"
    _write_text(sh_path, sh, report, dry_run=dry_run)

    return report


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} contains invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _merge_mcp(existing: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    payload = dict(existing)
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["code-index"] = server
    payload["mcpServers"] = servers
    return payload


def _write_json(
    path: Path, payload: dict[str, Any], report: dict[str, Any], *, dry_run: bool
) -> None:
    _record(path, report, dry_run=dry_run)
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str, report: dict[str, Any], *, dry_run: bool) -> None:
    _record(path, report, dry_run=dry_run)
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _record(path: Path, report: dict[str, Any], *, dry_run: bool) -> None:
    key = "would_write" if dry_run else "written"
    report[key].append(str(path))


def _repo_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    args = parse_args()
    try:
        report = install(
            Path(args.root),
            host=args.host,
            port=str(args.port),
            provider=args.provider,
            agent_command=args.agent_command,
            write_claude_settings=not args.no_claude_settings,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        verb = "would write" if args.dry_run else "wrote"
        paths = report["would_write"] if args.dry_run else report["written"]
        print(f"code-index-agent installer {verb} {len(paths)} file(s)")
        print(report["graph_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
