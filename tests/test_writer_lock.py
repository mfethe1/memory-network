"""Writer lock: re-entrant per process, mutually exclusive across processes,
raises LockTimeoutError when contention exceeds the timeout."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_index import config as cfg_mod
from code_index.locking import LockTimeoutError, writer_lock


def _make_config(tmp_path: Path):
    cfg = cfg_mod.load(tmp_path)
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_lock_is_acquired_and_released(tmp_path: Path):
    cfg = _make_config(tmp_path)
    with writer_lock(cfg, timeout_s=2):
        assert cfg.lock_path.exists()
    # After release, re-acquiring immediately must succeed.
    with writer_lock(cfg, timeout_s=2):
        pass


def test_lock_is_reentrant_within_one_process(tmp_path: Path):
    cfg = _make_config(tmp_path)
    with writer_lock(cfg, timeout_s=2):
        # Nested acquire in the same process is a no-op. Must not deadlock.
        with writer_lock(cfg, timeout_s=1):
            with writer_lock(cfg, timeout_s=1):
                assert cfg.lock_path.exists()


def test_lock_raises_on_timeout_from_other_process(tmp_path: Path):
    """A second OS process must fail with LockTimeoutError when the first
    holds the lock past the timeout window."""
    cfg = _make_config(tmp_path)

    # Child script: take the lock and hold it for 3 seconds.
    holder = tmp_path / "hold.py"
    repo_root = Path(__file__).resolve().parent.parent
    holder.write_text(
        f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(repo_root)!r})
from code_index import config as cfg_mod
from code_index.locking import writer_lock
cfg = cfg_mod.load(Path({str(tmp_path)!r}))
cfg.index_dir.mkdir(parents=True, exist_ok=True)
with writer_lock(cfg, timeout_s=1):
    print("HELD", flush=True)
    time.sleep(3)
""",
        encoding="utf-8",
    )

    # Start the holder and wait for it to grab the lock.
    proc = subprocess.Popen(
        [sys.executable, str(holder)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Read the HELD line to confirm the child has the lock.
        deadline = time.time() + 5
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.strip() == "HELD":
                break
        assert line.strip() == "HELD", f"holder didn't grab the lock (line={line!r})"

        # Now we try to acquire from this process with a short timeout.
        t0 = time.time()
        with pytest.raises(LockTimeoutError):
            with writer_lock(cfg, timeout_s=0.5):
                pass
        elapsed = time.time() - t0
        # Timeout must have fired promptly — not wait the full 3s.
        assert 0.4 <= elapsed <= 2.5, f"timeout slack: {elapsed}"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_lock_timeout_error_carries_useful_fields(tmp_path: Path):
    cfg = _make_config(tmp_path)
    try:
        raise LockTimeoutError(cfg.lock_path, 2.5)
    except LockTimeoutError as exc:
        assert exc.lock_path == cfg.lock_path
        assert exc.timeout_s == 2.5
        assert "lock" in str(exc).lower()


def test_lock_released_even_on_exception(tmp_path: Path):
    cfg = _make_config(tmp_path)
    with pytest.raises(ValueError):
        with writer_lock(cfg, timeout_s=1):
            raise ValueError("boom")
    # Must be acquirable again.
    with writer_lock(cfg, timeout_s=1):
        pass
