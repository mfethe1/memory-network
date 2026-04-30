"""Small shared helpers for the live graph server."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any


GRAPH_TOKEN_ENV_VAR = "CODE_INDEX_GRAPH_TOKEN"
GRAPH_SESSION_COOKIE = "code_index_graph_session"
GRAPH_SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _preflight_secret() -> str:
    env_secret = os.environ.get("CODE_INDEX_GRAPH_PREFLIGHT_SECRET", "").strip()
    if env_secret:
        return env_secret
    token = os.environ.get(GRAPH_TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    return secrets.token_hex(32)


def _session_cookie_value(secret: str, graph_token: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"graph-session:{graph_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _cookie_value(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else None


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")


def _validate_bearer(auth_header: str | None, expected: str) -> bool:
    if not auth_header or not expected:
        return False
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip(), expected.strip())


def _validate_token(value: str | None, expected: str) -> bool:
    if not value or not expected:
        return False
    return hmac.compare_digest(value.strip(), expected.strip())


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _auth_page_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Graph Auth</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f8fafc; color: #111827; }
    main { width: min(420px, calc(100vw - 32px)); border: 1px solid #d1d5db; background: white; padding: 24px; border-radius: 8px; box-shadow: 0 12px 32px rgba(15, 23, 42, 0.12); }
    h1 { font-size: 20px; margin: 0 0 12px; }
    p { color: #4b5563; line-height: 1.5; }
    label { display: block; font-size: 13px; font-weight: 600; margin: 16px 0 6px; }
    input { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid #9ca3af; border-radius: 6px; font: inherit; }
    button { margin-top: 14px; padding: 9px 14px; border: 1px solid #111827; border-radius: 6px; background: #111827; color: white; font: inherit; cursor: pointer; }
    .status { min-height: 20px; margin-top: 12px; color: #b91c1c; font-size: 13px; }
  </style>
</head>
<body>
  <main>
    <h1>Graph server token</h1>
    <p>Enter the local graph token to create a same-origin browser session.</p>
    <form id="auth-form">
      <label for="token">Token</label>
      <input id="token" name="token" type="password" autocomplete="current-password" autofocus>
      <button type="submit">Continue</button>
      <div class="status" id="status"></div>
    </form>
  </main>
  <script>
    try {
      const current = new URL(window.location.href);
      ["token", "graph_token", "access_token"].forEach((name) => current.searchParams.delete(name));
      window.history.replaceState({}, document.title, `${current.pathname}${current.search}${current.hash}`);
    } catch (_err) {}
    document.getElementById("auth-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = document.getElementById("token").value.trim();
      const status = document.getElementById("status");
      if (!token) {
        status.textContent = "Token required";
        return;
      }
      const response = await fetch("/api/auth/browser-session", {
        method: "POST",
        credentials: "same-origin",
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!response.ok) {
        status.textContent = "Invalid token";
        return;
      }
      window.location.replace("/repo-graph.html");
    });
  </script>
</body>
</html>"""
