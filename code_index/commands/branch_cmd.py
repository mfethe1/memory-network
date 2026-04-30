"""`code_index branch`: compare git branches and analyse change impact.

Supports:
  branch list                — local branches + index status
  branch diff <target>       — files changed between HEAD and target branch
  branch files <target>      — changed files with add/modify/delete categories
  branch impact <target>     — symbols impacted by changes between branches
  branch compare <a> <b>     — compare any two branches or commits
"""


from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.commands.impact_cmd import compute_impact


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git_bin() -> str | None:
    return shutil.which("git")


def _git(root: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    git = _git_bin()
    if not git:
        raise RuntimeError("git not found on PATH")
    proc = subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() if proc.stderr else "git command failed")
    return proc


def _current_branch(root: Path) -> str | None:
    try:
        proc = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    except RuntimeError:
        return None
    return proc.stdout.strip() or None


def _branches(root: Path) -> list[dict[str, Any]]:
    try:
        proc = _git(root, "branch", "--format=%(refname:short)\t%(objectname:short)\t%(committerdate:unix)")
    except RuntimeError:
        return []
    out: list[dict[str, Any]] = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append({
                "name": parts[0],
                "sha": parts[1],
                "committed_at": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None,
            })
    return out


def _diff_files(root: Path, base_ref: str, target_ref: str | None = None) -> list[dict[str, Any]]:
    """Return list of {status, path, old_path?} for git diff between refs."""
    if target_ref is None:
        # diff base_ref..HEAD
        range_spec = f"{base_ref}..HEAD"
    else:
        range_spec = f"{base_ref}..{target_ref}"
    proc = _git(root, "diff", "--name-status", "-z", "-C", range_spec)
    out: list[dict[str, Any]] = []
    entries = proc.stdout.split("\x00")
    i = 0
    while i < len(entries):
        if not entries[i]:
            i += 1
            continue
        status = entries[i][0]
        if status == "R":
            if i + 2 < len(entries):
                out.append({"status": "renamed", "path": entries[i + 2], "old_path": entries[i + 1]})
                i += 3
                continue
        elif i + 1 < len(entries):
            smap = {"A": "added", "M": "modified", "D": "deleted", "T": "type-changed"}
            out.append({"status": smap.get(status, "unknown"), "path": entries[i + 1]})
            i += 2
            continue
        out.append({"status": "unknown", "path": entries[i]})
        i += 1
    return out


def _merge_base(root: Path, a: str, b: str) -> str | None:
    proc = _git(root, "merge-base", a, b)
    return proc.stdout.strip() or None


def _commits_between(root: Path, base_ref: str, target_ref: str | None = None) -> list[dict[str, Any]]:
    if target_ref is None:
        range_spec = f"{base_ref}..HEAD"
    else:
        range_spec = f"{base_ref}..{target_ref}"
    proc = _git(root, "log", "--format=%H\t%ct\t%an\t%s", range_spec)
    out: list[dict[str, Any]] = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split("\t", 3)
        if len(parts) >= 3:
            out.append({
                "sha": parts[0],
                "committed_at": int(parts[1]) if parts[1].isdigit() else None,
                "author": parts[2],
                "summary": parts[3] if len(parts) > 3 else "",
            })
    return out


# ---------------------------------------------------------------------------
# Index cross-reference
# ---------------------------------------------------------------------------

def _symbols_in_file(conn: sqlite3.Connection, file_path: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.symbol_uid, s.kind, s.canonical_name, s.display_name,
               o.start_line, o.end_line
          FROM symbols s
          JOIN occurrences o ON o.symbol_pk = s.symbol_pk
          JOIN files f ON f.file_pk = o.file_pk
         WHERE f.file_path = ?
           AND o.role = 'definition'
           AND s.deleted_at IS NULL
         ORDER BY o.start_line ASC
        """,
        (file_path,),
    ).fetchall()
    return [dict(r) for r in rows]


def _chunks_in_file(conn: sqlite3.Connection, file_path: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.chunk_uid, c.chunk_type, c.symbol_name, c.start_line, c.end_line,
               c.content
          FROM chunks c
          JOIN files f ON f.file_pk = c.file_pk
         WHERE f.file_path = ?
           AND c.deleted_at IS NULL
         ORDER BY c.start_line ASC
        """,
        (file_path,),
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass
class BranchDiffResult:
    base_branch: str | None
    target_branch: str | None
    merge_base: str | None
    commits: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    symbols_changed: list[dict[str, Any]] = field(default_factory=list)
    impacted_symbols: list[dict[str, Any]] = field(default_factory=list)
    impacted_files: list[str] = field(default_factory=list)


def _build_diff(
    conn: sqlite3.Connection,
    root: Path,
    base_ref: str,
    target_ref: str | None,
    max_depth: int = 2,
    include_imports: bool = True,
) -> BranchDiffResult:
    current = _current_branch(root)
    mb = _merge_base(root, base_ref, target_ref or "HEAD")
    commits = _commits_between(root, base_ref, target_ref)
    files = _diff_files(root, base_ref, target_ref)

    result = BranchDiffResult(
        base_branch=base_ref,
        target_branch=target_ref or current,
        merge_base=mb,
        commits=commits,
        changed_files=files,
    )

    # Cross-reference with index: symbols in changed files
    sym_uids_seen: set[str] = set()
    for f in files:
        if f["status"] == "deleted":
            continue
        syms = _symbols_in_file(conn, f["path"])
        for s in syms:
            if s["symbol_uid"] not in sym_uids_seen:
                sym_uids_seen.add(s["symbol_uid"])
                result.symbols_changed.append({
                    "symbol_uid": s["symbol_uid"],
                    "canonical_name": s["canonical_name"],
                    "kind": s["kind"],
                    "file": f["path"],
                    "start_line": s["start_line"],
                })

    # Impact analysis: for each changed symbol, compute impact
    impacted_sym_uids: set[str] = set()
    impacted_files_set: set[str] = set()
    for s in result.symbols_changed:
        row = conn.execute(
            "SELECT symbol_pk FROM symbols WHERE symbol_uid = ?",
            (s["symbol_uid"],),
        ).fetchone()
        if row is None:
            continue
        impact = compute_impact(
            conn, int(row["symbol_pk"]), max_depth=max_depth, include_imports=include_imports
        )
        for sym in impact.get("impacted_symbols", []):
            uid = sym["symbol_uid"]
            if uid not in impacted_sym_uids and uid not in sym_uids_seen:
                impacted_sym_uids.add(uid)
                result.impacted_symbols.append(sym)
                if sym.get("def_file"):
                    impacted_files_set.add(sym["def_file"])

    result.impacted_files = sorted(impacted_files_set)
    return result


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace, config: cfg_mod.Config) -> int:
    root = config.root
    branches = _branches(root)
    current = _current_branch(root)
    out = []
    for b in branches:
        out.append({
            "name": b["name"],
            "current": b["name"] == current,
            "sha": b["sha"],
            "committed_at": b.get("committed_at"),
        })
    if args.json:
        print(json.dumps({"branches": out, "current": current, "root": str(root)}, indent=2))
    else:
        print(f"branches in {root}")
        for b in out:
            marker = "*" if b["current"] else " "
            print(f"  {marker} {b['name']:<30} {b['sha']}")
    return 0


def _cmd_diff(args: argparse.Namespace, config: cfg_mod.Config) -> int:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        base = args.target
        target = args.to_ref
        result = _build_diff(
            conn, config.root, base, target,
            max_depth=args.max_depth,
            include_imports=not args.no_imports,
        )
        payload = {
            "root": str(config.root),
            "base_branch": result.base_branch,
            "target_branch": result.target_branch,
            "merge_base": result.merge_base,
            "commits": result.commits,
            "changed_files": result.changed_files,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"diff {result.base_branch}..{result.target_branch or 'HEAD'}  (merge-base: {result.merge_base})")
            print(f"  {len(result.commits)} commits, {len(result.changed_files)} changed files")
            by_status: dict[str, list[str]] = {}
            for f in result.changed_files:
                by_status.setdefault(f["status"], []).append(f["path"])
            for st in ("added", "modified", "deleted", "renamed", "type-changed", "unknown"):
                if st in by_status:
                    print(f"\n  {st} ({len(by_status[st])}):")
                    for p in by_status[st][:20]:
                        print(f"    {p}")
                    if len(by_status[st]) > 20:
                        print(f"    ... +{len(by_status[st]) - 20} more")
    finally:
        db_mod.close(conn)
    return 0


def _cmd_files(args: argparse.Namespace, config: cfg_mod.Config) -> int:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        base = args.target
        target = args.to_ref
        files = _diff_files(config.root, base, target)
        # Cross-reference with index chunks
        enriched: list[dict[str, Any]] = []
        for f in files:
            entry: dict[str, Any] = {"status": f["status"], "path": f["path"]}
            if f.get("old_path"):
                entry["old_path"] = f["old_path"]
            if f["status"] != "deleted":
                chunks = _chunks_in_file(conn, f["path"])
                entry["chunks"] = [
                    {"type": c["chunk_type"], "symbol": c["symbol_name"], "lines": (c["start_line"], c["end_line"])}
                    for c in chunks
                ]
                entry["symbol_count"] = len(entry["chunks"])
            enriched.append(entry)
        payload = {
            "root": str(config.root),
            "base_branch": base,
            "target_branch": target or _current_branch(config.root),
            "files": enriched,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"changed files {base}..{target or 'HEAD'}")
            for e in enriched:
                chunks = f"  ({e.get('symbol_count', 0)} chunks)" if "symbol_count" in e else ""
                old = f" <- {e['old_path']}" if e.get("old_path") else ""
                print(f"  [{e['status']:<10}] {e['path']}{old}{chunks}")
    finally:
        db_mod.close(conn)
    return 0


def _cmd_impact(args: argparse.Namespace, config: cfg_mod.Config) -> int:
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.ensure_schema(conn, config)
        result = _build_diff(
            conn, config.root, args.target, args.to_ref,
            max_depth=args.max_depth,
            include_imports=not args.no_imports,
        )
        payload = {
            "root": str(config.root),
            "base_branch": result.base_branch,
            "target_branch": result.target_branch,
            "merge_base": result.merge_base,
            "commits": result.commits,
            "changed_files": result.changed_files,
            "symbols_changed": result.symbols_changed,
            "impacted_symbols": result.impacted_symbols,
            "impacted_files": result.impacted_files,
            "summary": {
                "commits": len(result.commits),
                "changed_files": len(result.changed_files),
                "symbols_changed": len(result.symbols_changed),
                "impacted_symbols": len(result.impacted_symbols),
                "impacted_files": len(result.impacted_files),
            },
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"impact of changes {result.base_branch}..{result.target_branch or 'HEAD'}"
            )
            print(f"  merge-base: {result.merge_base}")
            print(f"  commits: {len(result.commits)}")
            print(f"  changed files: {len(result.changed_files)}")
            print(f"  symbols changed: {len(result.symbols_changed)}")
            if result.impacted_symbols:
                print(f"\n  impacted symbols ({len(result.impacted_symbols)}):")
                for s in result.impacted_symbols[:20]:
                    loc = f"{s['def_file']}:{s['def_line']}" if s.get("def_file") else "?"
                    print(
                        f"    [{s['confidence']}] depth={s['depth']} {s['canonical_name']}  ({loc})"
                    )
                if len(result.impacted_symbols) > 20:
                    print(f"    ... +{len(result.impacted_symbols) - 20} more")
            if result.impacted_files:
                print(f"\n  impacted files ({len(result.impacted_files)}):")
                for p in result.impacted_files[:20]:
                    print(f"    {p}")
                if len(result.impacted_files) > 20:
                    print(f"    ... +{len(result.impacted_files) - 20} more")
    finally:
        db_mod.close(conn)
    return 0


def _cmd_compare(args: argparse.Namespace, config: cfg_mod.Config) -> int:
    # Compare two arbitrary refs
    return _cmd_diff(args, config)


def register_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "branch",
        help="compare git branches: diff, impact, files, list, compare",
    )
    p.add_argument("--root", help="repo root (default: cwd / nearest .code_index/)")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument(
        "branch_action",
        choices=["list", "diff", "files", "impact", "compare"],
        help="branch sub-action",
    )
    p.add_argument("target", nargs="?", help="target branch or commit ref")
    p.add_argument("to_ref", nargs="?", help="second ref for compare")
    p.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="transitive impact depth (default 2)",
    )
    p.add_argument(
        "--no-imports",
        action="store_true",
        help="exclude medium-confidence imports edges from impact",
    )
    p.set_defaults(func=run)
    return p


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    # Validate git repository state (list gracefully handles absence elsewhere)
    if args.branch_action != "list":
        try:
            _git(config.root, "rev-parse", "--git-dir")
        except RuntimeError:
            print("error: not a git repository")
            return 2
        try:
            _git(config.root, "rev-parse", "HEAD")
        except RuntimeError:
            print("error: repository has no commits")
            return 2

    if args.branch_action == "list":
        return _cmd_list(args, config)
    if args.branch_action == "diff":
        if not args.target:
            print("error: diff requires a target branch or commit")
            return 2
        return _cmd_diff(args, config)
    if args.branch_action == "files":
        if not args.target:
            print("error: files requires a target branch or commit")
            return 2
        return _cmd_files(args, config)
    if args.branch_action == "impact":
        if not args.target:
            print("error: impact requires a target branch or commit")
            return 2
        return _cmd_impact(args, config)
    if args.branch_action == "compare":
        if not args.target or not args.to_ref:
            print("error: compare requires two refs: `code_index branch compare <base> <target>`")
            return 2
        return _cmd_compare(args, config)

    print(f"error: unknown branch action: {args.branch_action}")
    return 2
