"""Filesystem scan + nested .gitignore loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from code_index.ignore import IgnoreMatcher


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    rel_path: str  # posix
    size: int


def _looks_binary(path: Path, probe_bytes: int = 8192) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(probe_bytes)
    except OSError:
        return True
    return b"\x00" in chunk


def iter_files(
    root: Path,
    matcher: IgnoreMatcher,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> Iterator[ScannedFile]:
    root = root.resolve()
    for entry in _walk(root, matcher):
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            continue
        if _looks_binary(entry):
            continue
        rel = entry.relative_to(root).as_posix()
        yield ScannedFile(path=entry, rel_path=rel, size=size)


def _walk(root: Path, matcher: IgnoreMatcher) -> Iterator[Path]:
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for child in children:
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if matcher.is_ignored(child, is_dir=is_dir):
                continue
            if is_dir:
                # Load nested .gitignore if present.
                nested = child / ".gitignore"
                if nested.is_file():
                    matcher.add_gitignore(nested)
                stack.append(child)
            else:
                yield child
