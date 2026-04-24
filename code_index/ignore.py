"""Gitignore-ish path filtering.

Scope limits:
- Handles literal names, `*`, `**`, leading `/`, trailing `/`, and leading `!`
  for negation.
- Does not implement character classes (`[abc]`) or `**/` in the middle of a
  path except as a literal `**` segment.
- Loads `.gitignore` files from the repo root downward; nested gitignores are
  supported by concatenating them in directory order.

Always-skip patterns are applied first and cannot be negated; config
`extra_ignore` is applied as additional gitignore rules.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ALWAYS_SKIP = (
    ".git",
    ".code_index",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "target",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".eggs",
)


@dataclass(frozen=True)
class _Rule:
    pattern: str
    negate: bool
    dir_only: bool
    anchored: bool


def _parse_line(raw: str) -> _Rule | None:
    line = raw.rstrip("\r\n")
    if not line or line.lstrip().startswith("#"):
        return None
    negate = False
    if line.startswith("!"):
        negate = True
        line = line[1:]
    line = line.strip()
    if not line:
        return None
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    anchored = line.startswith("/")
    if anchored:
        line = line[1:]
    return _Rule(pattern=line, negate=negate, dir_only=dir_only, anchored=anchored)


def _match(rule: _Rule, rel_posix: str, is_dir: bool) -> bool:
    if rule.dir_only and not is_dir:
        return False
    pattern = rule.pattern
    # Match against the full relative path and all parent subpaths if unanchored.
    candidates = [rel_posix]
    if not rule.anchored:
        parts = rel_posix.split("/")
        for i in range(len(parts)):
            candidates.append("/".join(parts[i:]))
    # Expand "**" as a glob that fnmatch handles for the whole pattern.
    for candidate in candidates:
        if "**" in pattern:
            # Replace `**` with `*` for fnmatch; then require at least one
            # of the segment matches to succeed.
            pat = pattern.replace("**/", "").replace("/**", "").replace("**", "*")
            if fnmatch.fnmatchcase(candidate, pat):
                return True
        if fnmatch.fnmatchcase(candidate, pattern):
            return True
        # Also try basename for unanchored non-slash patterns.
        if not rule.anchored and "/" not in pattern:
            if fnmatch.fnmatchcase(candidate.split("/")[-1], pattern):
                return True
    return False


@dataclass
class IgnoreMatcher:
    root: Path
    rules: list[_Rule] = field(default_factory=list)
    include_hidden: bool = False

    def add_gitignore(self, gitignore_path: Path) -> None:
        if not gitignore_path.is_file():
            return
        for raw in gitignore_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            rule = _parse_line(raw)
            if rule is not None:
                self.rules.append(rule)

    def add_patterns(self, patterns: list[str]) -> None:
        for raw in patterns:
            rule = _parse_line(raw)
            if rule is not None:
                self.rules.append(rule)

    def _always_skip(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return False
        parts = rel.parts
        for seg in parts:
            if seg in ALWAYS_SKIP:
                return True
        return False

    def is_ignored(self, path: Path, is_dir: bool | None = None) -> bool:
        if self._always_skip(path):
            return True
        if is_dir is None:
            is_dir = path.is_dir()
        rel = path.relative_to(self.root) if path.is_absolute() else path
        rel_posix = str(PurePosixPath(*rel.parts))
        if not self.include_hidden:
            for seg in rel.parts:
                if seg.startswith(".") and seg not in {".", ".."}:
                    return True
        ignored = False
        for rule in self.rules:
            if _match(rule, rel_posix, is_dir):
                ignored = not rule.negate
        return ignored


def build(
    root: Path, extra: list[str] | None = None, include_hidden: bool = False
) -> IgnoreMatcher:
    matcher = IgnoreMatcher(root=root, include_hidden=include_hidden)
    matcher.add_gitignore(root / ".gitignore")
    if extra:
        matcher.add_patterns(extra)
    return matcher
