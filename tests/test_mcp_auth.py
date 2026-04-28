"""Tests for the MCP HTTP-transport bearer-token auth helpers.

These exercise the pure helpers in `code_index.commands.mcp_serve_cmd`
directly — no real HTTP server is started. The tests that need the
middleware class `pytest.importorskip` on `mcp` since the middleware
builder pulls in starlette (vendored with mcp[cli]).
"""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from code_index import config as cfg_mod
from code_index.commands import mcp_serve_cmd as m


# ---------- token generation + file writing ----------


def test_generate_token_is_hex_and_unique() -> None:
    a = m._generate_token()
    b = m._generate_token()
    assert len(a) == 64  # 32 bytes hex-encoded
    assert all(c in "0123456789abcdef" for c in a)
    assert a != b


def test_write_token_file_creates_parent_and_sets_0600(tmp_path: Path) -> None:
    token = "deadbeef" * 8
    target = tmp_path / ".code_index" / "mcp-token"
    m._write_token_file(target, token)
    assert target.read_text(encoding="utf-8") == token
    if os.name == "posix":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_read_token_file_rejects_empty(tmp_path: Path) -> None:
    p = tmp_path / "token"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        m._read_token_file(p)


# ---------- bind validation ----------


@pytest.mark.parametrize(
    "bind,expected",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("192.168.1.1", False),
        ("example.com", False),
        ("not-an-address", False),
    ],
)
def test_is_loopback(bind: str, expected: bool) -> None:
    assert m._is_loopback(bind) is expected


# ---------- token source resolution ----------


def _make_config(tmp_path: Path) -> cfg_mod.Config:
    (tmp_path / ".code_index").mkdir()
    return cfg_mod.Config(root=tmp_path)


def test_resolve_bearer_prefers_flag(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    buf = io.StringIO()
    token, source = m._resolve_bearer_token(
        flag_token="from-flag",
        flag_token_file=None,
        env_token="from-env",
        config=cfg,
        generate_if_missing=True,
        stderr=buf,
    )
    assert token == "from-flag"
    assert source == "flag"
    assert buf.getvalue() == ""


def test_resolve_bearer_falls_back_to_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    token_file = tmp_path / "bearer.txt"
    token_file.write_text("from-file\n", encoding="utf-8")
    token, source = m._resolve_bearer_token(
        flag_token=None,
        flag_token_file=str(token_file),
        env_token="from-env",
        config=cfg,
        generate_if_missing=True,
        stderr=io.StringIO(),
    )
    assert token == "from-file"
    assert source == "file"


def test_resolve_bearer_falls_back_to_env(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    token, source = m._resolve_bearer_token(
        flag_token=None,
        flag_token_file=None,
        env_token="from-env",
        config=cfg,
        generate_if_missing=True,
        stderr=io.StringIO(),
    )
    assert token == "from-env"
    assert source == "env"


def test_resolve_bearer_generates_and_persists(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    buf = io.StringIO()
    token, source = m._resolve_bearer_token(
        flag_token=None,
        flag_token_file=None,
        env_token=None,
        config=cfg,
        generate_if_missing=True,
        stderr=buf,
    )
    assert source == "generated"
    assert token is not None and len(token) == 64
    token_path = cfg.index_dir / m.TOKEN_FILENAME
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8") == token
    # Printed to stderr once so the user can copy it.
    assert token in buf.getvalue()


def test_resolve_bearer_returns_none_when_not_generating(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    token, source = m._resolve_bearer_token(
        flag_token=None,
        flag_token_file=None,
        env_token=None,
        config=cfg,
        generate_if_missing=False,
        stderr=io.StringIO(),
    )
    assert token is None
    assert source == "none"


# ---------- header validation ----------


def test_validate_bearer_accepts_correct_header() -> None:
    assert m._validate_bearer("Bearer abc123", "abc123") is True
    assert (
        m._validate_bearer("bearer abc123", "abc123") is True
    )  # case-insensitive scheme


def test_validate_bearer_rejects_wrong_token() -> None:
    assert m._validate_bearer("Bearer wrong", "right") is False


def test_validate_bearer_rejects_missing_or_malformed() -> None:
    assert m._validate_bearer(None, "x") is False
    assert m._validate_bearer("", "x") is False
    assert m._validate_bearer("Basic abc123", "abc123") is False
    assert m._validate_bearer("abc123", "abc123") is False  # no scheme
    assert m._validate_bearer("Bearer", "abc123") is False  # no token


def test_validate_bearer_rejects_when_expected_empty() -> None:
    # Fail closed: empty server-side secret must never authenticate anyone.
    assert m._validate_bearer("Bearer anything", "") is False


# ---------- middleware integration (sanity only; no real server) ----------


def test_middleware_builder_returns_asgi_class() -> None:
    """Builds the middleware class without starting a server. Skipped when
    starlette isn't installed (which is bundled with mcp[cli]).
    """
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    cls = m._build_bearer_middleware("some-token")
    # Starlette BaseHTTPMiddleware subclass with dispatch defined.
    assert callable(getattr(cls, "dispatch", None))


# ---------- CLI integration: --bind 0.0.0.0 without --allow-remote fails ----


def test_http_rejects_non_loopback_bind_without_allow_remote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    # Minimal repo with an existing index db so _resolve_config succeeds.
    idx_dir = tmp_path / ".code_index"
    idx_dir.mkdir()
    (idx_dir / "index.db").write_bytes(b"")  # existence is all that's checked
    # Avoid touching the env's real token.
    monkeypatch.delenv(m.TOKEN_ENV_VAR, raising=False)

    import argparse

    args = argparse.Namespace(
        root=str(tmp_path),
        json=True,
        describe=False,
        transport="http",
        bind="0.0.0.0",
        port=None,
        allow_remote=False,
        bearer_token=None,
        bearer_token_file=None,
    )
    rc = m.run(args)
    out = capsys.readouterr().out
    assert rc == 2
    assert '"error": "remote bind refused"' in out
    assert '"bind": "0.0.0.0"' in out
