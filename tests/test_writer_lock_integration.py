"""Integration: mutating command paths surface `LockTimeoutError` as a
structured JSON error (exit 3 for CLI, `error` field for MCP tools) instead
of a raw exception."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands import init_cmd, mcp_serve_cmd, update_cmd
from code_index.locking import writer_lock


def _prep(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    db_mod.close(conn)
    return config


def test_init_returns_json_error_on_lock_timeout(tmp_path: Path, monkeypatch):
    """When another holder owns the lock, `init` must return exit 3 with a
    structured JSON error instead of raising."""
    config = _prep(tmp_path)

    # Simulate a second-process holder by directly grabbing the OS-level lock
    # on a FRESH FD that bypasses the in-process refcount. This mirrors what
    # a concurrent `code_index` subprocess would do.
    import os

    import code_index.locking as locking

    fd = os.open(str(config.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\x00")
        ok = (
            locking._try_lock_win(fd)
            if os.name == "nt"
            else locking._try_lock_posix(fd)
        )
        assert ok, "could not prime the contention scenario — lock already held"

        args = argparse.Namespace(
            root=str(tmp_path),
            json=True,
            force=False,
        )
        # Patch reindex's default lock timeout to a fraction of a second so
        # the test doesn't hang for 30s on the default.
        monkeypatch.setattr(
            "code_index.commands.init_cmd.reindex",
            lambda *a, **kw: __import__(
                "code_index.pipeline", fromlist=["reindex"]
            ).reindex(*a, **{**kw, "lock_timeout_s": 0.2}),
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = init_cmd.run(args)
        assert rc == 3
        payload = json.loads(buf.getvalue())
        assert payload["error"] == "another writer holds the lock"
        assert "lock_path" in payload
    finally:
        try:
            if os.name == "nt":
                locking._unlock_win(fd)
            else:
                locking._unlock_posix(fd)
        finally:
            os.close(fd)


def test_mcp_update_tool_returns_error_on_lock_timeout(tmp_path: Path, monkeypatch):
    """The MCP `update` tool must surface a structured error rather than
    raising when the writer lock is held."""
    config = _prep(tmp_path)

    # Hold the lock for the full duration of the tool call.
    with writer_lock(config):
        # In the same process we'd normally reuse the lock via refcount.
        # Force a real timeout by temporarily clearing the refcount so the
        # tool's acquire path actually runs against the OS-held lock.
        import code_index.locking as locking

        saved = locking._HELD_COUNT.pop(str(config.lock_path), 0)
        try:
            monkeypatch.setattr(
                "code_index.commands.mcp_serve_cmd.writer_lock",
                lambda cfg, **kw: writer_lock(cfg, timeout_s=0.2),
            )
            result = mcp_serve_cmd._tool_update(config, files=None, all=True)
            assert "error" in result
            assert result["error"] == "another writer holds the lock"
            assert "lock_path" in result
        finally:
            locking._HELD_COUNT[str(config.lock_path)] = saved


def test_mcp_rebuild_fts_tool_is_lock_guarded(tmp_path: Path, monkeypatch):
    """`_tool_rebuild_fts` must wrap its write section with `writer_lock`
    — previously it ran unlocked."""
    config = _prep(tmp_path)

    calls: list[str] = []
    from code_index.commands import mcp_serve_cmd as mod

    original = mod.writer_lock

    def _tracker(cfg, **kw):
        calls.append(str(cfg.lock_path))
        return original(cfg, **kw)

    monkeypatch.setattr(mod, "writer_lock", _tracker)
    result = mod._tool_rebuild_fts(config)
    assert calls, "_tool_rebuild_fts should acquire the writer lock"
    # Result shape is the rebuild_fts output — either a stats dict or an error.
    assert isinstance(result, dict)
