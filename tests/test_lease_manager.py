from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import lease_manager


def _config(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    return cfg_mod.load(tmp_path)


def test_create_lease_returns_token_but_active_claims_do_not(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")

        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
            metadata={
                "source": "test",
                "lease_token": "metadata-token-secret",
                "Authorization": "Bearer metadata-authorization-secret",
                "nested": {"refresh_token": "metadata-refresh-secret"},
                "api_key": "metadata-api-key-secret",
            },
        )
        active = agent_activity.active_file_claims(conn, file_path="pkg/a.py")
        events = lease_manager.claim_events(conn, claim_id=lease["claim"]["claim_id"])

        assert lease["lease_token"]
        assert lease["claim"]["mode"] == "edit"
        serialized = json.dumps({"active": active, "events": events}, sort_keys=True)
        assert "lease_token" not in serialized
        assert "lease_token_hash" not in serialized
        assert "metadata-token-secret" not in serialized
        assert "metadata-authorization-secret" not in serialized
        assert "metadata-refresh-secret" not in serialized
        assert "metadata-api-key-secret" not in serialized
        assert events[0]["metadata"] == {"source": "test", "nested": {}}
    finally:
        db_mod.close(conn)


def test_lease_lifecycle_events_are_recorded(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
        )

        lease_manager.renew_lease(
            conn,
            claim_id=lease["claim"]["claim_id"],
            lease_token=lease["lease_token"],
            fence_token=lease["claim"]["fence_token"],
            ttl_seconds=600,
        )
        lease_manager.release_lease(
            conn,
            claim_id=lease["claim"]["claim_id"],
            lease_token=lease["lease_token"],
            status="released",
        )
        events = lease_manager.claim_events(conn, claim_id=lease["claim"]["claim_id"])

        assert [event["event_type"] for event in events] == [
            "created",
            "renewed",
            "released",
        ]
    finally:
        db_mod.close(conn)


def test_renew_lease_requires_matching_token_and_fence(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
        )

        with pytest.raises(ValueError, match="lease token does not match claim"):
            lease_manager.renew_lease(
                conn,
                claim_id=lease["claim"]["claim_id"],
                lease_token="wrong-token",
                fence_token=lease["claim"]["fence_token"],
                ttl_seconds=600,
            )
        with pytest.raises(ValueError, match="lease fence token is stale"):
            lease_manager.renew_lease(
                conn,
                claim_id=lease["claim"]["claim_id"],
                lease_token=lease["lease_token"],
                fence_token=int(lease["claim"]["fence_token"]) + 1,
                ttl_seconds=600,
            )
        with pytest.raises(ValueError, match="lease fence token is malformed"):
            lease_manager.renew_lease(
                conn,
                claim_id=lease["claim"]["claim_id"],
                lease_token=lease["lease_token"],
                fence_token="abc-fence-secret",
                ttl_seconds=600,
            )
        with pytest.raises(ValueError, match="lease token does not match claim"):
            lease_manager.release_lease(
                conn,
                claim_id=lease["claim"]["claim_id"],
                lease_token="release-secret-token",
            )

        events = lease_manager.claim_events(conn, claim_id=lease["claim"]["claim_id"])
        assert [event["event_type"] for event in events] == [
            "created",
            "denied",
            "denied",
            "denied",
            "denied",
        ]
        denied_reasons = [
            event["metadata"].get("reason")
            for event in events
            if event["event_type"] == "denied"
        ]
        assert denied_reasons == [
            "bad_token",
            "stale_fence",
            "bad_fence",
            "bad_token",
        ]
        serialized = json.dumps(events, sort_keys=True)
        assert "wrong-token" not in serialized
        assert "abc-fence-secret" not in serialized
        assert "release-secret-token" not in serialized
    finally:
        db_mod.close(conn)


def test_matching_agent_event_preserves_first_class_lease(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
        )

        agent_activity.record_event(
            conn,
            run_id=run["run_id"],
            event_type="edit",
            file_path="pkg/a.py",
            message="normal edit event should not destroy the lease",
        )
        events_after_edit = lease_manager.claim_events(
            conn,
            claim_id=lease["claim"]["claim_id"],
        )
        renewed = lease_manager.renew_lease(
            conn,
            claim_id=lease["claim"]["claim_id"],
            lease_token=lease["lease_token"],
            fence_token=lease["claim"]["fence_token"],
            ttl_seconds=600,
        )
        events_after_renew = lease_manager.claim_events(
            conn,
            claim_id=lease["claim"]["claim_id"],
        )

        assert [event["event_type"] for event in events_after_edit] == ["created"]
        assert renewed["claim_id"] == lease["claim"]["claim_id"]
        assert renewed["fence_token"] == lease["claim"]["fence_token"]
        assert [event["event_type"] for event in events_after_renew] == [
            "created",
            "renewed",
        ]
    finally:
        db_mod.close(conn)


def test_expire_stale_leases_marks_expired_once(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="short lease",
            ttl_seconds=0.001,
        )
        time.sleep(0.02)

        expired = lease_manager.expire_stale_leases(conn)
        lease_manager.expire_stale_leases(conn)
        row = conn.execute(
            "SELECT status FROM agent_file_claims WHERE claim_id = ?",
            (lease["claim"]["claim_id"],),
        ).fetchone()
        events = lease_manager.claim_events(conn, claim_id=lease["claim"]["claim_id"])

        assert [claim["claim_id"] for claim in expired] == [lease["claim"]["claim_id"]]
        assert row["status"] == "expired"
        assert [event["event_type"] for event in events] == ["created", "expired"]
    finally:
        db_mod.close(conn)


def test_release_lease_requires_active_lease(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        run = agent_activity.start_run(conn, agent_name="Codex", prompt="edit")
        lease = lease_manager.create_lease(
            conn,
            run_id=run["run_id"],
            file_path="pkg/a.py",
            mode="edit",
            reason="editing selected file",
        )

        lease_manager.release_lease(
            conn,
            claim_id=lease["claim"]["claim_id"],
            lease_token=lease["lease_token"],
        )

        with pytest.raises(ValueError, match="lease is not active"):
            lease_manager.release_lease(
                conn,
                claim_id=lease["claim"]["claim_id"],
                lease_token=lease["lease_token"],
            )
    finally:
        db_mod.close(conn)


def test_apply_schema_upgrades_v10_claim_table_for_leases(tmp_path: Path):
    config = _config(tmp_path)
    conn = db_mod.connect(config.db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '10');
            CREATE TABLE agent_file_claims (
                claim_pk      INTEGER PRIMARY KEY,
                claim_id      TEXT NOT NULL UNIQUE,
                run_pk        INTEGER NOT NULL REFERENCES agent_runs(run_pk) ON DELETE CASCADE,
                file_path     TEXT NOT NULL,
                mode          TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'active',
                reason        TEXT,
                fence_token   INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                heartbeat_at  TEXT,
                expires_at    TEXT,
                released_at   TEXT,
                metadata_json TEXT,
                UNIQUE(run_pk, file_path, mode)
            );
            """
        )

        db_mod.apply_schema(conn)

        claim_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_file_claims)")
        }
        event_table = conn.execute(
            """
            SELECT 1
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'agent_file_claim_events'
            """
        ).fetchone()
        assert {
            "lease_token_hash",
            "lease_kind",
            "owner_agent",
            "heartbeat_interval_ms",
            "conflict_policy",
            "last_conflict_json",
        } <= claim_columns
        assert event_table is not None
        assert db_mod.get_schema_version(conn) == db_mod.SCHEMA_VERSION
    finally:
        db_mod.close(conn)
