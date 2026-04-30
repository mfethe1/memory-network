"""`code_index workspace`: manage multi-repo workspaces.

A workspace is a collection of indexed repositories. You can query,
graph, and compare across them. Workspace state is stored in
`~/.code_index/workspaces.json` (or `.code_index/workspace.json` when
--root is given).

Commands:
  workspace init [--name NAME]     — create a workspace in current directory
  workspace add <path> [--name N]  — add a repo to the workspace
  workspace remove <name>          — remove a repo by name
  workspace list                   — show workspace members
  workspace status                 — show index health for each member
  workspace query <pattern>        — FTS query across all workspace repos
  workspace graph [--output PATH]  — build a combined graph of all repos
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from code_index import config as cfg_mod
from code_index import db_router as db_mod


# ---------------------------------------------------------------------------
# Workspace storage
# ---------------------------------------------------------------------------

def _global_workspace_path() -> Path:
    home = Path.home()
    return home / ".code_index" / "workspaces.json"


def _local_workspace_path(root: Path) -> Path:
    return root / ".code_index" / "workspace.json"


def _load_workspace(path: Path) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"name": "default", "members": []}


def _save_workspace(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _resolve_workspace_path(args: argparse.Namespace, root_hint: Path) -> Path:
    if args.workspace_file:
        return Path(args.workspace_file)
    # If current dir has .code_index, prefer local; else global
    root = cfg_mod.find_root(root_hint)
    if root:
        return _local_workspace_path(root)
    return _global_workspace_path()


# ---------------------------------------------------------------------------
# Member helpers
# ---------------------------------------------------------------------------

def _member_dict(path: Path, name: str | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    cfg = cfg_mod.load(resolved)
    has_index = cfg.db_path.exists()
    branch: str | None = None
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            branch = proc.stdout.strip() or None
    except Exception:
        pass
    return {
        "name": name or resolved.name,
        "path": resolved.as_posix(),
        "has_index": has_index,
        "branch": branch,
    }


def _find_member(members: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for m in members:
        if m["name"] == key or m["path"] == key:
            return m
    return None


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def _cmd_init(args: argparse.Namespace, ws_path: Path) -> int:
    if ws_path.exists():
        print(f"error: workspace already exists at {ws_path}")
        return 2
    data = {"name": args.name_opt or ws_path.parent.name, "members": []}
    _save_workspace(ws_path, data)
    if args.json:
        print(json.dumps({"workspace": str(ws_path), "name": data["name"]}, indent=2))
    else:
        print(f"initialized workspace '{data['name']}' at {ws_path}")
    return 0


def _cmd_add(args: argparse.Namespace, ws_path: Path) -> int:
    data = _load_workspace(ws_path)
    target = Path(args.path_opt).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}")
        return 2
    name = args.name_opt or target.name
    existing = _find_member(data["members"], name)
    if existing:
        print(f"error: member '{name}' already exists ({existing['path']})")
        return 2
    target_posix = target.as_posix()
    for m in data["members"]:
        if m["path"] == target_posix:
            print(f"error: path already in workspace as '{m['name']}'")
            return 2
    member = _member_dict(target, name)
    data["members"].append(member)
    _save_workspace(ws_path, data)
    if args.json:
        print(json.dumps({"added": member, "workspace": str(ws_path)}, indent=2))
    else:
        print(f"added '{member['name']}' -> {member['path']} (indexed={member['has_index']})")
    return 0


def _cmd_remove(args: argparse.Namespace, ws_path: Path) -> int:
    data = _load_workspace(ws_path)
    key = args.name_opt
    existing = _find_member(data["members"], key)
    if not existing:
        print(f"error: member '{key}' not found")
        return 2
    data["members"] = [m for m in data["members"] if m["name"] != existing["name"]]
    _save_workspace(ws_path, data)
    if args.json:
        print(json.dumps({"removed": existing["name"], "workspace": str(ws_path)}, indent=2))
    else:
        print(f"removed '{existing['name']}' from workspace")
    return 0


def _cmd_list(args: argparse.Namespace, ws_path: Path) -> int:
    data = _load_workspace(ws_path)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"workspace: {data.get('name', 'default')}  ({ws_path})")
        if not data["members"]:
            print("  (no members)")
        for m in data["members"]:
            idx = "indexed" if m["has_index"] else "not indexed"
            branch = f"  [{m['branch']}]" if m.get("branch") else ""
            print(f"  {m['name']:<20} {m['path']:<50} {idx}{branch}")
    return 0


def _cmd_status(args: argparse.Namespace, ws_path: Path) -> int:
    data = _load_workspace(ws_path)
    results: list[dict[str, Any]] = []
    for m in data["members"]:
        cfg = cfg_mod.load(Path(m["path"]))
        healthy = False
        version: int | None = None
        files = 0
        symbols = 0
        if cfg.db_path.exists():
            try:
                conn = db_mod.connect(cfg.db_path)
                try:
                    db_mod.ensure_schema(conn, cfg)
                    healthy = True
                    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                    version = int(row["value"]) if row else None
                    files = conn.execute(
                        "SELECT COUNT(*) FROM files WHERE deleted_at IS NULL"
                    ).fetchone()[0]
                    symbols = conn.execute(
                        "SELECT COUNT(*) FROM symbols WHERE deleted_at IS NULL"
                    ).fetchone()[0]
                finally:
                    db_mod.close(conn)
            except Exception as exc:
                m["error"] = str(exc)
        r = {
            "name": m["name"],
            "path": m["path"],
            "has_index": m["has_index"],
            "branch": m.get("branch"),
            "healthy": healthy,
            "schema_version": version,
            "files": files,
            "symbols": symbols,
        }
        if "error" in m:
            r["error"] = m["error"]
        results.append(r)

    if args.json:
        print(json.dumps({"workspace": str(ws_path), "members": results}, indent=2))
    else:
        print(f"workspace status: {data.get('name', 'default')}")
        for r in results:
            status = "ok" if r["healthy"] else "missing/broken"
            detail = f"  files={r['files']} symbols={r['symbols']} schema=v{r['schema_version']}"
            print(f"  {r['name']:<20} {status}{detail}")
    return 0


def _cmd_query(args: argparse.Namespace, ws_path: Path) -> int:
    from code_index.search import fts
    data = _load_workspace(ws_path)
    pattern = args.pattern_opt
    all_results: list[dict[str, Any]] = []
    for m in data["members"]:
        cfg = cfg_mod.load(Path(m["path"]))
        if not cfg.db_path.exists():
            continue
        try:
            conn = db_mod.connect(cfg.db_path)
            try:
                db_mod.ensure_schema(conn, cfg)
                rows = fts.search(
                    conn,
                    pattern,
                    limit=args.limit,
                    language=args.lang,
                    chunk_type=args.type,
                )
                for r in rows:
                    r["repo"] = m["name"]
                all_results.extend(rows)
            finally:
                db_mod.close(conn)
        except Exception as exc:
            if args.json:
                all_results.append({"repo": m["name"], "error": str(exc)})

    # Simple re-rank: higher bm25 first
    all_results.sort(key=lambda x: x.get("bm25", 0), reverse=True)
    all_results = all_results[: args.limit]

    if args.json:
        print(json.dumps({"query": pattern, "results": all_results}, indent=2))
    else:
        print(f"query: {pattern!r}")
        for r in all_results:
            repo = r.get("repo", "?")
            if "error" in r:
                print(f"  [{repo}] error: {r['error']}")
                continue
            print(f"  [{repo}] {r.get('chunk_type','?')} {r.get('symbol_path','?')}  score={r.get('bm25',0):.3f}")
    return 0


def _cmd_graph(args: argparse.Namespace, ws_path: Path) -> int:
    data = _load_workspace(ws_path)
    members = data["members"]
    if not members:
        print("error: workspace has no members")
        return 2

    # Aggregate repo-maps from each member
    from code_index.commands.repo_map_cmd import build_repo_map
    combined: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for m in members:
        cfg = cfg_mod.load(Path(m["path"]))
        if not cfg.db_path.exists():
            continue
        try:
            conn = db_mod.connect(cfg.db_path)
            try:
                db_mod.ensure_schema(conn, cfg)
                payload = build_repo_map(conn, limit=100)
                for sym in payload.get("symbols", []):
                    sym["repo"] = m["name"]
                    combined.append(sym)
            finally:
                db_mod.close(conn)
        except Exception as exc:
            errors.append({"repo": m["name"], "error": str(exc)})

    combined.sort(key=lambda s: s.get("score", 0), reverse=True)
    combined = combined[: args.limit]

    payload = {
        "workspace": data.get("name", "default"),
        "members": [m["name"] for m in members],
        "symbols": combined,
    }
    if errors:
        payload["errors"] = errors

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"workspace repo-map: {payload['workspace']}")
        for s in combined[:20]:
            repo = s.get("repo", "?")
            print(f"  [{repo}] {s['kind']:<12} {s['canonical_name']:<50} centrality={s.get('centrality',0):.3f}")
        if errors:
            for e in errors:
                print(f"  error [{e['repo']}]: {e['error']}")
    return 0


def register_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "workspace",
        help="manage multi-repo workspaces: init, add, remove, list, status, query, graph",
    )
    p.add_argument("--root", help="repo root (default: cwd / nearest .code_index/)")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument(
        "workspace_action",
        choices=["init", "add", "remove", "list", "status", "query", "graph"],
        help="workspace sub-action",
    )
    p.add_argument(
        "--path",
        dest="path_opt",
        help="path for add",
    )
    p.add_argument(
        "--name",
        dest="name_opt",
        help="name for init/add/remove",
    )
    p.add_argument(
        "--pattern",
        dest="pattern_opt",
        help="query pattern for query",
    )
    p.add_argument(
        "--workspace-file",
        dest="workspace_file",
        help="explicit workspace JSON file path",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="result limit for query/graph (default 20)",
    )
    p.add_argument("--lang", help="filter by language (query only)")
    p.add_argument("--type", help="filter by chunk type (query only)")
    p.set_defaults(func=run)
    return p


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    ws_path = _resolve_workspace_path(args, root_hint)

    if args.workspace_action == "init":
        return _cmd_init(args, ws_path)
    if args.workspace_action == "add":
        return _cmd_add(args, ws_path)
    if args.workspace_action == "remove":
        return _cmd_remove(args, ws_path)
    if args.workspace_action == "list":
        return _cmd_list(args, ws_path)
    if args.workspace_action == "status":
        return _cmd_status(args, ws_path)
    if args.workspace_action == "query":
        if not args.pattern_opt:
            print("error: query requires a pattern")
            return 2
        return _cmd_query(args, ws_path)
    if args.workspace_action == "graph":
        return _cmd_graph(args, ws_path)

    print(f"error: unknown workspace action: {args.workspace_action}")
    return 2
