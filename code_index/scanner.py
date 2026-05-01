"""Filesystem scanner that walks a directory tree and yields text files.

Respects `.gitignore` patterns (including nested `.gitignore` files) and
skips binary files and files larger than a configurable size threshold.

Main exports:
    ScannedFile -- Immutable record for a discovered file (path, rel_path, size).
    iter_files  -- Yield ``ScannedFile`` objects for every non-ignored text file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from code_index.ignore import IgnoreMatcher


@dataclass(frozen=True)
class ScannedFile:
    """Immutable value object representing a single discovered file.

    Attributes:
        path: Absolute filesystem path.
        rel_path: Repository-relative path using forward slashes (POSIX).
        size: File size in bytes.
    """

    path: Path
    rel_path: str  # posix
    size: int


def _looks_binary(path: Path, probe_bytes: int = 8192) -> bool:
    """Heuristically decide whether *path* points to a binary file.

    Reads up to *probe_bytes* from the start of the file and looks for a
    null byte (``\\x00``), which is a strong signal of binary content.
    If the file cannot be read, it is treated as binary so that callers
    can safely skip it.
    """
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
    """Walk *root* and yield ``ScannedFile`` records for readable text files.

    The walk respects the ignore rules held in *matcher* (e.g. from root
    ``.gitignore`` files).  Nested ``.gitignore`` files are loaded on the
    fly by :func:`_walk`.  Files are skipped when they are unreadable,
    exceed *max_bytes*, or appear to be binary.

    Args:
        root: Directory to scan.  Resolved to an absolute path internally.
        matcher: ``IgnoreMatcher`` that decides which paths to ignore.
        max_bytes: Upper size limit in bytes (default 2 MiB).

    Yields:
        ``ScannedFile`` for each non-ignored text file under *root*.
    """
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
    """Depth-first walk of *root* yielding every non-ignored path.

    As directories are entered, any nested ``.gitignore`` file found inside
    them is registered with *matcher* so that deeper paths respect the new
    rules.

    Args:
        root: Directory to traverse.
        matcher: ``IgnoreMatcher`` that decides which paths to ignore.

    Yields:
        ``Path`` objects for files (directories are never yielded).
    """
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
