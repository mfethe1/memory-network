"""Lexical fast path: ripgrep when present, Python regex fallback otherwise.

Always returns a list of {file, line, column, text} hits with absolute-ish
paths normalized to repo-relative posix.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from code_index.config import Config
from code_index.ignore import IgnoreMatcher, build as build_matcher
from code_index.scanner import iter_files
from code_index.search.rg_discovery import ResolvedRg, resolve as resolve_rg


def resolve_ripgrep(config: Config | None = None) -> ResolvedRg:
    return resolve_rg(config_rg_path=config.rg_path if config else None)


def rg_available(config: Config | None = None) -> bool:
    return resolve_ripgrep(config).path is not None


def _normalize(root: Path, path_str: str) -> str:
    p = Path(path_str)
    try:
        if not p.is_absolute():
            p = (root / p).resolve()
        else:
            p = p.resolve()
        return p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return path_str.replace("\\", "/")


def _rg_search(
    *,
    rg_path: str,
    root: Path,
    pattern: str,
    path_glob: str | None,
    max_count: int,
    case_insensitive: bool,
    fixed_strings: bool,
) -> list[dict]:
    cmd = [rg_path, "--json", "--max-count", str(max_count)]
    if case_insensitive:
        cmd.append("-i")
    if fixed_strings:
        cmd.append("-F")
    if path_glob:
        cmd.extend(["-g", path_glob])
    cmd.extend(["--", pattern, str(root)])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(root),
        check=False,
    )
    hits: list[dict] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        path_obj = data.get("path", {}) or {}
        path_str = path_obj.get("text") or ""
        rel = _normalize(root, path_str)
        text_obj = data.get("lines", {}) or {}
        text = text_obj.get("text", "")
        line_no = data.get("line_number")
        submatches = data.get("submatches", []) or []
        col = submatches[0].get("start", 0) + 1 if submatches else 1
        hits.append(
            {
                "file": rel,
                "line": line_no,
                "column": col,
                "text": text.rstrip("\n"),
                "engine": "ripgrep",
            }
        )
    return hits


def _python_search(
    *,
    root: Path,
    matcher: IgnoreMatcher,
    pattern: str,
    path_glob: str | None,
    max_count: int,
    case_insensitive: bool,
    fixed_strings: bool,
    max_bytes: int,
) -> list[dict]:
    flags = re.IGNORECASE if case_insensitive else 0
    if fixed_strings:
        regex = re.compile(re.escape(pattern), flags)
    else:
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc

    hits: list[dict] = []
    glob_pat = path_glob or ""
    for scanned in iter_files(root, matcher, max_bytes=max_bytes):
        if glob_pat and not _glob_match(scanned.rel_path, glob_pat):
            continue
        try:
            text = scanned.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_matches = 0
        for idx, line in enumerate(text.splitlines(), start=1):
            m = regex.search(line)
            if m:
                hits.append(
                    {
                        "file": scanned.rel_path,
                        "line": idx,
                        "column": m.start() + 1,
                        "text": line,
                        "engine": "python-re",
                    }
                )
                file_matches += 1
                if file_matches >= max_count:
                    break
    return hits


def _glob_match(rel_posix: str, pattern: str) -> bool:
    import fnmatch

    if fnmatch.fnmatchcase(rel_posix, pattern):
        return True
    last = rel_posix.rsplit("/", 1)[-1]
    return fnmatch.fnmatchcase(last, pattern)


def grep(
    config: Config,
    *,
    pattern: str,
    path_glob: str | None = None,
    max_count: int = 50,
    case_insensitive: bool = False,
    fixed_strings: bool = False,
) -> dict:
    root = config.root
    resolved = resolve_ripgrep(config)
    if resolved.path:
        hits = _rg_search(
            rg_path=resolved.path,
            root=root,
            pattern=pattern,
            path_glob=path_glob,
            max_count=max_count,
            case_insensitive=case_insensitive,
            fixed_strings=fixed_strings,
        )
        engine = "ripgrep"
    else:
        matcher = build_matcher(
            root,
            extra=config.extra_ignore,
            include_hidden=config.include_hidden,
        )
        hits = _python_search(
            root=root,
            matcher=matcher,
            pattern=pattern,
            path_glob=path_glob,
            max_count=max_count,
            case_insensitive=case_insensitive,
            fixed_strings=fixed_strings,
            max_bytes=config.max_file_bytes,
        )
        engine = "python-re"
    return {
        "engine": engine,
        "pattern": pattern,
        "count": len(hits),
        "hits": hits,
        "rg_resolution": resolved.to_dict(),
    }
