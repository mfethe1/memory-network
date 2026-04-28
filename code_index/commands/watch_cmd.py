"""`code_index watch`: debounced filesystem reindex.

Uses watchdog when available. Events are coalesced for `debounce_ms`
milliseconds of quiet time; the collected batch is passed to the shared
`pipeline.reindex()` entrypoint — the same path `init` and `update` use.

Keyboard interrupt (Ctrl-C) exits cleanly and runs `PRAGMA optimize`.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.ignore import build as build_matcher
from code_index.locking import LockTimeoutError
from code_index.pipeline import reindex


def _watchdog_available() -> bool:
    try:
        import watchdog  # noqa: F401
        import watchdog.events  # noqa: F401
        import watchdog.observers  # noqa: F401
    except Exception:
        return False
    return True


# Extensions we never want to reindex on filesystem events — compiled,
# packaged, binary, or the index DB itself.
_BINARY_EXTS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".lib",
        ".obj",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".tif",
        ".tiff",
        ".svg",
        ".pdf",
        ".psd",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".webm",
        ".wav",
        ".ogg",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".jar",
        ".whl",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".db-wal",
        ".db-shm",
        ".db-journal",
        ".sqlite-wal",
        ".sqlite-shm",
        ".sqlite-journal",
        ".sqlite3-wal",
        ".sqlite3-shm",
        ".parquet",
        ".feather",
        ".arrow",
        ".lock",
        ".class",
    }
)

# Directory segments that should never produce watch events for code_index.
# Checked against any segment of the repo-relative path.
_BLOCK_DIR_SEGMENTS = frozenset(
    {
        ".git",
        ".code_index",
        ".claude",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        ".venv",
        "venv",
        "env",
        ".env",
        "virtualenv",
        "node_modules",
        ".yarn",
        ".pnpm-store",
        "dist",
        "build",
        "target",
        "out",
        ".next",
        ".nuxt",
        ".idea",
        ".vscode",
        ".cache",
        ".mypy",
        ".coverage",
    }
)


def should_skip_watch_event(rel_posix: str) -> tuple[bool, str | None]:
    """Return (skip, reason). Pure function; safe to call from tests.

    `rel_posix` is a repo-relative posix path.
    """
    if not rel_posix:
        return True, "empty_path"
    if rel_posix.startswith("/") or rel_posix.startswith("\\"):
        return True, "absolute_path"
    base = rel_posix.rsplit("/", 1)[-1]
    # Editor/OS temp artifacts.
    if base.startswith(".#") or base.startswith("#"):
        return True, "editor_temp"
    if base.endswith("~") or base.startswith(".~"):
        return True, "editor_temp"
    if base in {".DS_Store", "Thumbs.db", "desktop.ini"}:
        return True, "os_junk"
    # Vim swap files.
    if base.endswith(".swp") or base.endswith(".swo") or base.endswith(".swn"):
        return True, "vim_swap"
    # Segment blocklist.
    segments = rel_posix.split("/")
    for seg in segments[:-1]:  # directory parts only
        if seg in _BLOCK_DIR_SEGMENTS:
            return True, f"blocked_dir:{seg}"
        if seg.startswith(".") and seg not in {".", ".."}:
            # Hidden dir — not always junk, but we rely on the repo ignore
            # matcher to decide; here we only block the well-known ones above.
            pass
    # Extension blocklist.
    ext = ""
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1].lower()
    if ext in _BINARY_EXTS:
        return True, f"binary_ext:{ext}"
    return False, None


def _looks_like_binary_path(path: Path) -> bool:
    """Retained for callers outside the watch pipeline."""
    return path.suffix.lower() in _BINARY_EXTS


def run(args: argparse.Namespace) -> int:
    if not _watchdog_available():
        payload = {
            "error": "watchdog is not installed",
            "hint": "install with: pip install 'code-index[watch]'  (or: pip install watchdog)",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"error: {payload['error']}")
            print(f"hint:  {payload['hint']}")
        return 2

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    matcher = build_matcher(
        root, extra=config.extra_ignore, include_hidden=config.include_hidden
    )
    debounce_s = max(0.05, args.debounce_ms / 1000.0)

    pending: set[Path] = set()
    pending_lock = threading.Lock()
    last_event_at: list[float] = [0.0]
    stop_event = threading.Event()

    resolved_root = root.resolve()

    class Handler(FileSystemEventHandler):
        def _accept(self, src: str | bytes) -> Path | None:
            if isinstance(src, bytes):
                src = src.decode("utf-8", "replace")
            p = Path(src)
            try:
                p = p.resolve()
                rel = p.relative_to(resolved_root)
            except (OSError, ValueError):
                return None
            try:
                is_dir = p.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                return None
            rel_posix = rel.as_posix()
            skip, _reason = should_skip_watch_event(rel_posix)
            if skip:
                return None
            if matcher.is_ignored(p, is_dir=False):
                return None
            return p

        def on_any_event(self, event):  # type: ignore[override]
            # Include deletes so the pipeline can tombstone.
            target = self._accept(event.src_path)
            if target is None:
                return
            with pending_lock:
                pending.add(target)
                last_event_at[0] = time.time()

    handler = Handler()
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()

    if args.json:
        print(
            json.dumps(
                {
                    "event": "watch_started",
                    "root": str(root),
                    "debounce_ms": args.debounce_ms,
                }
            )
        )
    else:
        print(f"watching {root} (debounce {args.debounce_ms}ms, Ctrl-C to stop)")

    def _flush() -> None:
        with pending_lock:
            batch = list(pending)
            pending.clear()
        if not batch:
            return
        conn = db_mod.connect(config.db_path)
        try:
            db_mod.apply_schema(conn)
            try:
                stats = reindex(
                    conn, config, paths=batch, event_source="watch", force=False
                )
            except LockTimeoutError as exc:
                # Another writer owns the repo right now. Re-queue and move on;
                # the next debounce window will retry.
                with pending_lock:
                    for p in batch:
                        pending.add(p)
                payload_err = {
                    "event": "watch_flush_deferred",
                    "batch_size": len(batch),
                    "reason": "writer_lock_contention",
                    "lock_path": str(exc.lock_path),
                }
                if args.json:
                    print(json.dumps(payload_err))
                else:
                    print(
                        f"watch: deferred flush of {len(batch)} paths "
                        f"(lock held at {exc.lock_path})"
                    )
                return
        finally:
            db_mod.close(conn)
        payload = {
            "event": "watch_flush",
            "batch_size": len(batch),
            "stats": stats.to_dict(),
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print(
                f"reindexed {len(batch)} paths: "
                f"parsed={stats.files_parsed} "
                f"chunks+{stats.chunks_created}~{stats.chunks_updated}-{stats.chunks_tombstoned} "
                f"relations+{stats.relations_inserted} backfill+{stats.relations_backfilled}"
            )

    try:
        while not stop_event.is_set():
            time.sleep(0.1)
            with pending_lock:
                idle = time.time() - last_event_at[0]
                has_pending = bool(pending)
            if has_pending and last_event_at[0] > 0 and idle >= debounce_s:
                _flush()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=2.0)
        # Final flush for anything queued but not yet debounced.
        _flush()
    if args.json:
        print(json.dumps({"event": "watch_stopped"}))
    else:
        print("watch stopped")
    return 0
