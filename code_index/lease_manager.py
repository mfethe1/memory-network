"""Durable file leases for graph-supervised agent runs."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

SENSITIVE_METADATA_KEY_NAMES = {
    "access_key",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "bearer_token",
    "cookie",
    "key",
    "lease_token",
    "lease_token_hash",
    "passwd",
    "password",
    "session_cookie",
}
DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_after(ttl_seconds: float | None) -> str | None:
    if ttl_seconds is None:
        return None
    seconds = float(ttl_seconds)
    if seconds <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _is_sensitive_metadata_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    compact = normalized.replace("_", "").replace(".", "")
    parts = [part for part in normalized.replace(".", "_").split("_") if part]
    return (
        normalized in SENSITIVE_METADATA_KEY_NAMES
        or compact
        in {
            "accesskey",
            "apikey",
            "authheader",
            "bearertoken",
            "leasehash",
            "leasetoken",
            "leasetokenhash",
            "privatekey",
            "sessioncookie",
        }
        or "token" in compact
        or "secret" in compact
        or "authorization" in compact
        or "token" in parts
        or "secret" in parts
        or "authorization" in parts
        or ("key" in parts and any(part in {"api", "access", "private"} for part in parts))
    )


def redact_public_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): redact_public_metadata(item)
            for key, item in value.items()
            if not _is_sensitive_metadata_key(key)
        }
    if isinstance(value, list):
        return [redact_public_metadata(item) for item in value]
    return value


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _claim_row(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*,
               r.run_id,
               r.agent_name,
               r.status AS run_status
          FROM agent_file_claims c
          JOIN agent_runs r ON r.run_pk = c.run_pk
         WHERE c.claim_id = ?
         LIMIT 1
        """,
        (claim_id,),
    ).fetchone()


def _public_claim(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "claim_pk": int(row["claim_pk"]),
        "claim_id": row["claim_id"],
        "run_id": row["run_id"],
        "agent_name": row["agent_name"] or "Agent",
        "run_status": row["run_status"] or "working",
        "file_path": row["file_path"],
        "mode": row["mode"],
        "status": row["status"] or "active",
        "reason": row["reason"] or "",
        "fence_token": int(row["fence_token"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "metadata": redact_public_metadata(_json_loads(row["metadata_json"], {})),
    }


def _token_matches(row: sqlite3.Row, lease_token: str) -> bool:
    expected = str(row["lease_token_hash"] or "")
    actual = _hash_token(lease_token or "")
    return bool(expected) and secrets.compare_digest(expected, actual)


def _raise_unless_active(row: sqlite3.Row) -> None:
    if str(row["status"] or "") != "active":
        raise ValueError(f"lease is not active: {row['claim_id']}")


def _validate_fence_token(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_denied_event(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    reason: str,
    message: str,
) -> None:
    with _atomic(conn):
        record_claim_event(
            conn,
            claim_pk=int(row["claim_pk"]),
            event_type="denied",
            file_path=str(row["file_path"]),
            mode=str(row["mode"]),
            fence_token=int(row["fence_token"] or 0),
            message=message,
            metadata={"reason": reason},
        )


def record_claim_event(
    conn: sqlite3.Connection,
    *,
    claim_pk: int,
    event_type: str,
    file_path: str,
    mode: str,
    fence_token: int | None,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_file_claim_events(
            claim_pk, event_type, timestamp, file_path, mode, fence_token, message,
            metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_pk,
            event_type,
            _now_iso(),
            file_path,
            mode,
            fence_token,
            message,
            _json_dumps(redact_public_metadata(metadata or {})),
        ),
    )


def create_lease(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    file_path: str,
    mode: str,
    reason: str,
    ttl_seconds: float | None = 1800,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from code_index import agent_activity

    with _atomic(conn):
        token = _new_token()
        claim = agent_activity.claim_file(
            conn,
            run_id=run_id,
            file_path=file_path,
            mode=mode,
            reason=reason,
            ttl_seconds=ttl_seconds,
            metadata=metadata,
            _record_lifecycle_event=False,
        )
        conn.execute(
            """
            UPDATE agent_file_claims
               SET lease_token_hash = ?,
                   lease_kind = ?,
                   owner_agent = ?,
                   heartbeat_interval_ms = ?
             WHERE claim_id = ?
            """,
            (
                _hash_token(token),
                "lease",
                claim.get("agent_name"),
                DEFAULT_HEARTBEAT_INTERVAL_MS,
                claim["claim_id"],
            ),
        )
        row = _claim_row(conn, str(claim["claim_id"]))
        if row is None:
            raise ValueError(f"unknown claim_id: {claim['claim_id']}")
        record_claim_event(
            conn,
            claim_pk=int(row["claim_pk"]),
            event_type="created",
            file_path=str(row["file_path"]),
            mode=str(row["mode"]),
            fence_token=int(row["fence_token"] or 0),
            message=f"Lease created for {row['file_path']}.",
            metadata=metadata,
        )
    return {"claim": _public_claim(row), "lease_token": token}


def renew_lease(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    lease_token: str,
    fence_token: Any,
    ttl_seconds: float | None = 1800,
) -> dict[str, Any]:
    claim: dict[str, Any] | None = None
    error: str | None = None
    with _atomic(conn):
        expired = expire_stale_leases(conn, claim_id=claim_id)
        row = _claim_row(conn, claim_id)
        if row is None:
            error = f"unknown claim_id: {claim_id}"
        elif expired:
            error = f"lease is expired: {claim_id}"
        elif str(row["status"] or "") != "active":
            error = f"lease is not active: {claim_id}"
        elif not _token_matches(row, lease_token):
            _record_denied_event(
                conn,
                row=row,
                reason="bad_token",
                message="Lease renewal denied: lease token does not match claim.",
            )
            error = "lease token does not match claim"
        else:
            parsed_fence = _validate_fence_token(fence_token)
            if parsed_fence is None:
                _record_denied_event(
                    conn,
                    row=row,
                    reason="bad_fence",
                    message="Lease renewal denied: lease fence token is malformed.",
                )
                error = "lease fence token is malformed"
            elif int(row["fence_token"] or 0) != parsed_fence:
                _record_denied_event(
                    conn,
                    row=row,
                    reason="stale_fence",
                    message="Lease renewal denied: lease fence token is stale.",
                )
                error = "lease fence token is stale"
        if error is not None:
            pass
        else:
            assert row is not None
            expected_hash = str(row["lease_token_hash"] or "")
            current_fence = int(row["fence_token"] or 0)
            now = _now_iso()
            result = conn.execute(
                """
                UPDATE agent_file_claims
                   SET heartbeat_at = ?,
                       updated_at = ?,
                       expires_at = ?
                 WHERE claim_id = ?
                   AND status = 'active'
                   AND lease_kind = 'lease'
                   AND lease_token_hash = ?
                   AND fence_token = ?
                """,
                (
                    now,
                    now,
                    _iso_after(ttl_seconds),
                    claim_id,
                    expected_hash,
                    current_fence,
                ),
            )
            if result.rowcount != 1:
                error = "lease changed while renewing"
            else:
                row = _claim_row(conn, claim_id)
                if row is None:
                    error = f"unknown claim_id: {claim_id}"
                else:
                    record_claim_event(
                        conn,
                        claim_pk=int(row["claim_pk"]),
                        event_type="renewed",
                        file_path=str(row["file_path"]),
                        mode=str(row["mode"]),
                        fence_token=int(row["fence_token"] or 0),
                        message=f"Lease renewed for {row['file_path']}.",
                    )
                    claim = _public_claim(row)
    if error is not None:
        raise ValueError(error)
    assert claim is not None
    return claim


def release_lease(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    lease_token: str,
    status: str = "released",
) -> dict[str, Any]:
    claim: dict[str, Any] | None = None
    error: str | None = None
    with _atomic(conn):
        expired = expire_stale_leases(conn, claim_id=claim_id)
        row = _claim_row(conn, claim_id)
        if row is None:
            error = f"unknown claim_id: {claim_id}"
        elif expired:
            error = f"lease is expired: {claim_id}"
        elif str(row["status"] or "") != "active":
            error = f"lease is not active: {claim_id}"
        elif not _token_matches(row, lease_token):
            _record_denied_event(
                conn,
                row=row,
                reason="bad_token",
                message="Lease release denied: lease token does not match claim.",
            )
            error = "lease token does not match claim"
        if error is None:
            assert row is not None
            expected_hash = str(row["lease_token_hash"] or "")
            next_status = "expired" if status == "expired" else "released"
            now = _now_iso()
            result = conn.execute(
                """
                UPDATE agent_file_claims
                   SET status = ?,
                       lease_token_hash = NULL,
                       updated_at = ?,
                       released_at = ?
                 WHERE claim_id = ?
                   AND status = 'active'
                   AND lease_kind = 'lease'
                   AND lease_token_hash = ?
                """,
                (next_status, now, now, claim_id, expected_hash),
            )
            if result.rowcount != 1:
                error = "lease changed while releasing"
            else:
                row = _claim_row(conn, claim_id)
                if row is None:
                    error = f"unknown claim_id: {claim_id}"
                else:
                    record_claim_event(
                        conn,
                        claim_pk=int(row["claim_pk"]),
                        event_type=next_status,
                        file_path=str(row["file_path"]),
                        mode=str(row["mode"]),
                        fence_token=int(row["fence_token"] or 0),
                        message=f"Lease {next_status} for {row['file_path']}.",
                    )
                    claim = _public_claim(row)
    if error is not None:
        raise ValueError(error)
    assert claim is not None
    return claim


def expire_stale_leases(
    conn: sqlite3.Connection,
    *,
    claim_id: str | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    cutoff = now or _now_iso()
    clauses = [
        "c.lease_kind = 'lease'",
        "c.status = 'active'",
        "c.expires_at IS NOT NULL",
        "c.expires_at < ?",
    ]
    params: list[Any] = [cutoff]
    if claim_id:
        clauses.append("c.claim_id = ?")
        params.append(claim_id)
    with _atomic(conn):
        rows = conn.execute(
            f"""
            SELECT c.*,
                   r.run_id,
                   r.agent_name,
                   r.status AS run_status
              FROM agent_file_claims c
              JOIN agent_runs r ON r.run_pk = c.run_pk
             WHERE {" AND ".join(clauses)}
             ORDER BY c.expires_at ASC, c.claim_pk ASC
            """,
            params,
        ).fetchall()
        expired: list[dict[str, Any]] = []
        for row in rows:
            result = conn.execute(
                """
                UPDATE agent_file_claims
                   SET status = 'expired',
                       lease_token_hash = NULL,
                       updated_at = ?,
                       released_at = ?
                 WHERE claim_pk = ?
                   AND status = 'active'
                """,
                (cutoff, cutoff, row["claim_pk"]),
            )
            if result.rowcount != 1:
                continue
            record_claim_event(
                conn,
                claim_pk=int(row["claim_pk"]),
                event_type="expired",
                file_path=str(row["file_path"]),
                mode=str(row["mode"]),
                fence_token=int(row["fence_token"] or 0),
                message=f"Lease expired for {row['file_path']}.",
            )
            refreshed = _claim_row(conn, str(row["claim_id"]))
            if refreshed is not None:
                expired.append(_public_claim(refreshed))
    return expired


def claim_events(conn: sqlite3.Connection, *, claim_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.*
          FROM agent_file_claim_events e
          JOIN agent_file_claims c ON c.claim_pk = e.claim_pk
         WHERE c.claim_id = ?
         ORDER BY e.claim_event_pk
        """,
        (claim_id,),
    ).fetchall()
    return [
        {
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "file_path": row["file_path"],
            "mode": row["mode"],
            "fence_token": row["fence_token"],
            "message": row["message"],
            "metadata": redact_public_metadata(_json_loads(row["metadata_json"], {})),
        }
        for row in rows
    ]
