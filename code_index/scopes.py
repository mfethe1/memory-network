"""Root-relative scope helpers for commands that target a repo subtree."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SCOPE_SELECTED_PATH_LIMIT = 50


@dataclass(frozen=True)
class ScopeSelection:
    path: str
    explicit: bool

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "explicit": self.explicit}


def normalize_path(path: str | Path) -> str:
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text or "."


def normalize_repo_path(root: Path, path: str | Path, *, label: str = "path") -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    root = root.resolve()
    raw_path = Path(text).expanduser()
    resolved = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
    try:
        rel_path = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside root") from exc
    return normalize_path(rel_path.as_posix())


def resolve_scope(root: Path, scope: str | Path | None) -> ScopeSelection:
    if scope is None or not str(scope).strip():
        return ScopeSelection(path=".", explicit=False)
    root = root.resolve()
    path = Path(str(scope).strip()).expanduser()
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        rel_path = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("scope must be inside root") from exc
    if not resolved.exists():
        raise ValueError(f"scope does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"scope is not a directory: {resolved}")
    return ScopeSelection(path=normalize_path(rel_path.as_posix()), explicit=True)


def resolve_scope_from_args(root: Path, args: argparse.Namespace) -> ScopeSelection:
    cached = getattr(args, "_resolved_scope", None)
    if isinstance(cached, ScopeSelection):
        return cached
    selection = resolve_scope(root, getattr(args, "scope", None))
    setattr(args, "_resolved_scope", selection)
    return selection


def path_in_scope(path: str, scope: str | ScopeSelection | None) -> bool:
    if scope is None:
        return True
    scope_path = scope.path if isinstance(scope, ScopeSelection) else normalize_path(scope)
    if scope_path == ".":
        return True
    normalized = normalize_path(path)
    return normalized == scope_path or normalized.startswith(f"{scope_path}/")


def indexed_file_paths_for_scope(
    conn: sqlite3.Connection,
    scope: str | ScopeSelection,
    *,
    limit: int | None = None,
) -> list[str]:
    scope_path = scope.path if isinstance(scope, ScopeSelection) else normalize_path(scope)
    params: list[object]
    if scope_path == ".":
        params = []
        rows = conn.execute(
            """
            SELECT file_path
              FROM files
             WHERE deleted_at IS NULL
             ORDER BY file_path
            """
        ).fetchall()
    else:
        params = [scope_path, f"{scope_path}/%"]
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(int(limit))
        rows = conn.execute(
            f"""
            SELECT file_path
              FROM files
             WHERE deleted_at IS NULL
               AND (file_path = ? OR file_path LIKE ?)
             ORDER BY file_path
             {limit_sql}
            """,
            params,
        ).fetchall()
    if scope_path == "." and limit is not None:
        rows = rows[: int(limit)]
    return [str(row["file_path"]) for row in rows]


def validate_paths_in_scope(
    paths: list[str] | tuple[str, ...],
    scope: str | ScopeSelection | None,
    *,
    label: str = "path",
) -> None:
    if scope is None:
        return
    scope_path = scope.path if isinstance(scope, ScopeSelection) else normalize_path(scope)
    if scope_path == ".":
        return
    for path in paths:
        if path and not path_in_scope(path, scope_path):
            raise ValueError(f"{label} is outside scope: {path}")


def apply_scope_to_request(
    conn: sqlite3.Connection,
    root: Path,
    request: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    scope_selection = resolve_scope_from_args(root, args)
    out = dict(request)
    selected_paths = [
        normalize_repo_path(root, path, label="selected path")
        for path in list(out.get("selected_paths") or [])
        if str(path or "").strip()
    ]
    validate_paths_in_scope(selected_paths, scope_selection, label="selected path")

    selected_nodes = [str(item) for item in list(out.get("selected_nodes") or []) if item]
    _validate_selected_nodes_in_scope(selected_nodes, scope_selection)

    node = out.get("node") if isinstance(out.get("node"), dict) else {}
    if node.get("path"):
        node = dict(node)
        node["path"] = normalize_repo_path(root, node["path"], label="node path")
        validate_paths_in_scope([str(node["path"])], scope_selection, label="node path")
        out["node"] = node

    if scope_selection.explicit and not selected_paths and not selected_nodes and not node:
        selected_paths = indexed_file_paths_for_scope(
            conn,
            scope_selection,
            limit=DEFAULT_SCOPE_SELECTED_PATH_LIMIT,
        )
        if selected_paths:
            selected_nodes = [f"file:{selected_paths[0]}"]
            out["node"] = {
                "id": f"file:{selected_paths[0]}",
                "path": selected_paths[0],
                "kind": "file",
            }
        elif scope_selection.path != ".":
            selected_nodes = [f"dir:{scope_selection.path}"]
            out["node"] = {
                "id": f"dir:{scope_selection.path}",
                "path": scope_selection.path,
                "kind": "directory",
            }

    out["selected_paths"] = _dedupe(selected_paths)
    out["selected_nodes"] = _dedupe(selected_nodes)
    metadata = dict(out.get("metadata") or {})
    metadata["selected_paths"] = list(out["selected_paths"])
    metadata["scope"] = scope_selection.to_dict()
    out["metadata"] = metadata
    out["scope"] = scope_selection.to_dict()
    return out


def _validate_selected_nodes_in_scope(
    selected_nodes: list[str],
    scope: ScopeSelection,
) -> None:
    if scope.path == ".":
        return
    for node_id in selected_nodes:
        path = ""
        if node_id.startswith("file:"):
            path = node_id.removeprefix("file:")
        elif node_id.startswith("dir:"):
            path = node_id.removeprefix("dir:")
        if path and not path_in_scope(path, scope):
            raise ValueError(f"selected node is outside scope: {node_id}")


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out
