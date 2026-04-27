"""HTTP bearer-token helpers for `code_index mcp-serve`."""

from __future__ import annotations

import ipaddress
import secrets
import stat
from pathlib import Path

from code_index import config as cfg_mod


TOKEN_FILENAME = "mcp-token"
TOKEN_ENV_VAR = "CODE_INDEX_MCP_TOKEN"




def _generate_token() -> str:
    """Return a 32-byte hex-encoded random token (64 hex chars)."""
    return secrets.token_hex(32)


def _write_token_file(path: Path, token: str) -> None:
    """Write token to path with mode 0600 on POSIX. Creates parent dirs.

    Note: On Windows the chmod to 0600 is a best-effort no-op — NTFS ACLs
    provide the real protection and we don't try to set them here. Callers
    should rely on the file living under `.code_index/` (which inherits the
    repo's existing ACLs).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except (OSError, NotImplementedError):
        # Windows / odd filesystems: best-effort only.
        pass


def _read_token_file(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"bearer token file is empty: {path}")
    return token


def _is_loopback(bind: str) -> bool:
    """True iff bind is a literal loopback address ('127.0.0.1', '::1', 'localhost')."""
    if bind in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _resolve_bearer_token(
    *,
    flag_token: str | None,
    flag_token_file: str | None,
    env_token: str | None,
    config: cfg_mod.Config,
    generate_if_missing: bool,
    stderr,
) -> tuple[str | None, str]:
    """Return (token, source). Source is one of: 'flag', 'file', 'env', 'generated'.

    If `generate_if_missing` is True and no other source is set, generate a
    new token, persist it to `.code_index/mcp-token` with 0600 perms, and
    print it ONCE to `stderr` so the user can copy it.

    If `generate_if_missing` is False, returns (None, 'none') when nothing is
    set — caller decides what to do.
    """
    if flag_token:
        return flag_token.strip(), "flag"
    if flag_token_file:
        return _read_token_file(Path(flag_token_file)), "file"
    if env_token:
        return env_token.strip(), "env"
    if not generate_if_missing:
        return None, "none"
    token = _generate_token()
    token_path = config.index_dir / TOKEN_FILENAME
    _write_token_file(token_path, token)
    print(
        f"code_index mcp-serve: generated bearer token (copy this):\n"
        f"  token: {token}\n"
        f"  file:  {token_path} (mode 0600 on POSIX)\n"
        f"  env:   export {TOKEN_ENV_VAR}={token}\n",
        file=stderr,
    )
    return token, "generated"


def _validate_bearer(auth_header: str | None, expected: str) -> bool:
    """Return True iff `auth_header` is a well-formed `Bearer <expected>`.

    Uses `secrets.compare_digest` to avoid timing leaks.
    """
    if not auth_header or not expected:
        return False
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1].strip(), expected)


_UNAVAILABLE = {
    "error": "MCP SDK not installed",
    "hint": "install with: pip install 'code-index[mcp]'  (or: pip install mcp)",
}
