"""Content hashing utilities.

Two hashes per chunk:
- raw_hash:        sha256 of exact bytes. Any whitespace change flips it.
- normalized_hash: sha256 of whitespace-normalized text. Stable under
                   formatting-only edits. Intentionally conservative — does not
                   parse; just collapses horizontal whitespace and strips
                   blank/trailing whitespace on each line.
"""

from __future__ import annotations

import hashlib
import re

_WS_RUN = re.compile(r"[ \t]+")


def raw_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def normalized_hash(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = _WS_RUN.sub(" ", line).strip()
        if stripped:
            lines.append(stripped)
    normalized = "\n".join(lines) + "\n"
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def worktree_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_uid(payload: str) -> str:
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
