"""SQLite-backed fumemory-compatible pointer store for passive M1."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from code_index.openclaw_context.completed_work import CompletedWorkEntry
from code_index.openclaw_context.completed_work import normalize_completed_work_file_path
from code_index.openclaw_context.models import CMAInvocationRecord
from code_index.openclaw_context.models import ContextHealthEvent
from code_index.openclaw_context.models import ContextManifest
from code_index.openclaw_context.models import ContextPointer
from code_index.openclaw_context.models import ContextSource
from code_index.openclaw_context.models import HandoffPacket
from code_index.openclaw_context.models import canonical_json
from code_index.openclaw_context.models import json_tuple
from code_index.openclaw_context.models import mapping_tuple
from code_index.openclaw_context.models import string_tuple
from code_index.openclaw_context.models import utc_now_iso


class SQLiteContextStore:
    """Local SQLite store shaped like the planned fumemory context tables."""

    def __init__(self, path: str | Path | sqlite3.Connection = ":memory:") -> None:
        self.path: str | None = None
        if isinstance(path, sqlite3.Connection):
            self._conn = path
            self._owns_conn = False
        else:
            self.path = str(path)
            if not _is_in_memory_path(self.path) and not self.path.startswith("file:"):
                Path(self.path).expanduser().resolve().parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
            self._conn = sqlite3.connect(self.path)
            self._owns_conn = True
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._ensure_schema()

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    def ping(self) -> None:
        self._conn.execute("SELECT 1").fetchone()

    def sqlite_pragmas(self) -> dict[str, Any]:
        """Return operational SQLite pragmas used for local durability checks."""

        return {
            "journal_mode": str(
                self._conn.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower(),
            "busy_timeout": int(
                self._conn.execute("PRAGMA busy_timeout").fetchone()[0] or 0
            ),
            "foreign_keys": int(
                self._conn.execute("PRAGMA foreign_keys").fetchone()[0] or 0
            ),
            "synchronous": int(
                self._conn.execute("PRAGMA synchronous").fetchone()[0] or 0
            ),
        }

    def upsert_context_source(
        self,
        *,
        source_uri: str,
        source_kind: str,
        source_hash: str,
        sensitivity: str = "repo",
        host_id: str | None = None,
        repo_id: str | None = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContextSource:
        source_uri = _required_text(source_uri, "source_uri")
        source_kind = _required_text(source_kind, "source_kind")
        source_hash = _required_text(source_hash, "source_hash")
        source_id = _id("src", source_uri)
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO context_sources(
                source_id, source_uri, source_kind, source_hash, sensitivity,
                host_id, repo_id, provider, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_uri) DO UPDATE SET
                source_kind = excluded.source_kind,
                source_hash = excluded.source_hash,
                sensitivity = excluded.sensitivity,
                host_id = excluded.host_id,
                repo_id = excluded.repo_id,
                provider = excluded.provider,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                source_uri,
                source_kind,
                source_hash,
                sensitivity,
                host_id,
                repo_id,
                provider,
                canonical_json(metadata or {}),
                now,
                now,
            ),
        )
        self._conn.commit()
        source = self.get_context_source_by_uri(source_uri)
        assert source is not None
        return source

    def get_context_source_by_uri(self, source_uri: str) -> ContextSource | None:
        row = self._conn.execute(
            "SELECT * FROM context_sources WHERE source_uri = ?",
            (source_uri,),
        ).fetchone()
        return _source_from_row(row) if row is not None else None

    def upsert_context_pointer(
        self,
        *,
        source_uri: str,
        source_kind: str,
        content_hash: str,
        locator: dict[str, Any],
        summary: str = "",
        tokens_estimate: int = 0,
        sensitivity: str = "repo",
        pointer_kind: str = "context",
        host_id: str | None = None,
        repo_id: str | None = None,
        provider: str | None = None,
        target_symbols: list[str] | tuple[str, ...] = (),
        tags: list[str] | tuple[str, ...] = (),
        required: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ContextPointer:
        locator_json = canonical_json(locator)
        source = self.upsert_context_source(
            source_uri=source_uri,
            source_kind=source_kind,
            source_hash=content_hash,
            sensitivity=sensitivity,
            host_id=host_id,
            repo_id=repo_id,
            provider=provider,
            metadata=metadata,
        )
        pointer_id = _id("ptr", f"{source_uri}\0{content_hash}\0{locator_json}")
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO context_pointers(
                pointer_id, source_id, source_uri, source_kind, pointer_kind,
                content_hash, locator_json, summary, tokens_estimate,
                sensitivity, host_id, repo_id, provider, target_symbols_json,
                tags_json, required, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_uri, content_hash, locator_json) DO UPDATE SET
                source_id = excluded.source_id,
                source_kind = excluded.source_kind,
                pointer_kind = excluded.pointer_kind,
                summary = excluded.summary,
                tokens_estimate = excluded.tokens_estimate,
                sensitivity = excluded.sensitivity,
                host_id = excluded.host_id,
                repo_id = excluded.repo_id,
                provider = excluded.provider,
                target_symbols_json = excluded.target_symbols_json,
                tags_json = excluded.tags_json,
                required = excluded.required,
                updated_at = excluded.updated_at
            """,
            (
                pointer_id,
                source.source_id,
                source_uri,
                source_kind,
                pointer_kind,
                content_hash,
                locator_json,
                summary,
                max(0, int(tokens_estimate or 0)),
                sensitivity,
                host_id,
                repo_id,
                provider,
                canonical_json(list(target_symbols or ())),
                canonical_json(list(tags or ())),
                1 if required else 0,
                now,
                now,
            ),
        )
        self._conn.commit()
        pointer = self.get_context_pointer(pointer_id)
        if pointer is not None:
            return pointer
        row = self._conn.execute(
            """
            SELECT pointer_id FROM context_pointers
             WHERE source_uri = ? AND content_hash = ? AND locator_json = ?
             LIMIT 1
            """,
            (source_uri, content_hash, locator_json),
        ).fetchone()
        assert row is not None
        pointer = self.get_context_pointer(row["pointer_id"])
        assert pointer is not None
        return pointer

    def get_context_pointer(self, pointer_id: str) -> ContextPointer | None:
        row = self._conn.execute(
            "SELECT * FROM context_pointers WHERE pointer_id = ?",
            (pointer_id,),
        ).fetchone()
        return _pointer_from_row(row) if row is not None else None

    def list_context_pointers(
        self,
        *,
        policy: Any | None = None,
        target_symbols: list[str] | tuple[str, ...] = (),
        pointer_kind: str | None = None,
    ) -> list[ContextPointer]:
        rows = self._conn.execute(
            "SELECT * FROM context_pointers ORDER BY pointer_pk ASC"
        ).fetchall()
        pointers = [_pointer_from_row(row) for row in rows]
        if pointer_kind is not None:
            pointers = [
                pointer
                for pointer in pointers
                if pointer.pointer_kind == pointer_kind
                or pointer_kind in pointer.tags
            ]
        targets = {str(symbol) for symbol in target_symbols or () if str(symbol)}
        if targets:
            pointers = [
                pointer
                for pointer in pointers
                if not pointer.target_symbols
                or bool(targets.intersection(pointer.target_symbols))
            ]
        if policy is not None:
            from code_index.openclaw_context.policy import pointer_visible

            pointers = [
                pointer for pointer in pointers if pointer_visible(pointer, policy)
            ]
        return pointers

    def list_avoid_pointers(
        self,
        target_symbols: list[str] | tuple[str, ...],
    ) -> list[ContextPointer]:
        targets = {str(symbol) for symbol in target_symbols or () if str(symbol)}
        if not targets:
            return []
        return [
            pointer
            for pointer in self.list_context_pointers()
            if (
                pointer.pointer_kind == "avoid"
                or "avoid" in pointer.tags
                or pointer.source_kind == "avoid"
            )
            and bool(targets.intersection(pointer.target_symbols))
        ]

    def record_relevance_score(
        self,
        *,
        pointer_id: str,
        task_id: str,
        score: float,
        target_symbol: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        score_id = _id(
            "score",
            canonical_json(
                {
                    "pointer_id": pointer_id,
                    "task_id": task_id,
                    "target_symbol": target_symbol,
                }
            ),
        )
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO context_relevance_scores(
                score_id, pointer_id, task_id, target_symbol, score,
                rationale, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(score_id) DO UPDATE SET
                score = excluded.score,
                rationale = excluded.rationale,
                created_at = excluded.created_at
            """,
            (
                score_id,
                pointer_id,
                task_id,
                target_symbol,
                float(score),
                rationale,
                now,
            ),
        )
        self._conn.commit()
        return {
            "score_id": score_id,
            "pointer_id": pointer_id,
            "task_id": task_id,
            "target_symbol": target_symbol,
            "score": float(score),
            "rationale": rationale,
            "created_at": now,
        }

    def upsert_agent_context_lease(
        self,
        *,
        lease_id: str,
        agent_id: str,
        run_id: str,
        task_id: str,
        provider: str,
        status: str = "active",
        budget_tokens: int = 0,
        soft_limit_tokens: int = 0,
        hard_limit_tokens: int = 0,
        estimated_used_tokens: int = 0,
        context_manifest_hash: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO agent_context_leases(
                lease_id, agent_id, run_id, task_id, provider, budget_tokens,
                soft_limit_tokens, hard_limit_tokens, estimated_used_tokens,
                status, context_manifest_hash, expires_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lease_id) DO UPDATE SET
                estimated_used_tokens = excluded.estimated_used_tokens,
                status = excluded.status,
                context_manifest_hash = excluded.context_manifest_hash,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (
                lease_id,
                agent_id,
                run_id,
                task_id,
                provider,
                int(budget_tokens or 0),
                int(soft_limit_tokens or 0),
                int(hard_limit_tokens or 0),
                int(estimated_used_tokens or 0),
                status,
                context_manifest_hash,
                expires_at,
                now,
                now,
            ),
        )
        self._conn.commit()
        return {
            "lease_id": lease_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "task_id": task_id,
            "provider": provider,
            "budget_tokens": int(budget_tokens or 0),
            "soft_limit_tokens": int(soft_limit_tokens or 0),
            "hard_limit_tokens": int(hard_limit_tokens or 0),
            "estimated_used_tokens": int(estimated_used_tokens or 0),
            "status": status,
            "context_manifest_hash": context_manifest_hash,
            "expires_at": expires_at,
            "updated_at": now,
        }

    def record_health_event(
        self,
        *,
        host_id: str | None,
        run_id: str,
        agent_id: str,
        task_id: str,
        event_kind: str,
        severity: str,
        observed_tokens: int,
        budget_tokens: int,
        details: dict[str, Any] | None = None,
    ) -> ContextHealthEvent:
        details = details or {}
        event_id = _id(
            "che",
            canonical_json(
                {
                    "host_id": host_id,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "event_kind": event_kind,
                    "severity": severity,
                    "observed_tokens": int(observed_tokens or 0),
                    "details": details,
                }
            ),
        )
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO context_health_events(
                event_id, host_id, run_id, agent_id, task_id, event_kind,
                severity, observed_tokens, budget_tokens, details_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                host_id,
                run_id,
                agent_id,
                task_id,
                event_kind,
                severity,
                int(observed_tokens or 0),
                int(budget_tokens or 0),
                canonical_json(details),
                now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM context_health_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert row is not None
        return _health_event_from_row(row)

    def list_context_health_events(
        self,
        *,
        run_id: str | None = None,
    ) -> list[ContextHealthEvent]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM context_health_events ORDER BY event_pk ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM context_health_events
                 WHERE run_id = ?
                 ORDER BY event_pk ASC
                """,
                (run_id,),
            ).fetchall()
        return [_health_event_from_row(row) for row in rows]

    def upsert_handoff_packet(
        self,
        *,
        handoff_id: str,
        host_id: str | None,
        from_run_id: str,
        task_id: str,
        trigger_kind: str,
        status: str,
        packet: dict[str, Any],
        packet_hash: str,
        provider: str | None = None,
        repo_root: str | None = None,
    ) -> HandoffPacket:
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO handoff_packets(
                handoff_id, host_id, from_run_id, task_id, trigger_kind,
                status, provider, repo_root, packet_json, packet_hash,
                created_at, consumed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(packet_hash) DO UPDATE SET
                status = handoff_packets.status
            """,
            (
                handoff_id,
                host_id,
                from_run_id,
                task_id,
                trigger_kind,
                status,
                provider,
                repo_root,
                canonical_json(packet),
                packet_hash,
                now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM handoff_packets WHERE packet_hash = ?",
            (packet_hash,),
        ).fetchone()
        assert row is not None
        return _handoff_from_row(row)

    def list_handoff_packets(self) -> list[HandoffPacket]:
        rows = self._conn.execute(
            "SELECT * FROM handoff_packets ORDER BY handoff_pk ASC"
        ).fetchall()
        return [_handoff_from_row(row) for row in rows]

    def record_completed_work(self, entry: CompletedWorkEntry) -> CompletedWorkEntry:
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO completed_work_index(
                work_id, idempotency_key, host_id, repo_id, task_id, run_id,
                completed_at, files_changed_json, symbols_affected_json,
                approach_taken, approaches_rejected_json,
                verification_results_json, follow_up_pointers_json, trace_id,
                source_event_offsets_json, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (
                entry.work_id,
                entry.idempotency_key,
                entry.host_id,
                entry.repo_id,
                entry.task_id,
                entry.run_id,
                entry.completed_at or now,
                canonical_json(list(entry.files_changed)),
                canonical_json(list(entry.symbols_affected)),
                entry.approach_taken,
                canonical_json(list(entry.approaches_rejected)),
                canonical_json(entry.verification_results or {}),
                canonical_json([dict(pointer) for pointer in entry.follow_up_pointers]),
                entry.trace_id,
                canonical_json(entry.source_event_offsets or {}),
                canonical_json(entry.metadata or {}),
                now,
                now,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM completed_work_index WHERE idempotency_key = ?",
            (entry.idempotency_key,),
        ).fetchone()
        assert row is not None
        work_id = row["work_id"]
        self._conn.execute(
            "DELETE FROM completed_work_files WHERE work_id = ?",
            (work_id,),
        )
        self._conn.execute(
            "DELETE FROM completed_work_symbols WHERE work_id = ?",
            (work_id,),
        )
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO completed_work_files(
                work_id, file_path, normalized_file_path
            )
            VALUES (?, ?, ?)
            """,
            [
                (work_id, path, normalize_completed_work_file_path(path))
                for path in entry.files_changed
            ],
        )
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO completed_work_symbols(work_id, symbol)
            VALUES (?, ?)
            """,
            [(work_id, symbol) for symbol in entry.symbols_affected],
        )
        self._conn.commit()
        saved = self.get_completed_work(work_id)
        assert saved is not None
        return saved

    def get_completed_work(self, work_id: str) -> CompletedWorkEntry | None:
        row = self._conn.execute(
            "SELECT * FROM completed_work_index WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        return _completed_work_from_row(row) if row is not None else None

    def list_completed_work_by_symbol(self, symbol: str) -> list[CompletedWorkEntry]:
        rows = self._conn.execute(
            """
            SELECT completed_work_index.*
              FROM completed_work_index
              JOIN completed_work_symbols
                ON completed_work_symbols.work_id = completed_work_index.work_id
             WHERE completed_work_symbols.symbol = ?
             ORDER BY completed_work_index.completed_at DESC,
                      completed_work_index.work_pk DESC
            """,
            (str(symbol or "").strip(),),
        ).fetchall()
        return [_completed_work_from_row(row) for row in rows]

    def list_completed_work_by_file(self, file_path: str) -> list[CompletedWorkEntry]:
        rows = self._conn.execute(
            """
            SELECT completed_work_index.*
              FROM completed_work_index
              JOIN completed_work_files
                ON completed_work_files.work_id = completed_work_index.work_id
             WHERE completed_work_files.normalized_file_path = ?
             ORDER BY completed_work_index.completed_at DESC,
                      completed_work_index.work_pk DESC
            """,
            (normalize_completed_work_file_path(file_path),),
        ).fetchall()
        return [_completed_work_from_row(row) for row in rows]

    def record_cma_invocation(self, record: CMAInvocationRecord) -> CMAInvocationRecord:
        now = record.created_at or utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO cma_invocations(
                invocation_id, run_id, task_id, trigger_event_kind, tier,
                model_id, status, decision_kind, correction_pointer_ids_json,
                rationale, escalate, observed_tokens, budget_tokens, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invocation_id) DO UPDATE SET
                run_id = excluded.run_id,
                task_id = excluded.task_id,
                trigger_event_kind = excluded.trigger_event_kind,
                tier = excluded.tier,
                model_id = excluded.model_id,
                status = excluded.status,
                decision_kind = excluded.decision_kind,
                correction_pointer_ids_json = excluded.correction_pointer_ids_json,
                rationale = excluded.rationale,
                escalate = excluded.escalate,
                observed_tokens = excluded.observed_tokens,
                budget_tokens = excluded.budget_tokens,
                created_at = excluded.created_at
            """,
            (
                record.invocation_id,
                record.run_id,
                record.task_id,
                record.trigger_event_kind,
                record.tier,
                record.model_id,
                record.status,
                record.decision_kind,
                canonical_json(list(record.correction_pointer_ids)),
                record.rationale,
                1 if record.escalate else 0,
                record.observed_tokens,
                record.budget_tokens,
                now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM cma_invocations WHERE invocation_id = ?",
            (record.invocation_id,),
        ).fetchone()
        assert row is not None
        return _cma_invocation_from_row(row)

    def list_cma_invocations(self, run_id: str | None = None) -> list[CMAInvocationRecord]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM cma_invocations ORDER BY invocation_pk ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM cma_invocations
                 WHERE run_id = ?
                 ORDER BY invocation_pk ASC
                """,
                (run_id,),
            ).fetchall()
        return [_cma_invocation_from_row(row) for row in rows]

    def count_active_cma_invocations(self, window_seconds: float = 600.0) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - max(
            0.0,
            float(window_seconds),
        )
        active = 0
        for record in self.list_cma_invocations():
            if record.status not in {"invoked", "escalated"}:
                continue
            created = _iso_timestamp(record.created_at)
            if created is not None and created >= cutoff:
                active += 1
        return active

    def last_cma_invocation_for_run(self, run_id: str) -> CMAInvocationRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM cma_invocations
             WHERE run_id = ?
             ORDER BY created_at DESC, invocation_pk DESC
             LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return _cma_invocation_from_row(row) if row is not None else None

    def get_manifest_by_request_hash(self, request_hash: str) -> ContextManifest | None:
        row = self._conn.execute(
            "SELECT * FROM context_manifests WHERE request_hash = ?",
            (request_hash,),
        ).fetchone()
        return _manifest_from_row(row) if row is not None else None

    def store_manifest(self, manifest: ContextManifest) -> ContextManifest:
        now = manifest.created_at or utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO context_manifests(
                manifest_id, request_hash, status, host_id, repo_id, task_id,
                run_id, provider, route_scope, pointer_ids_json,
                required_pointer_ids_json, load_order_json, omitted_json, token_budget_json,
                estimated_tokens, source_hashes_json, peer_agent_states_json,
                expires_at, signature_key_id, signature, signed_payload,
                error_kind, error_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_hash) DO UPDATE SET
                manifest_id = excluded.manifest_id,
                status = excluded.status,
                host_id = excluded.host_id,
                repo_id = excluded.repo_id,
                task_id = excluded.task_id,
                run_id = excluded.run_id,
                provider = excluded.provider,
                route_scope = excluded.route_scope,
                pointer_ids_json = excluded.pointer_ids_json,
                required_pointer_ids_json = excluded.required_pointer_ids_json,
                load_order_json = excluded.load_order_json,
                omitted_json = excluded.omitted_json,
                token_budget_json = excluded.token_budget_json,
                estimated_tokens = excluded.estimated_tokens,
                source_hashes_json = excluded.source_hashes_json,
                peer_agent_states_json = excluded.peer_agent_states_json,
                expires_at = excluded.expires_at,
                signature_key_id = excluded.signature_key_id,
                signature = excluded.signature,
                signed_payload = excluded.signed_payload,
                error_kind = excluded.error_kind,
                error_message = excluded.error_message,
                created_at = excluded.created_at
            WHERE context_manifests.status != 'signed'
               OR excluded.status = 'signed'
            """,
            (
                manifest.manifest_id,
                manifest.request_hash,
                manifest.status,
                manifest.host_id,
                manifest.repo_id,
                manifest.task_id,
                manifest.run_id,
                manifest.provider,
                manifest.route_scope,
                canonical_json(list(manifest.pointer_ids)),
                canonical_json(list(manifest.required_pointer_ids)),
                canonical_json(list(manifest.load_order)),
                canonical_json([dict(item) for item in manifest.omitted_context]),
                canonical_json(manifest.token_budget or {}),
                manifest.estimated_tokens,
                canonical_json(manifest.source_hashes or {}),
                canonical_json([dict(item) for item in manifest.peer_agent_states]),
                manifest.expires_at,
                manifest.signature_key_id,
                manifest.signature,
                manifest.signed_payload,
                manifest.error_kind,
                manifest.error_message,
                now,
            ),
        )
        self._conn.commit()
        if manifest.request_hash:
            saved = self.get_manifest_by_request_hash(manifest.request_hash)
            assert saved is not None
            return saved
        return manifest

    def _configure_connection(self) -> None:
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.DatabaseError:
            pass

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_sources(
                source_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL UNIQUE,
                source_uri TEXT NOT NULL UNIQUE,
                source_kind TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                host_id TEXT,
                repo_id TEXT,
                provider TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS context_pointers(
                pointer_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                pointer_id TEXT NOT NULL UNIQUE,
                source_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                pointer_kind TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                tokens_estimate INTEGER NOT NULL DEFAULT 0,
                sensitivity TEXT NOT NULL,
                host_id TEXT,
                repo_id TEXT,
                provider TEXT,
                target_symbols_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]',
                required INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_uri, content_hash, locator_json)
            );

            CREATE TABLE IF NOT EXISTS context_relevance_scores(
                score_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                score_id TEXT NOT NULL UNIQUE,
                pointer_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                target_symbol TEXT,
                score REAL NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_context_leases(
                lease_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                lease_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                budget_tokens INTEGER NOT NULL DEFAULT 0,
                soft_limit_tokens INTEGER NOT NULL DEFAULT 0,
                hard_limit_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_used_tokens INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                context_manifest_hash TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS handoff_packets(
                handoff_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                handoff_id TEXT NOT NULL UNIQUE,
                host_id TEXT,
                from_run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                trigger_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT,
                repo_root TEXT,
                packet_json TEXT NOT NULL,
                packet_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS context_health_events(
                event_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                host_id TEXT,
                run_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                observed_tokens INTEGER NOT NULL DEFAULT 0,
                budget_tokens INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS context_manifests(
                manifest_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                manifest_id TEXT NOT NULL UNIQUE,
                request_hash TEXT UNIQUE,
                status TEXT NOT NULL,
                host_id TEXT NOT NULL,
                repo_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                route_scope TEXT NOT NULL DEFAULT 'local',
                pointer_ids_json TEXT NOT NULL DEFAULT '[]',
                required_pointer_ids_json TEXT NOT NULL DEFAULT '[]',
                load_order_json TEXT NOT NULL DEFAULT '[]',
                omitted_json TEXT NOT NULL DEFAULT '[]',
                token_budget_json TEXT NOT NULL DEFAULT '{}',
                estimated_tokens INTEGER NOT NULL DEFAULT 0,
                source_hashes_json TEXT NOT NULL DEFAULT '{}',
                peer_agent_states_json TEXT NOT NULL DEFAULT '[]',
                expires_at TEXT,
                signature_key_id TEXT,
                signature TEXT,
                signed_payload TEXT,
                error_kind TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS completed_work_index(
                work_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                host_id TEXT,
                repo_id TEXT,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                files_changed_json TEXT NOT NULL DEFAULT '[]',
                symbols_affected_json TEXT NOT NULL DEFAULT '[]',
                approach_taken TEXT NOT NULL DEFAULT '',
                approaches_rejected_json TEXT NOT NULL DEFAULT '[]',
                verification_results_json TEXT NOT NULL DEFAULT '{}',
                follow_up_pointers_json TEXT NOT NULL DEFAULT '[]',
                trace_id TEXT,
                source_event_offsets_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS completed_work_files(
                file_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                normalized_file_path TEXT NOT NULL,
                UNIQUE(work_id, normalized_file_path)
            );

            CREATE INDEX IF NOT EXISTS idx_completed_work_files_path
                ON completed_work_files(normalized_file_path);

            CREATE TABLE IF NOT EXISTS completed_work_symbols(
                symbol_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                UNIQUE(work_id, symbol)
            );

            CREATE INDEX IF NOT EXISTS idx_completed_work_symbols_symbol
                ON completed_work_symbols(symbol);

            CREATE TABLE IF NOT EXISTS cma_invocations(
                invocation_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                trigger_event_kind TEXT NOT NULL,
                tier INTEGER NOT NULL DEFAULT 1,
                model_id TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_kind TEXT,
                correction_pointer_ids_json TEXT NOT NULL DEFAULT '[]',
                rationale TEXT NOT NULL DEFAULT '',
                escalate INTEGER NOT NULL DEFAULT 0,
                observed_tokens INTEGER NOT NULL DEFAULT 0,
                budget_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_context_manifest_columns()
        self._conn.commit()

    def _ensure_context_manifest_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(context_manifests)")
        }
        if "route_scope" not in columns:
            self._conn.execute(
                "ALTER TABLE context_manifests "
                "ADD COLUMN route_scope TEXT NOT NULL DEFAULT 'local'"
            )


def _source_from_row(row: sqlite3.Row) -> ContextSource:
    return ContextSource(
        source_id=row["source_id"],
        source_uri=row["source_uri"],
        source_kind=row["source_kind"],
        source_hash=row["source_hash"],
        sensitivity=row["sensitivity"],
        host_id=row["host_id"],
        repo_id=row["repo_id"],
        provider=row["provider"],
        metadata=_json_dict(row["metadata_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _pointer_from_row(row: sqlite3.Row) -> ContextPointer:
    return ContextPointer(
        pointer_id=row["pointer_id"],
        source_id=row["source_id"],
        source_uri=row["source_uri"],
        source_kind=row["source_kind"],
        pointer_kind=row["pointer_kind"],
        content_hash=row["content_hash"],
        locator=_json_dict(row["locator_json"]),
        summary=row["summary"],
        tokens_estimate=max(0, int(row["tokens_estimate"] or 0)),
        sensitivity=row["sensitivity"],
        host_id=row["host_id"],
        repo_id=row["repo_id"],
        provider=row["provider"],
        target_symbols=string_tuple(row["target_symbols_json"]),
        tags=string_tuple(row["tags_json"]),
        required=bool(row["required"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _health_event_from_row(row: sqlite3.Row) -> ContextHealthEvent:
    return ContextHealthEvent(
        event_id=row["event_id"],
        host_id=row["host_id"],
        run_id=row["run_id"],
        agent_id=row["agent_id"],
        task_id=row["task_id"],
        event_kind=row["event_kind"],
        severity=row["severity"],
        observed_tokens=int(row["observed_tokens"] or 0),
        budget_tokens=int(row["budget_tokens"] or 0),
        details=_json_dict(row["details_json"]),
        created_at=row["created_at"],
    )


def _handoff_from_row(row: sqlite3.Row) -> HandoffPacket:
    return HandoffPacket(
        handoff_id=row["handoff_id"],
        host_id=row["host_id"],
        from_run_id=row["from_run_id"],
        task_id=row["task_id"],
        trigger_kind=row["trigger_kind"],
        status=row["status"],
        provider=row["provider"],
        repo_root=row["repo_root"],
        packet=_json_dict(row["packet_json"]),
        packet_hash=row["packet_hash"],
        created_at=row["created_at"],
        consumed_at=row["consumed_at"],
    )


def _completed_work_from_row(row: sqlite3.Row) -> CompletedWorkEntry:
    return CompletedWorkEntry(
        work_id=row["work_id"],
        idempotency_key=row["idempotency_key"],
        host_id=row["host_id"],
        repo_id=row["repo_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        completed_at=row["completed_at"],
        files_changed=string_tuple(row["files_changed_json"]),
        symbols_affected=string_tuple(row["symbols_affected_json"]),
        approach_taken=row["approach_taken"] or "",
        approaches_rejected=json_tuple(row["approaches_rejected_json"]),
        verification_results=_json_dict(row["verification_results_json"]),
        follow_up_pointers=mapping_tuple(row["follow_up_pointers_json"]),
        trace_id=row["trace_id"],
        source_event_offsets=_json_dict(row["source_event_offsets_json"]),
        metadata=_json_dict(row["metadata_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _manifest_from_row(row: sqlite3.Row) -> ContextManifest:
    return ContextManifest(
        manifest_id=row["manifest_id"],
        request_hash=row["request_hash"],
        status=row["status"],
        host_id=row["host_id"],
        repo_id=row["repo_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        provider=row["provider"],
        route_scope=row["route_scope"],
        pointer_ids=string_tuple(row["pointer_ids_json"]),
        required_pointer_ids=string_tuple(row["required_pointer_ids_json"]),
        load_order=string_tuple(row["load_order_json"]),
        omitted_context=mapping_tuple(row["omitted_json"]),
        token_budget=_json_dict(row["token_budget_json"]),
        estimated_tokens=int(row["estimated_tokens"] or 0),
        source_hashes=_json_dict(row["source_hashes_json"]),
        peer_agent_states=mapping_tuple(row["peer_agent_states_json"]),
        expires_at=row["expires_at"],
        signature_key_id=row["signature_key_id"],
        signature=row["signature"],
        signed_payload=row["signed_payload"],
        error_kind=row["error_kind"],
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


def _cma_invocation_from_row(row: sqlite3.Row) -> CMAInvocationRecord:
    return CMAInvocationRecord(
        invocation_id=row["invocation_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        trigger_event_kind=row["trigger_event_kind"],
        tier=int(row["tier"] or 1),
        model_id=row["model_id"],
        status=row["status"],
        decision_kind=row["decision_kind"],
        correction_pointer_ids=string_tuple(row["correction_pointer_ids_json"]),
        rationale=row["rationale"] or "",
        escalate=bool(row["escalate"]),
        observed_tokens=int(row["observed_tokens"] or 0),
        budget_tokens=int(row["budget_tokens"] or 0),
        created_at=row["created_at"],
    )


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _is_in_memory_path(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text == ":memory:" or text.startswith("file::memory:")
