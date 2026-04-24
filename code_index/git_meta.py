"""Git metadata lookup used by the reindex pipeline.

Populates `files.git_blob_oid`, `files.git_committed_at`, `files.git_author`
on each reindex pass when the repo root contains a `.git/`. Keep the
subprocess contract small and cheap:

- One `git ls-files --stage` invocation per reindex builds a path → blob-oid
  map. That's strictly cheaper than one `git log` per file on large repos.
- One `git log` invocation per file whose mtime actually changed fills in
  the last-modified timestamp + author. Reindexes that don't touch a file
  (mtime-short-circuit hit) never pay for a git log subprocess.
- First subprocess failure poisons the whole resolver — we treat the repo
  as non-git for the remainder of that reindex.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitMeta:
    """Loaded lazily by `resolver_for`.

    `blob_oids` is populated once per reindex (from `git ls-files --stage`);
    `_commit_cache` fills in on demand to avoid N subprocess calls when
    nothing touched a given file.
    """

    root: Path
    enabled: bool = True
    blob_oids: dict[str, str] = field(default_factory=dict)
    _commit_cache: dict[str, tuple[int | None, str | None]] = field(
        default_factory=dict
    )
    _git: str | None = None

    def _git_bin(self) -> str | None:
        if self._git is not None:
            return self._git or None
        exe = shutil.which("git")
        self._git = exe or ""
        return exe

    def blob_oid(self, rel_posix: str) -> str | None:
        if not self.enabled:
            return None
        return self.blob_oids.get(rel_posix)

    def commit_info(self, rel_posix: str) -> tuple[int | None, str | None]:
        """Return (committed_at_unix_ts, author_name) for the last commit
        that touched `rel_posix`. Results cached per-reindex."""
        if not self.enabled:
            return None, None
        if rel_posix in self._commit_cache:
            return self._commit_cache[rel_posix]
        git = self._git_bin()
        if not git:
            self.enabled = False
            return None, None
        try:
            proc = subprocess.run(
                [
                    git,
                    "-C",
                    str(self.root),
                    "log",
                    "-1",
                    "--format=%ct%x00%an",
                    "--",
                    rel_posix,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.enabled = False
            return None, None
        if proc.returncode != 0:
            # First failure poisons the rest of this reindex; git is cheap
            # to re-probe on the next run.
            self.enabled = False
            self._commit_cache[rel_posix] = (None, None)
            return None, None
        line = (proc.stdout or "").strip().split("\n", 1)[0]
        if not line:
            self._commit_cache[rel_posix] = (None, None)
            return None, None
        try:
            ts_str, author = line.split("\x00", 1)
            ts = int(ts_str)
        except (ValueError, IndexError):
            self._commit_cache[rel_posix] = (None, None)
            return None, None
        self._commit_cache[rel_posix] = (ts, author)
        return ts, author


def resolver_for(root: Path) -> GitMeta:
    """Build a per-reindex GitMeta. Non-git repos get an always-disabled
    instance that short-circuits every call."""
    root = root.resolve()
    if not (root / ".git").exists():
        return GitMeta(root=root, enabled=False)
    meta = GitMeta(root=root, enabled=True)
    git = meta._git_bin()
    if not git:
        meta.enabled = False
        return meta
    try:
        proc = subprocess.run(
            [git, "-C", str(root), "ls-files", "--stage"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        meta.enabled = False
        return meta
    if proc.returncode != 0:
        meta.enabled = False
        return meta
    for line in (proc.stdout or "").splitlines():
        # Format: <mode> <blob_oid> <stage>\t<path>
        try:
            meta_part, path = line.split("\t", 1)
        except ValueError:
            continue
        bits = meta_part.split()
        if len(bits) >= 2:
            meta.blob_oids[path] = bits[1]
    return meta
