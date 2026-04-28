"""Cross-process advisory writer lock for code_index.

Only one writer at a time (reindex, embed, rebuild-fts, rebuild-tests,
install-hooks, watch-flush, MCP update/rebuild). Readers (symbol, query,
grep, impact, tests, similar, repo-map, ask, doctor) don't take the lock
— SQLite WAL already handles reader-writer separation at the SQL level.

Implementation:
- POSIX: `fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` on
  `config.lock_path`. On timeout, raise `LockTimeoutError`.
- Windows: `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` on the same path.
- The lock file is created on demand and never deleted — the OS releases
  the lock when the fd closes. Empty file is fine.

Retries every `poll_ms` ms (default 100) until `timeout_s` elapses. Uses
a simple exponential backoff capped at 500ms so we don't thundering-herd
when multiple writers wake up together.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Iterator


class LockTimeoutError(RuntimeError):
    """Raised when the writer lock can't be acquired within the timeout."""

    def __init__(self, lock_path: Path, timeout_s: float) -> None:
        super().__init__(
            f"another code_index writer holds the lock at {lock_path!s} "
            f"(waited {timeout_s:.1f}s)"
        )
        self.lock_path = lock_path
        self.timeout_s = timeout_s


def _try_lock_posix(fd: int) -> bool:
    import fcntl

    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError):
        return False


def _unlock_posix(fd: int) -> None:
    import fcntl

    try:
        fcntl.lockf(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _try_lock_win(fd: int) -> bool:
    import msvcrt

    try:
        # msvcrt.locking locks `nbytes` starting at the CURRENT file cursor.
        # Always seek to byte 0 so every acquirer contends over the SAME byte.
        os.lseek(fd, 0, os.SEEK_SET)
        # LK_NBLCK: non-blocking exclusive lock on 1 byte.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock_win(fd: int) -> None:
    import msvcrt

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


_HELD_COUNT: dict[str, int] = {}
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(lock_key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[lock_key] = lock
        return lock


@contextlib.contextmanager
def writer_lock(
    config, *, timeout_s: float = 30.0, poll_ms: int = 100
) -> Iterator[None]:
    """Acquire an exclusive writer lock on `config.lock_path`.

    Re-entrant within a single Python process. The first acquire takes the
    OS-level lock; nested acquires bump an in-process refcount. The OS lock
    is released to OTHER processes only when the outermost context exits.

    Why re-entrancy is load-bearing: both `reindex()` and the command
    wrappers that call it (`rebuild_fts`, `rebuild_tests`, `embed`) may
    acquire this lock. POSIX `fcntl.lockf` is per-process, so a second
    FD acquire succeeds but the FIRST `unlock` drops it for the whole
    process — which would silently release the outer caller's lock.
    Windows `msvcrt.locking` would instead deadlock the second acquire.
    Guarding with a per-process refcount avoids both.
    """
    lock_path = config.lock_path
    lock_key = str(lock_path)
    local_lock = _thread_lock(lock_key)
    local_lock.acquire()
    try:
        # Nested: already held by this thread in this process.
        if _HELD_COUNT.get(lock_key, 0) > 0:
            _HELD_COUNT[lock_key] += 1
            try:
                yield
            finally:
                _HELD_COUNT[lock_key] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\x00")
        except OSError:
            pass

        is_win = os.name == "nt"
        deadline = time.monotonic() + timeout_s
        backoff = poll_ms / 1000.0
        acquired = False
        try:
            while True:
                ok = _try_lock_win(fd) if is_win else _try_lock_posix(fd)
                if ok:
                    acquired = True
                    _HELD_COUNT[lock_key] = 1
                    break
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(lock_path, timeout_s)
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 0.5)

            yield
        finally:
            if acquired:
                _HELD_COUNT.pop(lock_key, None)
                if is_win:
                    _unlock_win(fd)
                else:
                    _unlock_posix(fd)
            try:
                os.close(fd)
            except OSError:
                pass
    finally:
        local_lock.release()
